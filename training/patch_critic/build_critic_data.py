import json
import logging
import random
from neo4j import GraphDatabase

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

URI = "bolt://localhost:7687"
AUTH = ("neo4j", "autobot_password")
import os
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PR_JSONL = os.path.join(SCRIPT_DIR, "../../etl/training_data/prs_clean.jsonl")
OUTPUT_FILE = os.path.join(SCRIPT_DIR, "critic_train_graphrag.jsonl")

def extract_diff(files):
    # Extracts the first hunk per file capped to prevent OOM
    parts = []
    for f in files:
        patch = f.get("patch", "")
        if not patch: continue
        
        # Super minimal first hunk extraction
        lines = patch.split('\n')
        hunk_count = 0
        hunk_lines = []
        for line in lines:
            if line.startswith('@@'):
                hunk_count += 1
                if hunk_count == 2: break
            hunk_lines.append(line)
            
        parts.append(f"{f.get('filename')}:\n" + "\n".join(hunk_lines))
        
    return "\n".join(parts)[:1500]

def build_critic_data():
    driver = GraphDatabase.driver(URI, auth=AUTH)
    
    with open(PR_JSONL, "r", encoding="utf-8") as f:
        prs = [json.loads(line) for line in f]
        
    merged_prs = [p for p in prs if p.get("pr", {}).get("merged_at")]
    not_merged_prs = [p for p in prs if not p.get("pr", {}).get("merged_at")]
    
    random.shuffle(merged_prs)
    random.shuffle(not_merged_prs)
    
    logging.info(f"Loaded {len(merged_prs)} merged, {len(not_merged_prs)} not-merged.")
    
    # Pre-compile Neo4j queries
    history_query = """
    UNWIND $file_names AS fname
    MATCH (r:Review)-[:APPLIES_TO]->(f:File {filename: fname})
    WHERE r.body IS NOT NULL
    RETURN f.filename AS file, collect(r.body)[0..2] AS historic_comments
    """
    
    def get_friction(session, file_names):
        if not file_names: return "None found."
        res = session.run(history_query, parameters={"file_names": file_names})
        idioms = []
        for record in res:
            bodies = [b.replace('\n', ' ')[:100] for b in record["historic_comments"] if len(b) > 20]
            if bodies:
                idioms.append(f"- Past reviews on `{record['file']}`: {' | '.join(bodies)}")
        return "\n".join(idioms) if idioms else "None found."
        
    def build_record(session, record, label, reasoning):
        fnames = [f["filename"] for f in (record.get("files") or []) if "filename" in f]
        friction = get_friction(session, fnames)
        
        plan = (record.get("pr", {}).get("body") or "Fix issue").replace('\n', ' ')[:200]
        diff = extract_diff(record.get("files") or [])
        
        inp = (
            "--- HISTORICAL REVIEW FRICTION ---\n"
            f"{friction}\n\n"
            "--- CURRENT PATCH ---\n"
            f"PLAN: {plan}\n\n"
            f"DIFF:\n{diff}\n\n"
            "TASK: Evaluate Patch. Output VERDICT and REASONING."
        )
        out = f"VERDICT: {label}\nREASONING: {reasoning}"
        return {"input": inp, "output": out}

    with driver.session() as session:
        # 1. BUILD REJECTS
        reject_pairs = []
        for r in not_merged_prs:
            if not any(f.get("patch") for f in (r.get("files") or [])): continue
            
            reviews = r.get("reviews") or []
            rcs = r.get("review_comments") or []
            cr = [rev for rev in reviews if rev.get("state") == "CHANGES_REQUESTED" and len(rev.get("body","")) > 20]
            sub = [c for c in rcs if c.get("diff_hunk") and len(c.get("body","")) > 20 and not c.get("body","").startswith("```")]
            
            if cr: reasoning = cr[0].get("body", "")[:400]
            elif sub: reasoning = sub[0].get("body", "")[:400]
            else: continue
            
            reject_pairs.append(build_record(session, r, "REJECT", reasoning))
            
        ACTUAL_CAP = min(len(reject_pairs), 250)
        reject_pairs = reject_pairs[:ACTUAL_CAP]
        logging.info(f"Generated {len(reject_pairs)} REJECTs. Setting Cap to {ACTUAL_CAP}.")
        
        # 2. BUILD REVISE
        revise_pairs = []
        for r in merged_prs:
            if len(revise_pairs) >= ACTUAL_CAP: break
            if not any(f.get("patch") for f in (r.get("files") or [])): continue
            
            reviews = r.get("reviews") or []
            rcs = r.get("review_comments") or []
            cr = [rev for rev in reviews if rev.get("state") == "CHANGES_REQUESTED" and len(rev.get("body","")) > 20]
            sub = [c for c in rcs if c.get("diff_hunk") and len(c.get("body","")) > 20 and not c.get("body","").startswith("```")]
            
            if not (cr or sub): continue
            
            if sub: reasoning = sub[0].get("body", "")[:400]
            else: reasoning = cr[0].get("body", "")[:400]
            
            revise_pairs.append(build_record(session, r, "REVISE", reasoning))
            
        logging.info(f"Generated {len(revise_pairs)} REVISEs.")
        
        # 3. BUILD ACCEPT
        # No GPT-4o Teacher labeling employed - we use diff characteristics as per your notebook instruction!
        accept_pairs = []
        for r in merged_prs:
            if len(accept_pairs) >= ACTUAL_CAP: break
            if not any(f.get("patch") for f in (r.get("files") or [])): continue
            
            reviews = r.get("reviews") or []
            rcs = r.get("review_comments") or []
            
            if any(rev.get("state") == "CHANGES_REQUESTED" for rev in reviews): continue
            if any(c.get("diff_hunk") and len(c.get("body","")) > 20 for c in rcs): continue
            
            # Simple heuristic reasoning
            files = r.get("files") or []
            if len(files) == 1: reasoning = "Targeted single-file fix with minimal footprint. LGTM."
            elif any("test" in f.get("filename", "").lower() for f in files): reasoning = "Patch includes test coverage and looks clean. No objections."
            else: reasoning = "Clean patch addressing the described issue."
            
            accept_pairs.append(build_record(session, r, "ACCEPT", reasoning))

        logging.info(f"Generated {len(accept_pairs)} ACCEPTs.")
        
        all_pairs = reject_pairs + revise_pairs + accept_pairs
        random.shuffle(all_pairs)
        
        with open(OUTPUT_FILE, "w", encoding="utf-8") as out:
            for p in all_pairs:
                out.write(json.dumps(p) + "\n")
                
    driver.close()
    logging.info(f"Critic dataset compiled! Extracted {len(all_pairs)} balanced DPO pairs to {OUTPUT_FILE}")

if __name__ == "__main__":
    build_critic_data()
