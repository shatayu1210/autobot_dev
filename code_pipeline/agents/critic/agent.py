import os
import json
import httpx
from typing import Literal
from google.adk.agents import Agent
from google import genai
from pydantic import BaseModel, Field

# --- Configuration ---
CODER_ENDPOINT_ID = os.environ.get("CODER_ENDPOINT_ID", "")
GCP_PROJECT = os.environ.get("GCP_PROJECT", "")
GCP_REGION = os.environ.get("GCP_REGION", "us-central1")

VERTEX_PREDICT_URL = (
    f"https://{GCP_REGION}-aiplatform.googleapis.com/v1/projects/{GCP_PROJECT}"
    f"/locations/{GCP_REGION}/endpoints/{CODER_ENDPOINT_ID}:rawPredict"
)

MODEL = "gemini-3-flash-preview"
FALLBACK_MODEL = "gemini-2.5-flash-preview-04-17"


# --- Data Models ---
class CriticVerdict(BaseModel):
    """Structured verdict from the Critic agent."""

    verdict: Literal["ACCEPT", "REVISE", "REJECT"] = Field(
        description=(
            "The critic's decision: "
            "'ACCEPT' if the diff correctly implements the plan and is ready for testing, "
            "'REVISE' if the diff has issues that the patcher should fix, "
            "'REJECT' if the diff is fundamentally wrong and planning should restart."
        )
    )
    feedback: str = Field(
        description=(
            "Detailed feedback explaining the verdict. "
            "For REVISE: specific issues to fix (e.g., missing edge case, wrong function). "
            "For REJECT: why the approach is fundamentally flawed. "
            "For ACCEPT: brief confirmation of correctness."
        )
    )


# --- Fallback ---

async def _call_gemini_fallback(prompt: str, max_tokens: int = 1024) -> str:
    """Fallback: calls Gemini when the Vertex AI vLLM endpoint is unavailable."""
    try:
        client = genai.Client()
        response = client.models.generate_content(
            model=FALLBACK_MODEL,
            contents=prompt,
            config=genai.types.GenerateContentConfig(
                max_output_tokens=max_tokens,
                temperature=0.1,
            ),
        )
        return response.text
    except Exception as fallback_err:
        return (
            f"Error: Both primary endpoint and Gemini fallback failed. "
            f"Fallback error: {str(fallback_err)}"
        )


# --- Tools ---

async def call_critic_endpoint(prompt: str) -> str:
    """Calls the Coder Vertex AI endpoint with the critic_lora adapter.

    Sends the critique prompt to the vLLM-served Qwen2.5-Coder-7B-Instruct
    model with the critic LoRA adapter for diff evaluation.
    Falls back to Gemini if the primary endpoint is unavailable.

    Args:
        prompt: The formatted critique prompt including issue, plan, and diff.

    Returns:
        The raw model output string containing the verdict and feedback.
    """
    import google.auth
    import google.auth.transport.requests

    try:
        credentials, _ = google.auth.default()
        auth_req = google.auth.transport.requests.Request()
        credentials.refresh(auth_req)

        headers = {
            "Authorization": f"Bearer {credentials.token}",
            "Content-Type": "application/json",
        }

        payload = {
            "model": "critic_lora",
            "prompt": prompt,
            "max_tokens": 1024,
            "temperature": 0.1,
        }

        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                VERTEX_PREDICT_URL,
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            result = response.json()

            choices = result.get("choices", [])
            if choices:
                return choices[0].get("text", "")
            return json.dumps(result)
    except Exception as e:
        print(f"[Critic] Primary endpoint failed: {e}. Falling back to Gemini.")
        return await _call_gemini_fallback(prompt, max_tokens=1024)


# --- Critic Agent ---
critic = Agent(
    name="critic",
    model=MODEL,
    description="Evaluates generated diffs against the plan and issue requirements.",
    instruction="""
    You are the Critic agent in the AutoBot VS Code pipeline.
    Your job is to evaluate a unified diff against the original issue and the plan.

    **Workflow:**
    1. Read the issue data, plan, and diff from session state / context.
    2. Use `call_critic_endpoint` with a formatted critique prompt.
    3. Parse the model output and return a structured verdict.

    **Critique Prompt Format (for call_critic_endpoint):**
```
    ISSUE_TITLE: <title>
    ISSUE_BODY: <body>

    PLAN:
    <structured plan>

    DIFF:
    <unified diff>

    TASK: Evaluate this diff. Your first word MUST be one of: ACCEPT, REVISE, or REJECT.
    Then provide your reasoning.

    ACCEPT: The diff correctly implements the plan, handles edge cases, and is ready for testing.
    REVISE: The diff has specific issues that can be fixed (list them).
    REJECT: The diff is fundamentally wrong — the plan itself may need to change.
```

    **Decision Criteria:**
    - ACCEPT: Diff matches plan, no syntax issues, edge cases handled, tests likely to pass.
    - REVISE: Diff is on the right track but has fixable issues (missing null checks, wrong
      variable name, incomplete implementation of one plan item, etc.).
    - REJECT: Diff is fundamentally wrong (wrong files, wrong approach, would break existing
      functionality in ways not addressed by the plan).

    **Output:**
    You MUST return a structured verdict with:
    - `verdict`: exactly one of ACCEPT, REVISE, or REJECT
    - `feedback`: detailed reasoning for your decision
    """,
    output_schema=CriticVerdict,
    tools=[call_critic_endpoint],
    # Disallow transfers as it uses output_schema
    disallow_transfer_to_parent=True,
    disallow_transfer_to_peers=True,
)

root_agent = critic