import os
import json
import random
import math
import re
import statistics
from pathlib import Path
from neo4j import GraphDatabase
from tqdm import tqdm

URI = "bolt://localhost:7687"
AUTH = ("neo4j", "autobot_password")

# Balanced training set: equal YES (linked PR) vs NO (closed without PR) examples.
NUM_YES = 1500
NUM_NO = 1500
GRAPH_TOP_K = 6
TS_MAX_FILES = 6
TS_MAX_CLASSES_PER_FILE = 4
TS_MAX_FUNCTIONS_PER_FILE = 6

SYSTEM_PROMPT = (
    "You are AutoBot Planner. Decide if code change is required and output "
    "STRICT plain text only. Never use markdown, bold, bullets with **, or prose outside format.\n"
    "STRICT GROUNDING: You are an engineering tool. You must only select target files from the "
    "provided context blocks. Do not hallucinate paths or add file extensions not present in the "
    "retrieval. If a change is needed but no file is in context, identify the most relevant "
    "directory from the tree-sitter context.\n"
    "DECISION POLICY: Do not output NO solely because an exact target file is missing from context. "
    "If issue evidence indicates a real code change is needed, output YES and anchor to the best "
    "supported module or directory from the provided context.\n"
    "The user message may include a --- RETRIEVAL EVIDENCE --- block; use it as context only. "
    "Do not echo it and do not output a CONFIDENCE field.\n"
    "If code change is needed, output:\n"
    "REQUIRES_CODE_CHANGE: YES\n"
    "REASON: <one sentence>\n"
    "PLAN:\n"
    "- What to change: <one concise paragraph>\n"
    "- Target files:\n"
    "  - <repo/path.py>\n"
    "- Test strategy: <one sentence>\n"
    "If code change is not needed, output:\n"
    "REQUIRES_CODE_CHANGE: NO\n"
    "REASON: <one sentence>"
)


def to_chatml(user_prompt, assistant_output):
    return (
        f"<|im_start|>system\n{SYSTEM_PROMPT}<|im_end|>\n"
        f"<|im_start|>user\n{user_prompt}<|im_end|>\n"
        f"<|im_start|>assistant\n{assistant_output}<|im_end|>"
    )


def first_n_words(text, n=300):
    words = (text or "").replace("\n", " ").split()
    return " ".join(words[:n])


def synthesize_no_reason(title: str, body: str | None) -> str:
    """
    One-sentence rationale for REQUIRES_CODE_CHANGE: NO (no linked PR).
    Grounded in title/body heuristics; safe single line for training targets.
    """
    title = (title or "").strip()
    body = (body or "").strip()
    blob = f"{title}\n{body}".replace("\n", " ")
    blob = re.sub(r"\s+", " ", blob).strip()
    low = blob.lower()

    if any(k in low for k in ("duplicate", "dupe of", "closing as duplicate", "superseded by")):
        return (
            "This was closed as a duplicate or superseded report, so it does not define "
            "a new repository code-change task."
        )
    if any(k in low for k in ("wontfix", "won't fix", "invalid", "not a bug", "by design", "works as intended")):
        return (
            "Maintainers marked this invalid, by design, or otherwise not requiring "
            "a code change."
        )

    hint = first_n_words(f"{title} {body}", 32)
    hint = re.sub(r"\s+", " ", hint).strip()
    if len(hint) > 140:
        hint = hint[:137].rsplit(" ", 1)[0] + "."
    return (
        f"This ticket was closed without a linked fixing PR in the source data, so this training label is NO "
        f"(context snippet: {hint})."
    )


