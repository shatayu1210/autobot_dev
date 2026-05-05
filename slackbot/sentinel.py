import requests
from cache import mark_seen
from config import SCORER_THRESHOLD, HF_TOKEN, SCORER_ENDPOINT

import os
import openai
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "../.env"), override=True)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = openai.OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

USE_OPENAI = False

MODEL_ID = "autobot298/autobot-scorer-merged"

# ── Use Hugging Face API ──────────────────────────────
API_URL = f"https://api-inference.huggingface.co/models/{MODEL_ID}"
HEADERS = {"Authorization": f"Bearer {HF_TOKEN}"}

if USE_OPENAI:
    print("Sentinel will use OpenAI API (Testing Alternative) ✅")
else:
    print(f"Sentinel will use HF API: {MODEL_ID} ✅")

# ── Class tokens ─────────────────────────────────────────────
VALID_CLASSES = {"low", "medium", "high"}
HIGH_CLASS = "high"


def build_prompt(title: str, body: str) -> str:
    """
    Format prompt exactly as the scorer was trained on.
    Output should be one of: low | medium | high
    """
    # Truncate body to avoid token overflow on 1.5B model
    body_truncated = body[:1000] if body else "No description provided."

    return f"""You are an issue severity classifier for Apache Airflow.
Classify the following GitHub issue into one of these severity levels: low, medium, high

Issue Title: {title}
Issue Body: {body_truncated}

Output ONLY the single word (low, medium, or high). No preamble.
Severity:"""


def parse_class(generated_text: str) -> str:
    """
    Extract the predicted class from model output.
    Supports both text (low/medium/high) and classification labels (LABEL_0/1/2).
    """
    text = str(generated_text).strip().lower()

    # Map for SequenceClassification labels
    # LABEL_0=low, LABEL_1=medium, LABEL_2=high (based on training config)
    label_map = {
        "label_0": "low",
        "label_1": "medium",
        "label_2": "high"
    }
    if text in label_map:
        return label_map[text]

    # Check for exact class match
    for cls in VALID_CLASSES:
        if text.startswith(cls):
            return cls

    # Fallback — scan for any class word in output
    for cls in VALID_CLASSES:
        if cls in text:
            return cls

    # Default to low if unparseable
    print(f"  ⚠️  Could not parse class from: '{text[:50]}' — defaulting to 'low'")
    return "low"


def score_issue(issue: dict) -> dict:
    """
    Run Sentinel inference on a single issue.
    Returns issue dict enriched with score info.
    """
    prompt = build_prompt(issue["title"], issue["body"])

    if USE_OPENAI and client:
        try:
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=5
            )
            generated = resp.choices[0].message.content.strip()
        except Exception as e:
            print(f"  ⚠️  OpenAI API Error: {e}")
            generated = "low" # safe fallback
    else:
        # HTTP call to HF Space
        payload = {
            "title": issue["title"],
            "body": issue.get("body") or ""
        }

        try:
            response = requests.post(f"{SCORER_ENDPOINT}/score", headers=HEADERS, json=payload, timeout=60)
            response.raise_for_status()
            res_json = response.json()
            generated = res_json.get("label", "low")
        except Exception as e:
            print(f"  ⚠️  HF Space API Error: {e}")
            generated = "low" # safe fallback

    predicted_class = parse_class(generated)
    is_high = predicted_class == HIGH_CLASS

    return {
        **issue,
        "predicted_class": predicted_class,
        "is_high_severity": is_high,
        "raw_output": generated.strip()
    }


def run_sentinel(issues: list[dict]) -> list[dict]:
    """
    Score all new issues from poller.
    Marks ALL issues as seen (so we don't re-score them).
    Returns only HIGH severity issues for Reasoner.
    """
    if not issues:
        print("Sentinel: no issues to score.")
        return []

    print(f"\nSentinel scoring {len(issues)} issues...")
    print("-" * 50)

    high_severity = []

    for i, issue in enumerate(issues):
        num = issue["issue_number"]
        title = issue["title"][:60]

        print(f"  [{i+1}/{len(issues)}] #{num}: {title}...")

        try:
            scored = score_issue(issue)
            cls = scored["predicted_class"]
            raw = scored["raw_output"][:30]

            print(f"    → Class: {cls.upper()} | Raw: '{raw}'")

            # Mark as seen regardless of score
            mark_seen(num, issue["title"])

            if scored["is_high_severity"]:
                high_severity.append(scored)
                print(f"    ✅ HIGH severity — forwarding to Reasoner")
            else:
                print(f"    ⏭️  Skipping ({cls}) — below threshold")

        except Exception as e:
            print(f"    ❌ Scoring failed for #{num}: {e}")
            mark_seen(num, issue["title"])  # mark seen to avoid retry loop
            continue

    print("-" * 50)
    print(f"Sentinel done: {len(high_severity)} HIGH / {len(issues)} total")

    return high_severity


# ── Standalone test ──────────────────────────────────────────
if __name__ == "__main__":
    print("Running Sentinel in test mode...\n")

    test_issues = [
        {
            "issue_number": 99901,
            "title": "Scheduler crashes silently when DAG has 500+ task instances",
            "body": "When a DAG runs with more than 500 task instances, the scheduler drops tasks without any error logs. This causes silent data pipeline failures in production environments with no way to detect them.",
            "url": "https://github.com/apache/airflow/issues/99901",
            "created_at": "2026-05-02T10:00:00Z",
            "labels": ["bug", "scheduler"]
        },
        {
            "issue_number": 99902,
            "title": "Typo in documentation for BashOperator",
            "body": "There is a small typo in the docs page for BashOperator. The word 'exmaple' should be 'example'.",
            "url": "https://github.com/apache/airflow/issues/99902",
            "created_at": "2026-05-02T10:05:00Z",
            "labels": ["documentation"]
        },
        {
            "issue_number": 99903,
            "title": "XCom backend causes OOM crash on large payload in production",
            "body": "XCom backend is loading entire payload into memory before serialization. On datasets larger than 2GB this causes an OOM crash that takes down the entire worker process and all running tasks.",
            "url": "https://github.com/apache/airflow/issues/99903",
            "created_at": "2026-05-02T10:10:00Z",
            "labels": ["bug", "xcom"]
        }
    ]

    high_issues = run_sentinel(test_issues)

    print(f"\n{'='*50}")
    print(f"HIGH severity issues ready for Reasoner: {len(high_issues)}")
    for issue in high_issues:
        print(f"  → #{issue['issue_number']}: {issue['title'][:60]}")
    print("✅ Phase 2 test complete")
