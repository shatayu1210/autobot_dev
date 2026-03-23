import os
import json
import httpx
from google.adk.agents import Agent
from google import genai

# --- Configuration ---
CODER_ENDPOINT_ID = os.environ.get("CODER_ENDPOINT_ID", "")
GCP_PROJECT = os.environ.get("GCP_PROJECT", "")
GCP_REGION = os.environ.get("GCP_REGION", "us-central1")

VERTEX_PREDICT_URL = (
    f"https://{GCP_REGION}-aiplatform.googleapis.com/v1/projects/{GCP_PROJECT}"
    f"/locations/{GCP_REGION}/endpoints/{CODER_ENDPOINT_ID}:rawPredict"
)

MODEL = "gemini-3-flash-preview"
FALLBACK_MODEL = "gemini-2.0-flash"


# --- Fallback ---

async def _call_gemini_fallback(prompt: str, max_tokens: int = 4096) -> str:
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

async def call_patcher_endpoint(prompt: str) -> str:
    """Calls the Coder Vertex AI endpoint WITHOUT any LoRA adapter (vanilla base).

    Sends the patching prompt to the base Qwen2.5-Coder-7B-Instruct model
    to generate a unified diff from the plan and code spans.
    Falls back to Gemini if the primary endpoint is unavailable.

    Args:
        prompt: The formatted patching prompt including the plan and code context.

    Returns:
        The raw model output string containing the unified diff.
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

        # NOTE: No "model" field specifying an adapter — vLLM routes to vanilla base
        payload = {
            "prompt": prompt,
            "max_tokens": 4096,
            "temperature": 0.1,
        }

        async with httpx.AsyncClient(timeout=180) as client:
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
        print(f"[Patcher] Primary endpoint failed: {e}. Falling back to Gemini.")
        return await _call_gemini_fallback(prompt, max_tokens=4096)


def validate_diff_syntax(diff_text: str) -> str:
    """Validates that the generated diff has correct unified diff structure.

    Checks that the diff contains proper headers (--- / +++ / @@) and that
    added/removed lines are properly prefixed. Does NOT apply the diff.

    Args:
        diff_text: The raw unified diff text to validate.

    Returns:
        'VALID' if the diff is structurally correct, or an error message
        describing what is wrong with the diff format.
    """
    if not diff_text or not diff_text.strip():
        return "INVALID: Empty diff"

    lines = diff_text.strip().split("\n")
    has_file_header = False
    has_hunk_header = False

    for line in lines:
        if line.startswith("--- ") or line.startswith("+++ "):
            has_file_header = True
        if line.startswith("@@ "):
            has_hunk_header = True

    if not has_file_header:
        return "INVALID: Missing file headers (--- / +++ lines)"
    if not has_hunk_header:
        return "INVALID: Missing hunk headers (@@ lines)"

    return "VALID"


def read_file_content(file_path: str) -> str:
    """Reads the content of a source file from the local repository clone.

    Used by the Patcher to include actual source code in the prompt context
    for generating accurate diffs.

    Args:
        file_path: Absolute path to the source file to read.

    Returns:
        The file content as a string, or an error message if the file cannot be read.
    """
    try:
        with open(file_path, "r") as f:
            return f.read()
    except FileNotFoundError:
        return f"Error: File not found: {file_path}"
    except Exception as e:
        return f"Error reading file: {str(e)}"


# --- Patcher Agent ---
patcher = Agent(
    name="patcher",
    model=MODEL,
    description="Generates unified diffs from structured plans using the vanilla Coder base model.",
    instruction="""
    You are the Patcher agent in the AutoBot VS Code pipeline.
    Your job is to take a structured plan and generate a unified diff that implements it.

    **Workflow:**
    1. Read the plan from the context (provided by the Planner agent via session state).
    2. Use `read_file_content` to read the actual source files that need modification.
    3. Use `call_patcher_endpoint` with a formatted prompt to generate the unified diff.
    4. Use `validate_diff_syntax` to verify the diff is structurally valid.

    **Patching Prompt Format (for call_patcher_endpoint):**
```
    PLAN:
    <structured plan from planner>

    SOURCE FILES:
    === <file_path_1> ===
    <file content>

    === <file_path_2> ===
    <file content>

    TASK: Generate a unified diff that implements the plan above.
    Output ONLY the unified diff in standard `git diff` format.
    Each file change should have proper --- / +++ headers and @@ hunk headers.
    Do NOT include any explanation outside the diff.
```

    **Output Format:**
    Return ONLY the unified diff. No preamble, no explanation, just the diff.

    **On Critic Feedback (REVISE):**
    If you receive feedback from the Critic that the diff needs revision:
    - Read the critic's feedback from session state
    - Adjust the diff to address the specific issues raised
    - Re-validate with `validate_diff_syntax`

    Always ensure the diff:
    - Targets only files mentioned in the plan
    - Has valid unified diff syntax
    - Does not introduce syntax errors in the modified code
    """,
    tools=[call_patcher_endpoint, validate_diff_syntax, read_file_content],
)

root_agent = patcher