def clean_pr_body_for_plan(text: str) -> str:
    """
    Remove PR-template noise and markup so planner target stays actionable.
    """
    if not text:
        return ""

    t = str(text)
    # Remove HTML comments and markdown image/link noise.
    t = re.sub(r"<!--.*?-->", " ", t, flags=re.DOTALL)
    t = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", t)  # markdown images
    t = re.sub(r"https?://\S+", " ", t)

    # Drop common PR template / license / contribution boilerplate lines.
    drop_markers = (
        "licensed to the apache software foundation",
        "contributor license agreements",
        "you may not use this file except in compliance",
        "thank you for contributing",
        "pull request guidelines",
        "read the pull request guidelines",
        "add meaningful description above",
    )

    kept_lines = []
    for raw_line in t.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        ll = line.lower()
        if any(marker in ll for marker in drop_markers):
            continue
        # Drop image/embed-like remnants and repetitive separators.
        if ll.startswith("<img") or ll.startswith("![]("):
            continue
        if set(line) <= {"-", "=", "*", "_"}:
            continue
        kept_lines.append(line)

    t = " ".join(kept_lines)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def clamp01(x):
    return max(0.0, min(1.0, float(x)))


def normalize_repo_path(path: str) -> str:
    """Normalize repo-style file paths for matching across sources."""
    if not path:
        return ""
    p = str(path).replace("\\", "/").strip()
    p = p.lstrip("./")
    if p.startswith("/"):
        # Keep only repo-relative suffix if absolute path accidentally appears.
        parts = [x for x in p.split("/") if x]
        for marker in ("airflow-core", "airflow", "providers", "tests", "task-sdk", "devel-common", "airflow-ctl"):
            if marker in parts:
                i = parts.index(marker)
                return "/".join(parts[i:])
    return p


def candidate_ts_paths(path: str) -> list[str]:
    """
    Generate candidate key variants for Tree-sitter index lookup.
    Handles monorepo layout drift, e.g.:
      airflow/... -> airflow-core/src/airflow/...
      airflow/providers/<pkg>/... -> providers/<pkg>/src/airflow/providers/<pkg>/...
    """
    p = normalize_repo_path(path)
    if not p:
        return []

    candidates = [p]

    # Common repo-name normalization drift.
    candidates.append(p.replace("task_sdk/", "task-sdk/"))
    candidates.append(p.replace("devel_common/", "devel-common/"))
    candidates.append(p.replace("airflow_ctl/", "airflow-ctl/"))

    # Historical path noise we often see from graph snapshots.
    candidates.append(p.replace("github/workflows/", ".github/workflows/"))
    candidates.append(p.replace("chart/files/", "chart/templates/"))

    if p.startswith("airflow/"):
        # New monorepo core layout.
        candidates.append(f"airflow-core/src/{p}")
        candidates.append(p.replace("airflow/", "airflow-core/src/airflow/", 1))
        candidates.append(p.replace("airflow/", "airflow-core/tests/", 1))
        candidates.append(p.replace("airflow/", "tests/", 1))

    provider_prefix = "airflow/providers/"
    if p.startswith(provider_prefix):
        rest = p[len(provider_prefix) :]
        parts = rest.split("/")
        if len(parts) >= 2:
            provider_name = parts[0]
            suffix = "/".join(parts[1:])
            candidates.append(
                f"providers/{provider_name}/src/airflow/providers/{provider_name}/{suffix}"
            )
            # Provider tests are common historical candidates.
            candidates.append(f"providers/{provider_name}/tests/{suffix}")
            candidates.append(f"providers/{provider_name}/tests/unit/{suffix}")
            candidates.append(f"providers/{provider_name}/tests/system/{suffix}")
            candidates.append(f"providers/{provider_name}/tests/integration/{suffix}")

    # Reverse-map monorepo provider src path back to historical path.
    if p.startswith("providers/"):
        parts = p.split("/")
        if len(parts) >= 6 and parts[2] == "src" and parts[3] == "airflow" and parts[4] == "providers":
            provider_name = parts[1]
            suffix = "/".join(parts[5:])
            candidates.append(f"airflow/providers/{provider_name}/{suffix}")

    # Tests path drift: tests/... <-> airflow-core/tests/... <-> providers/*/tests/...
    if p.startswith("tests/"):
        candidates.append(f"airflow-core/{p}")
        # tests/providers/<provider>/... -> providers/<provider>/tests/(unit|system|integration)/...
        parts = p.split("/")
        if len(parts) >= 4 and parts[1] == "providers":
            provider_name = parts[2]
            suffix = "/".join(parts[3:])
            candidates.append(f"providers/{provider_name}/tests/{suffix}")
            candidates.append(f"providers/{provider_name}/tests/unit/{suffix}")
            candidates.append(f"providers/{provider_name}/tests/system/{suffix}")
            candidates.append(f"providers/{provider_name}/tests/integration/{suffix}")

    if p.startswith("airflow-core/tests/"):
        suffix = p[len("airflow-core/tests/") :]
        candidates.append(f"tests/{suffix}")

    # UI/web split changed in some snapshots.
    if p.startswith("airflow/ui/"):
        candidates.append(p.replace("airflow/ui/", "airflow/www/", 1))
        candidates.append(p.replace("airflow/ui/", "airflow-core/src/airflow/ui/", 1))
    if p.startswith("airflow/www/"):
        candidates.append(p.replace("airflow/www/", "airflow/ui/", 1))
        candidates.append(p.replace("airflow/www/", "airflow-core/src/airflow/www/", 1))

    # api_connexion path can appear with/without airflow-core/src prefix.
    if p.startswith("airflow/api_connexion/"):
        candidates.append(f"airflow-core/src/{p}")
    if p.startswith("airflow-core/src/airflow/api_connexion/"):
        candidates.append(p.replace("airflow-core/src/", "", 1))

    # Deduplicate while preserving order.
    uniq = []
    seen = set()
    for c in candidates:
        if c not in seen:
            uniq.append(c)
            seen.add(c)
    return uniq


