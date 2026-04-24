import os
from neo4j import GraphDatabase
# No OpenAI needed! We use a free, local model.
from sentence_transformers import SentenceTransformer
from tqdm import tqdm

URI = "bolt://localhost:7687"
AUTH = ("neo4j", "autobot_password")

# Load a powerful, lightweight local embedding model. 
# all-MiniLM-L6-v2 works extremely well for semantic similarity and runs purely on your CPU/GPU for FREE.
print("Loading local embedding model...")
model = SentenceTransformer('all-MiniLM-L6-v2') 

def create_vector_index(driver):
    with driver.session() as session:
        # We drop the old OpenAI 1536-dimension index if it exists
        session.run("DROP INDEX issue_embeddings IF EXISTS")
        
        print("Creating Vector Index for 'embedding' property on 'Issue' nodes...")
        try:
            # MiniLM-L6-v2 uses exactly 384 dimensions.
            session.run("""
            CREATE VECTOR INDEX issue_embeddings IF NOT EXISTS
            FOR (i:Issue)
            ON (i.embedding)
            OPTIONS {indexConfig: {
              `vector.dimensions`: 384,
              `vector.similarity_function`: 'cosine'
            }}
            """)
            print("Vector Index created successfully with 384 dimensions.")
        except Exception as e:
            print(f"Error creating vector index: {e}")

def vectorize_issues(driver):
    print("Fetching un-vectorized issues...")
    fetch_query = """
    MATCH (i:Issue)
    WHERE i.embedding IS NULL AND i.title IS NOT NULL
    RETURN i.number AS number, i.title AS title, i.body_truncated AS body
    """
    
    update_query = """
    UNWIND $batch AS record
    MATCH (i:Issue {number: record.number})
    SET i.embedding = record.embedding
    """

    with driver.session() as session:
        result = session.run(fetch_query)
        issues = [{"number": record["number"], "text": f"{record['title']}\n{record['body']}"} for record in result]

    print(f"Found {len(issues)} issues to vectorize.")
    if not issues:
        return

    batch_size = 500
    current_batch = []
    
    for issue in tqdm(issues, desc="Vectorizing Issues locally"):
        try:
            # Generate the embedding completely locally 
            # .tolist() converts the numpy array to a python list for Neo4j
            embedding = model.encode(issue["text"]).tolist()
            
            current_batch.append({
                "number": issue["number"],
                "embedding": embedding
            })
            
            if len(current_batch) >= batch_size:
                with driver.session() as session:
                    session.run(update_query, parameters={"batch": current_batch})
                current_batch = []
                
        except Exception as e:
            print(f"Failed embedding for issue {issue['number']}: {e}")
            
    # Flush remaining
    if current_batch:
        with driver.session() as session:
            session.run(update_query, parameters={"batch": current_batch})
            
    print("Vectorization completely finished!")

def main():
    driver = GraphDatabase.driver(URI, auth=AUTH)
    create_vector_index(driver)
    vectorize_issues(driver)
    driver.close()

if __name__ == "__main__":
    main()
