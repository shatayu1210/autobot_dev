import requests
from config import HF_TOKEN, REASONER_ENDPOINT

HEADERS = {
    "Authorization": f"Bearer {HF_TOKEN}",
    "Content-Type": "application/json"
}

def build_prompt(title: str, body: str) -> str:
    body_truncated = body[:2000] if body else "No description provided."
    return f"""You are an expert Apache Airflow engineer analyzing a high severity GitHub issue.

Issue Title: {title}
Issue Body: {body_truncated}

Provide a structured analysis in exactly this format:
SUMMARY: <one sentence describing the issue>
ROOT_CAUSE: <one sentence explaining why this is happening>
SUGGESTED_ACTION: <one sentence recommending what to do>

Analysis:"""


def parse_analysis(generated_text: str) -> dict:
    result = {
        "summary": "",
        "root_cause": "",
        "suggested_action": ""
    }
    for line in generated_text.strip().split("\n"):
        line = line.strip()
        if line.upper().startswith("SUMMARY:"):
            result["summary"] = line.split(":", 1)[1].strip()
        elif line.upper().startswith("ROOT_CAUSE:"):
            result["root_cause"] = line.split(":", 1)[1].strip()
        elif line.upper().startswith("SUGGESTED_ACTION:"):
            result["suggested_action"] = line.split(":", 1)[1].strip()

    if not result["summary"] and generated_text.strip():
        result["summary"] = generated_text.strip()[:300]
        result["root_cause"] = "Could not parse structured format."
        result["suggested_action"] = "Review manually."

    return result


def analyze_issue(issue: dict) -> dict:
    prompt = build_prompt(issue["title"], issue["body"])

    payload = {
        "inputs": prompt,
        "parameters": {
            "max_new_tokens": 200,
            "temperature": 0.3,
            "return_full_text": False
        }
    }

    response = requests.post(
        REASONER_ENDPOINT,
        headers=HEADERS,
        json=payload,
        timeout=120  # 7B model needs more time
    )

    if response.status_code != 200:
        raise Exception(f"Endpoint error: {response.status_code} — {response.text[:200]}")

    result   = response.json()
    generated = result[0]["generated_text"]
    analysis  = parse_analysis(generated)

    return {
        **issue,
        "analysis":      analysis,
        "raw_reasoning": generated.strip()
    }


def run_reasoner(high_issues: list[dict]) -> list[dict]:
    if not high_issues:
        print("Reasoner: no HIGH issues to analyze.")
        return []

    print(f"\nReasoner analyzing {len(high_issues)} HIGH severity issues via HF Endpoint...")
    print("-" * 50)

    analyzed = []

    for i, issue in enumerate(high_issues):
        num   = issue["issue_number"]
        title = issue["title"][:60]
        print(f"  [{i+1}/{len(high_issues)}] #{num}: {title}...")

        try:
            result = analyze_issue(issue)
            a      = result["analysis"]
            print(f"    SUMMARY:    {a['summary'][:80]}")
            print(f"    ROOT CAUSE: {a['root_cause'][:80]}")
            print(f"    ACTION:     {a['suggested_action'][:80]}")
            analyzed.append(result)

        except Exception as e:
            print(f"    ❌ Reasoner failed for #{num}: {e}")
            analyzed.append({
                **issue,
                "analysis": {
                    "summary":          issue["title"],
                    "root_cause":       "Analysis failed.",
                    "suggested_action": "Review manually."
                },
                "raw_reasoning": ""
            })

    print("-" * 50)
    print(f"Reasoner done: {len(analyzed)} issues analyzed")
    return analyzed


if __name__ == "__main__":
    test_high_issues = [
        {
            "issue_number": 99901,
            "title": "Scheduler crashes silently when DAG has 500+ task instances",
            "body": "When a DAG runs with more than 500 task instances the scheduler drops tasks without any error logs.",
            "url": "https://github.com/apache/airflow/issues/99901",
            "created_at": "2026-05-04T10:00:00Z",
            "labels": ["bug", "scheduler"],
            "predicted_class": "high",
            "is_high_severity": True,
            "raw_output": "high"
        }
    ]

    analyzed = run_reasoner(test_high_issues)
    print(f"\n{'='*50}")
    for issue in analyzed:
        print(f"#{issue['issue_number']}: {issue['title'][:60]}")
        print(f"  Summary:    {issue['analysis']['summary'][:100]}")
        print(f"  Root Cause: {issue['analysis']['root_cause'][:100]}")
        print(f"  Action:     {issue['analysis']['suggested_action'][:100]}")
    print("✅ Phase 7 Reasoner test complete")