def resolve_treesitter_index_path() -> Path:
    """
    Locate tree-sitter index in preferred order:
      1) TREE_SITTER_INDEX_PATH env var
      2) repo_root/tree_sitter/treesitter_index.json
      3) repo_root/cli/patch_planner/treesitter/outputs/treesitter_index.json
    """
    env_path = os.getenv("TREE_SITTER_INDEX_PATH")
    if env_path:
        p = Path(env_path).expanduser().resolve()
        if p.exists():
            return p
        raise FileNotFoundError(f"TREE_SITTER_INDEX_PATH set but not found: {p}")

    this_file = Path(__file__).resolve()
    repo_root = this_file.parents[2]
    candidate_paths = [
        repo_root / "tree_sitter" / "treesitter_index.json",
        repo_root / "cli" / "patch_planner" / "treesitter" / "outputs" / "treesitter_index.json",
    ]
    for p in candidate_paths:
        if p.exists():
            return p
    raise FileNotFoundError(
        "Tree-sitter index not found. Expected one of:\n"
        f"  - {candidate_paths[0]}\n"
        f"  - {candidate_paths[1]}\n"
        "Or set TREE_SITTER_INDEX_PATH=/absolute/path/to/treesitter_index.json"
    )


def load_treesitter_index() -> dict:
    index_path = resolve_treesitter_index_path()
    with open(index_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    print(f"Loaded Tree-sitter index from: {index_path} ({len(data)} files)")
    # Normalize keys once for robust lookup.
    return {normalize_repo_path(k): v for k, v in data.items()}


def format_treesitter_context(index: dict, candidate_files: list, gt_files: list | None = None) -> str:
    """
    Build compact per-file structural context for Planner prompt.
    Priority:
      - GraphRAG candidates (top-K)
      - For YES examples, append ground-truth files not already included (capped)
    """
    gt_files = gt_files or []
    selected = []
    selected_raw = []

    for p in candidate_files[:GRAPH_TOP_K]:
        np = normalize_repo_path(p)
        if np and np not in selected_raw:
            selected_raw.append(np)

    # Add up to 2 GT files not present in candidate set (YES rows only).
    for p in gt_files:
        np = normalize_repo_path(p)
        if np and np not in selected_raw:
            selected_raw.append(np)
        if len(selected_raw) >= TS_MAX_FILES + 2:
            break

    selected_raw = selected_raw[: TS_MAX_FILES + 2]

    # Resolve selected paths to actual Tree-sitter keys using alias candidates.
    for p in selected_raw:
        for alias in candidate_ts_paths(p):
            if alias in index and alias not in selected:
                selected.append(alias)
                break

    lines = []
    included = 0
    for p in selected:
        entry = index.get(p)
        if not entry:
            continue
        classes = entry.get("classes", [])[:TS_MAX_CLASSES_PER_FILE]
        functions = entry.get("functions", [])[:TS_MAX_FUNCTIONS_PER_FILE]
        parts = []
        if classes:
            parts.append(f"classes: {', '.join(classes)}")
        if functions:
            parts.append(f"functions: {', '.join(functions)}")
        if not parts:
            continue
        lines.append(f"{p} | " + " | ".join(parts))
        included += 1
        if included >= TS_MAX_FILES:
            break

    return "\n".join(lines) if lines else "No matching Tree-sitter entries found for selected files."


def extract_graph_candidate_block(candidates: list[str]) -> str:
    return "\n".join(candidates) if candidates else "None found"


def collect_coverage_stats(stats: dict, *, is_yes: bool, candidates: list[str], ts_context: str) -> None:
    graph_has = bool(candidates)
    ts_has = "No matching Tree-sitter entries found" not in ts_context

    stats["total"] += 1
    if is_yes:
        stats["yes"] += 1
        stats["yes_graph_nonempty"] += int(graph_has)
        stats["yes_graph_none"] += int(not graph_has)
    else:
        stats["no"] += 1
        stats["no_graph_nonempty"] += int(graph_has)
        stats["no_graph_none"] += int(not graph_has)
    stats["ts_nonempty"] += int(ts_has)
    stats["ts_none"] += int(not ts_has)
    stats["graph_candidate_counts"].append(len(candidates))
    ts_line_count = 0 if not ts_has else len([x for x in ts_context.splitlines() if x.strip()])
    stats["ts_line_counts"].append(ts_line_count)


def collect_quality_stats(stats: dict, *, prompt: str, target: str, is_yes: bool) -> None:
    # Rough token estimate: ~4 chars/token for English/mixed code text.
    prompt_tokens_est = max(1, len(prompt) // 4)
    target_tokens_est = max(1, len(target) // 4)
    stats["prompt_tokens_est"].append(prompt_tokens_est)
    stats["target_tokens_est"].append(target_tokens_est)

    has_required = ("REQUIRES_CODE_CHANGE:" in target) and ("REASON:" in target)
    has_confidence = "CONFIDENCE:" in target
    stats["target_missing_required"] += int(not has_required)
    stats["target_has_confidence"] += int(has_confidence)

    if is_yes:
        has_plan = "PLAN:" in target and "- Target files:" in target and "- Test strategy:" in target
        stats["yes_missing_plan_fields"] += int(not has_plan)


def _summary_line(values: list[int], label: str) -> str:
    if not values:
        return f"  {label}: n=0"
    return (
        f"  {label}: n={len(values)} "
        f"min={min(values)} p50={int(statistics.median(values))} "
        f"p90={int(sorted(values)[int(0.9 * (len(values)-1))])} max={max(values)}"
    )


def print_coverage_stats(stats: dict, output_file: str) -> None:
    total = stats["total"]
    yes = stats["yes"]
    no = stats["no"]

    def pct(n, d):
        return (100.0 * n / d) if d else 0.0

    print("\n=== Planner dataset coverage stats ===")
    print(f"Total rows: {total}")
    print(f"YES rows: {yes}")
    print(f"NO rows: {no}")
    print("\nGraphRAG candidate coverage:")
    print(
        f"  YES with >=1 candidate: {stats['yes_graph_nonempty']} ({pct(stats['yes_graph_nonempty'], yes):.1f}%)"
    )
    print(f"  YES with none:          {stats['yes_graph_none']} ({pct(stats['yes_graph_none'], yes):.1f}%)")
    print(
        f"  NO with >=1 candidate:  {stats['no_graph_nonempty']} ({pct(stats['no_graph_nonempty'], no):.1f}%)"
    )
    print(f"  NO with none:           {stats['no_graph_none']} ({pct(stats['no_graph_none'], no):.1f}%)")
    print("\nTree-sitter context coverage (all rows):")
    print(f"  Non-empty TS context:   {stats['ts_nonempty']} ({pct(stats['ts_nonempty'], total):.1f}%)")
    print(f"  Empty TS context:       {stats['ts_none']} ({pct(stats['ts_none'], total):.1f}%)")
    print("\nDistribution checks:")
    print(_summary_line(stats["graph_candidate_counts"], "Graph candidate count/row"))
    print(_summary_line(stats["ts_line_counts"], "Tree-sitter lines/row"))
    print(_summary_line(stats["prompt_tokens_est"], "Prompt tokens est./row"))
    print(_summary_line(stats["target_tokens_est"], "Target tokens est./row"))
    print("\nFormat checks:")
    print(f"  Targets missing required fields: {stats['target_missing_required']}")
    print(f"  Targets with forbidden CONFIDENCE: {stats['target_has_confidence']}")
    print(f"  YES targets missing PLAN structure: {stats['yes_missing_plan_fields']}")
    print(f"\nOutput file: {output_file}")


def score_retrieval_confidence(sim_scores, file_weights):
    """
    Confidence from retrieval quality (not candidate count):
      - similarity strength
      - top-file margin (consensus)
      - entropy (concentration)
    """
    if not sim_scores or not file_weights:
        return {
            "raw": 0.0,
            "bucket": "LOW",
            "sim_top1": 0.0,
            "sim_top3_mean": 0.0,
            "top1_weight": 0.0,
            "top2_weight": 0.0,
            "margin_norm": 0.0,
            "entropy_norm": 1.0,
            "candidate_count": len(file_weights),
        }

    sim_sorted = sorted([float(s) for s in sim_scores], reverse=True)
    sim_top1 = clamp01(sim_sorted[0])
    sim_top3_mean = clamp01(sum(sim_sorted[:3]) / min(3, len(sim_sorted)))

    w_sorted = sorted([float(w) for w in file_weights.values()], reverse=True)
    top1 = w_sorted[0]
    top2 = w_sorted[1] if len(w_sorted) > 1 else 0.0
    margin_norm = clamp01((top1 - top2) / max(top1, 1e-9))

    total = sum(w_sorted)
    probs = [w / total for w in w_sorted if w > 0]
    entropy = -sum(p * math.log(p + 1e-12) for p in probs)
    max_entropy = math.log(max(len(probs), 1))
    entropy_norm = clamp01(entropy / max(max_entropy, 1e-9)) if probs else 1.0
    concentration = 1.0 - entropy_norm

    raw = clamp01((0.45 * sim_top3_mean) + (0.35 * margin_norm) + (0.20 * concentration))
    if raw >= 0.72:
        bucket = "HIGH"
    elif raw >= 0.45:
        bucket = "MEDIUM"
    else:
        bucket = "LOW"

    return {
        "raw": round(raw, 3),
        "bucket": bucket,
        "sim_top1": round(sim_top1, 3),
        "sim_top3_mean": round(sim_top3_mean, 3),
        "top1_weight": round(top1, 3),
        "top2_weight": round(top2, 3),
        "margin_norm": round(margin_norm, 3),
        "entropy_norm": round(entropy_norm, 3),
        "candidate_count": len(file_weights),
    }

def get_planner_training_data(driver):
    with driver.session() as session:
        print("Fetching YES samples (Issues with linked PRs)...")
        # Issues that have a PR (meaning a code change was needed and applied)
        yes_query = """
        MATCH (i:Issue)-[:RESOLVED_BY]->(p:PR)
        WHERE i.embedding IS NOT NULL
        RETURN i.number AS issue_number, i.title AS title, i.body_truncated AS body,
               collect(DISTINCT p.body_truncated) AS pr_bodies,
               [(p)-[:TOUCHES]->(f:File) | f.filename] AS target_files
        LIMIT 10000
        """
        yes_results = session.run(yes_query).data()
        
        print("Fetching NO samples (Issues that were closed without PRs)...")
        # Issues that do not link to a PR (e.g. questions, wontfix, invalid)
        no_query = """
        MATCH (i:Issue)
        WHERE NOT (i)-[:RESOLVED_BY]->(:PR) AND i.embedding IS NOT NULL
        RETURN i.number AS issue_number, i.title AS title, i.body_truncated AS body
        LIMIT 10000
        """
        no_results = session.run(no_query).data()

    # Filter out empty targets from YES results
    valid_yes = [r for r in yes_results if len(r.get('target_files', [])) > 0]
    
    # Randomly shuffle and split
    random.shuffle(valid_yes)
    random.shuffle(no_results)
    
    sampled_yes = valid_yes[:NUM_YES]
    sampled_no = no_results[:NUM_NO]
    
    return sampled_yes, sampled_no

def fetch_graphrag_context(driver, issue_number):
    """
    Given an issue number, performs a GraphRAG vector search to find:
    Historically similar issues -> Their PRs -> The files touched.
    """
    with driver.session() as session:
        # 1. Fetch current issue embedding
        embed_query = "MATCH (i:Issue {number: $num}) RETURN i.embedding AS emb"
        result = session.run(embed_query, num=issue_number).single()
        if not result or not result["emb"]:
            return [], score_retrieval_confidence([], {})
            
        emb = result["emb"]
        
        # 2. Vector search similar historical issues (excluding itself)
        # Then traverse to see what files were touched to fix them!
        rag_query = """
        CALL db.index.vector.queryNodes('issue_embeddings', 8, $emb)
        YIELD node AS sim_issue, score
        WHERE sim_issue.number <> $num

        MATCH (sim_issue)-[:RESOLVED_BY]->(p:PR)-[:TOUCHES]->(f:File)
        RETURN
            sim_issue.number AS sim_issue_number,
            score AS sim_score,
            f.filename AS candidate_file
        """

        rag_rows = session.run(rag_query, emb=emb, num=issue_number).data()
        if not rag_rows:
            return [], score_retrieval_confidence([], {})

        file_weights = {}
        sim_scores = []
        seen_sim = set()
        for row in rag_rows:
            score = float(row.get("sim_score") or 0.0)
            sim_num = row.get("sim_issue_number")
            candidate = row.get("candidate_file")
            if sim_num not in seen_sim:
                sim_scores.append(score)
                seen_sim.add(sim_num)
            if candidate:
                file_weights[candidate] = file_weights.get(candidate, 0.0) + score

        top_files = sorted(file_weights.items(), key=lambda kv: kv[1], reverse=True)[:GRAPH_TOP_K]
        candidates = [f for f, _ in top_files]
        conf = score_retrieval_confidence(sim_scores, dict(top_files))
        return candidates, conf

def build_dataset():
    driver = GraphDatabase.driver(URI, auth=AUTH)
    treesitter_index = load_treesitter_index()
    
    sampled_yes, sampled_no = get_planner_training_data(driver)
    
    output_file = os.path.join(os.path.dirname(__file__), "planner_train_graphrag.jsonl")
    
    print(f"Building {len(sampled_yes) + len(sampled_no)} training records. This requires Vector Searches...")
    
    stats = {
        "total": 0,
        "yes": 0,
        "no": 0,
        "yes_graph_nonempty": 0,
        "yes_graph_none": 0,
        "no_graph_nonempty": 0,
        "no_graph_none": 0,
        "ts_nonempty": 0,
        "ts_none": 0,
        "graph_candidate_counts": [],
        "ts_line_counts": [],
        "prompt_tokens_est": [],
        "target_tokens_est": [],
        "target_missing_required": 0,
        "target_has_confidence": 0,
        "yes_missing_plan_fields": 0,
    }

    with open(output_file, 'w', encoding='utf-8') as f:
        # Process YES samples
        for row in tqdm(sampled_yes, desc="Processing YES samples"):
            candidates, conf = fetch_graphrag_context(driver, row["issue_number"])
            ts_context = format_treesitter_context(
                treesitter_index, candidate_files=candidates, gt_files=row.get("target_files", [])
            )
            collect_coverage_stats(stats, is_yes=True, candidates=candidates, ts_context=ts_context)
            
            # Format the LLM Input String (Context + Issue)
            prompt = (
                "--- GRAPH RETRIEVAL CONTEXT ---\n"
                "Historically similar tickets touched these candidate files:\n"
                f"{extract_graph_candidate_block(candidates)}\n\n"
                "--- RETRIEVAL EVIDENCE ---\n"
                f"retrieval_confidence: {conf['bucket']} ({conf['raw']})\n"
                f"sim_top1: {conf['sim_top1']} | sim_top3_mean: {conf['sim_top3_mean']}\n"
                f"top1_weight: {conf['top1_weight']} | top2_weight: {conf['top2_weight']} | margin_norm: {conf['margin_norm']}\n"
                f"entropy_norm: {conf['entropy_norm']} | candidate_count: {conf['candidate_count']}\n\n"
                "--- REPO STRUCTURE CONTEXT (TREE-SITTER) ---\n"
                f"{ts_context}\n\n"
            )
            prompt += f"--- CURRENT TICKET ---\nTitle: {row['title']}\nBody: {row['body']}\n\nTask: Output a patch plan consisting of YES/NO for Requires Code Change, followed by target files."
            
            pr_bodies = [body for body in row.get('pr_bodies', []) if body]
            # Keep target concise and intent-focused: clean + first 180 words from PR body.
            what_to_change_text = (
                "To fix this issue: " + first_n_words(clean_pr_body_for_plan(pr_bodies[0]), 180)
                if pr_bodies
                else "Implement fix according to requested feature."
            )
            file_str = "\n  - ".join(set(row["target_files"]))
            
            # Target output (Structured plan)
            target = (
                "REQUIRES_CODE_CHANGE: YES\n"
                "REASON: Identified issue requiring logic update.\n\n"
                "PLAN:\n"
                f"- What to change: {what_to_change_text}\n"
                f"- Target files:\n  - {file_str}\n"
                "- Test strategy: Update or add unit tests for the modified functions."
            )
            collect_quality_stats(stats, prompt=prompt, target=target, is_yes=True)
            
            f.write(json.dumps({"input": prompt, "output": target, "text": to_chatml(prompt, target)}) + "\n")
            
        # Process NO samples
        for row in tqdm(sampled_no, desc="Processing NO samples"):
            candidates, conf = fetch_graphrag_context(driver, row["issue_number"])
            ts_context = format_treesitter_context(treesitter_index, candidate_files=candidates)
            collect_coverage_stats(stats, is_yes=False, candidates=candidates, ts_context=ts_context)
            
            prompt = (
                "--- GRAPH RETRIEVAL CONTEXT ---\n"
                "Historically similar tickets touched these candidate files:\n"
                f"{extract_graph_candidate_block(candidates)}\n\n"
                "--- RETRIEVAL EVIDENCE ---\n"
                f"retrieval_confidence: {conf['bucket']} ({conf['raw']})\n"
                f"sim_top1: {conf['sim_top1']} | sim_top3_mean: {conf['sim_top3_mean']}\n"
                f"top1_weight: {conf['top1_weight']} | top2_weight: {conf['top2_weight']} | margin_norm: {conf['margin_norm']}\n"
                f"entropy_norm: {conf['entropy_norm']} | candidate_count: {conf['candidate_count']}\n\n"
                "--- REPO STRUCTURE CONTEXT (TREE-SITTER) ---\n"
                f"{ts_context}\n\n"
            )
            prompt += f"--- CURRENT TICKET ---\nTitle: {row['title']}\nBody: {row['body']}\n\nTask: Output a patch plan consisting of YES/NO for Requires Code Change, followed by target files."

            no_reason = synthesize_no_reason(row.get("title"), row.get("body")).replace("\n", " ").strip()
            target = f"REQUIRES_CODE_CHANGE: NO\nREASON: {no_reason}"
            collect_quality_stats(stats, prompt=prompt, target=target, is_yes=False)
            
            f.write(json.dumps({"input": prompt, "output": target, "text": to_chatml(prompt, target)}) + "\n")

    driver.close()
    print(f"File completely generated at {output_file}!")
    print_coverage_stats(stats, output_file)

if __name__ == "__main__":
    build_dataset()
