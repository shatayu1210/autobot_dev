import json
import os
import re

from .state import PipelineState
from .tools import fetch_github_issue, call_llm, validate_diff


def parse_input_node(state: PipelineState) -> dict:
    """Parse issue_ref string to extract owner, repo, and issue_number."""
    issue_ref = state.get("issue_ref", "")

    owner = ""
    repo = ""
    issue_number = 0

    try:
        match = re.search(r"([\w][\w.\-]+)/([\w][\w.\-]+)[^\d]+(\d+)", issue_ref)
        if match:
            owner = match.group(1)
            repo = match.group(2)
            issue_number = int(match.group(3))
    except Exception as e:
        pass

    return {
        "owner": owner,
        "repo": repo,
        "issue_number": issue_number,
        "iteration": 0,
        "max_iterations": 3,
        "verdict": "",
        "feedback": "",
        "patch_plan": "",
        "patch_diff": "",
        "final_result": "",
        "issue_data": {},
        "progress": [f"Starting pipeline for: {issue_ref}"],
    }


def planner_node(state: PipelineState) -> dict:
    """Fetch the GitHub issue and generate a structured patch plan."""
    owner = state["owner"]
    repo = state["repo"]
    issue_number = state["issue_number"]

    try:
        github_token = os.getenv("GITHUB_TOKEN", "")
        data = fetch_github_issue(owner, repo, issue_number, github_token)
    except Exception as e:
        return {
            "issue_data": {},
            "patch_plan": f"ERROR fetching issue: {e}",
            "progress": [f"Planner: ERROR fetching issue #{issue_number} from {owner}/{repo}: {e}"],
        }

    title = data.get("title", "")
    body = data.get("body", "")
    labels = ", ".join(data.get("labels", [])) or "none"
    comments_text = ""
    if data.get("comments"):
        comments_text = "\n\nCOMMENTS:\n" + "\n---\n".join(data["comments"][:5])

    prompt = f"""You are a code Planner. Analyze this GitHub issue and produce a structured patch plan.

ISSUE_TITLE: {title}
ISSUE_BODY: {body}
ISSUE_LABELS: {labels}{comments_text}

TASK: Generate a structured patch plan. For each file that needs changes:
- File path
- Function/class to modify
- Description of the change
- ADD / MODIFY / DELETE operation

Start with REQUIRES_CODE_CHANGE: YES or NO.
If YES, list the numbered file changes."""

    try:
        plan_text = call_llm(prompt, temperature=0.2, max_tokens=2048)
    except Exception as e:
        plan_text = f"ERROR generating plan: {e}"

    return {
        "issue_data": data,
        "patch_plan": plan_text,
        "progress": [f"Planner: analyzed issue #{issue_number} ({title[:60]}), created patch plan"],
    }


def patcher_node(state: PipelineState) -> dict:
    """Generate a unified diff implementing the patch plan."""
    patch_plan = state.get("patch_plan", "")
    feedback = state.get("feedback", "")
    iteration = state.get("iteration", 0)

    feedback_section = ""
    if feedback:
        feedback_section = f"\nCRITIC FEEDBACK TO ADDRESS:\n{feedback}\n"

    prompt = f"""You are a code Patcher. Generate a unified diff implementing this plan.

PLAN:
{patch_plan}
{feedback_section}
TASK: Output ONLY a unified diff in standard git diff format.
Each file must have proper --- / +++ headers and @@ hunk headers.
Output the diff and nothing else."""

    try:
        raw_diff = call_llm(prompt, temperature=0.2, max_tokens=4096)
    except Exception as e:
        return {
            "patch_diff": f"ERROR: {e}",
            "progress": [f"Patcher (iter {iteration}): ERROR generating diff: {e}"],
        }

    # Strip markdown code fences if the model wrapped the diff
    diff_text = raw_diff.strip()
    if diff_text.startswith("```"):
        lines = diff_text.splitlines()
        # Drop first line (```diff or ```) and last closing ```
        inner_lines = []
        skip_first = True
        for line in lines:
            if skip_first:
                skip_first = False
                continue
            if line.strip() == "```":
                break
            inner_lines.append(line)
        diff_text = "\n".join(inner_lines)

    validation = validate_diff(diff_text)
    if validation != "VALID":
        progress_msg = f"Patcher (iter {iteration}): generated diff (validation: {validation})"
    else:
        progress_msg = f"Patcher (iter {iteration}): generated valid unified diff"

    return {
        "patch_diff": diff_text,
        "progress": [progress_msg],
    }


