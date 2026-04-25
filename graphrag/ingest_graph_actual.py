import os
import json
import glob
from neo4j import GraphDatabase

URI = "bolt://localhost:7687"
AUTH = ("neo4j", "autobot_password")

# Path to your extracted data
DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../etl/training_data"))

def clear_graph(session):
    print("Clearing existing graph...")
    session.run("MATCH (n) DETACH DELETE n")
    print("Constraints being created...")
    session.run("CREATE CONSTRAINT IF NOT EXISTS FOR (i:Issue) REQUIRE i.number IS UNIQUE")
    session.run("CREATE CONSTRAINT IF NOT EXISTS FOR (p:PR) REQUIRE p.number IS UNIQUE")
    session.run("CREATE CONSTRAINT IF NOT EXISTS FOR (f:File) REQUIRE f.filename IS UNIQUE")
    session.run("CREATE CONSTRAINT IF NOT EXISTS FOR (r:Review) REQUIRE r.id IS UNIQUE")

def ingest_issues(driver):
    issue_files = glob.glob(os.path.join(DATA_DIR, "issues_clean*.jsonl"))
    print(f"Found {len(issue_files)} issue JSONL files.")
    
    query = """
    UNWIND $batch AS record
    MERGE (i:Issue {number: record.issue_number})
    SET i.title = record.issue.title,
        i.body_truncated = substring(record.issue.body, 0, 800),
        i.created_at = record.issue.created_at
        
    // For each linked PR, create the PR node and relationship
    WITH i, record
    UNWIND record.resolved_by_prs AS pr_num
    MERGE (p:PR {number: pr_num})
    MERGE (i)-[:RESOLVED_BY]->(p)
    """

    for file_path in issue_files:
        batch = []
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                    issue = data.get("issue", {})
                    if not issue:
                        continue
                    
                    record = {
                        "issue_number": data.get("issue_number"),
                        "issue": {
                            "title": issue.get("title", ""),
                            "body": issue.get("body") or "",
                            "created_at": issue.get("created_at", "")
                        },
                        "resolved_by_prs": data.get("resolved_by_prs") or []
                    }
                    batch.append(record)
                    
                    if len(batch) >= 500:
                        with driver.session() as session:
                            session.run(query, parameters={"batch": batch})
                        batch = []
                except Exception as e:
                    print(f"Error parsing line in {file_path}: {e}")
        
        # Flush remaining
        if batch:
            with driver.session() as session:
                session.run(query, parameters={"batch": batch})
    print("Finished ingesting Issues.")

def ingest_prs(driver):
    pr_files = glob.glob(os.path.join(DATA_DIR, "prs_clean*.jsonl"))
    print(f"Found {len(pr_files)} PR JSONL files.")

    # Due to size, we ingest PR metadata and files separately for cleanliness.
    pr_query = """
    UNWIND $batch AS record
    MERGE (p:PR {number: record.pr_number})
    SET p.title = record.pr_title,
        p.body_truncated = substring(record.pr_body, 0, 1000),
        p.merged_at = record.merged_at
        
    WITH p, record
    UNWIND record.files AS filename
    MERGE (f:File {filename: filename})
    MERGE (p)-[:TOUCHES]->(f)
    """
    
    review_query = """
    UNWIND $batch AS record
    MERGE (p:PR {number: record.pr_number})
    
    WITH p, record
    UNWIND record.reviews AS review
    MERGE (r:Review {id: review.id})
    SET r.body = review.body,
        r.state = review.state,
        r.is_inline_comment = review.is_inline_comment,
        r.diff_hunk = review.diff_hunk
    MERGE (r)-[:REVIEWED_IN]->(p)
    
    // Connect Review to File if it's an inline comment
    WITH r, p, review
    WHERE review.filename IS NOT NULL AND review.filename <> ""
    MERGE (f:File {filename: review.filename})
    MERGE (r)-[:APPLIES_TO]->(f)
    """

    for file_path in pr_files:
        pr_batch = []
        review_batch = []
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                    pr = data.get("pr", {})
                    if not pr:
                        continue
                        
                    pr_num = pr.get("number")
                    
                    files = data.get("files") or []
                    filenames = [f.get("filename") for f in files if getattr(f, "get", lambda x: None)("filename")]
                    
                    pr_record = {
                        "pr_number": pr_num,
                        "pr_title": pr.get("title", ""),
                        "pr_body": pr.get("body") or "",
                        "merged_at": pr.get("merged_at", ""),
                        "files": filenames
                    }
                    pr_batch.append(pr_record)
                    
                    reviews = []
                    # Add PR Reviews
                    for rev in (data.get("reviews") or []):
                        if not rev.get("id"): continue
                        reviews.append({
                            "id": str(rev.get("id")) + "_review",
                            "body": rev.get("body") or "",
                            "state": rev.get("state", ""),
                            "is_inline_comment": False,
                            "diff_hunk": None,
                            "filename": None
                        })
                    
                    # Add PR Review Comments (inline code comments)
                    for comment in (data.get("review_comments") or []):
                        if not comment.get("id"): continue
                        reviews.append({
                            "id": str(comment.get("id")) + "_comment",
                            "body": comment.get("body") or "",
                            "state": "COMMENT", # It's inline friction
                            "is_inline_comment": True,
                            "diff_hunk": comment.get("diff_hunk"),
                            "filename": comment.get("path") # The file it targets
                        })
                        
                    if reviews:
                        review_batch.append({
                            "pr_number": pr_num,
                            "reviews": reviews
                        })
                    
                    if len(pr_batch) >= 200:
                        with driver.session() as session:
                            session.run(pr_query, parameters={"batch": pr_batch})
                            if review_batch:
                                session.run(review_query, parameters={"batch": review_batch})
                        pr_batch = []
                        review_batch = []
                        
                except Exception as e:
                    print(f"Error parsing line in {file_path}: {e}")
        
        # Flush remaining
        if pr_batch:
            with driver.session() as session:
                session.run(pr_query, parameters={"batch": pr_batch})
                if review_batch:
                    session.run(review_query, parameters={"batch": review_batch})
    print("Finished ingesting PRs and Reviews.")

def run_ingestion():
    driver = GraphDatabase.driver(URI, auth=AUTH)
    with driver.session() as session:
        clear_graph(session)
        
    ingest_issues(driver)
    ingest_prs(driver)
    
    driver.close()
    print("Graph Ingestion Complete! You can now query Neo4j for extractions.")

if __name__ == "__main__":
    run_ingestion()