def critic_node(state: PipelineState) -> dict:
    """Evaluate the diff and return ACCEPT / REVISE / REJECT verdict."""
    issue_data = state.get("issue_data", {})
    patch_plan = state.get("patch_plan", "")
    patch_diff = state.get("patch_diff", "")
    iteration = state.get("iteration", 0)

    title = issue_data.get("title", "")
    body = issue_data.get("body", "")

    prompt = f"""You are a code Critic. Evaluate this diff against the original issue and plan.

ISSUE_TITLE: {title}
ISSUE_BODY: {body}

PLAN:
{patch_plan}

DIFF:
{patch_diff}

TASK: Respond with a JSON object only:
{{"verdict": "ACCEPT|REVISE|REJECT", "feedback": "your reasoning"}}

ACCEPT: diff correctly implements the plan, ready for review.
REVISE: diff has fixable issues (list them in feedback).
REJECT: fundamentally wrong approach."""

    try:
        raw_response = call_llm(prompt, temperature=0.1, max_tokens=1024)
    except Exception as e:
        return {
            "verdict": "REVISE",
            "feedback": f"Critic call failed: {e}",
            "iteration": iteration + 1,
            "progress": [f"Critic (iter {iteration}): ERROR calling LLM, defaulting to REVISE"],
        }

    # Parse JSON from the response (strip markdown fences if present)
    response_text = raw_response.strip()
    if response_text.startswith("```"):
        lines = response_text.splitlines()
        inner_lines = []
        skip_first = True
        for line in lines:
            if skip_first:
                skip_first = False
                continue
            if line.strip() == "```":
                break
            inner_lines.append(line)
        response_text = "\n".join(inner_lines)

    verdict = "REVISE"
    feedback = ""
    try:
        parsed = json.loads(response_text)
        verdict = parsed.get("verdict", "REVISE").upper()
        feedback = parsed.get("feedback", "")
        if verdict not in ("ACCEPT", "REVISE", "REJECT"):
            verdict = "REVISE"
    except Exception:
        # Fallback: try to extract verdict with regex
        match = re.search(r'"verdict"\s*:\s*"(ACCEPT|REVISE|REJECT)"', response_text)
        if match:
            verdict = match.group(1)
        fb_match = re.search(r'"feedback"\s*:\s*"([^"]*)"', response_text)
        if fb_match:
            feedback = fb_match.group(1)
        else:
            feedback = response_text[:500]

    return {
        "verdict": verdict,
        "feedback": feedback,
        "iteration": iteration + 1,
        "progress": [f"Critic (iter {iteration}): verdict={verdict}"],
    }


def output_node(state: PipelineState) -> dict:
    """Format the final result based on verdict."""
    verdict = state.get("verdict", "")
    feedback = state.get("feedback", "")
    patch_plan = state.get("patch_plan", "")
    patch_diff = state.get("patch_diff", "")
    iteration = state.get("iteration", 0)
    max_iterations = state.get("max_iterations", 3)

    if verdict == "ACCEPT":
        result_text = (
            f"## Patch Accepted\n\n"
            f"### Plan Summary\n{patch_plan}\n\n"
            f"### Unified Diff\n```diff\n{patch_diff}\n```\n\n"
            f"### Critic Feedback\n{feedback}"
        )
    elif verdict == "REJECT":
        result_text = (
            f"## Patch Rejected\n\n"
            f"### Critic Feedback\n{feedback}"
        )
    else:
        # Loop exhausted (max iterations reached without ACCEPT/REJECT)
        result_text = (
            f"## Patch Loop Exhausted\n\n"
            f"### Last Critic Feedback\n{feedback}\n\n"
            f"### Last Diff\n```diff\n{patch_diff}\n```"
        )

    return {"final_result": result_text}
