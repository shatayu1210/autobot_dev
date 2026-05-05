# Patcher Orchestrator Tests

Run these tests locally to verify the deterministic conditional logic of the Patcher and Sandbox loop.

## 1. Diff Format Validation
- **Action**: Manually pass a badly formatted string (e.g., missing `+++` or wrapped in conversational text like "Here is the diff:") into the patcher validation pipeline.
- **Expected**: The linting logic instantly catches the bad format, prevents the HTTP call to the Sandbox, and immediately queues a retry appending format correction instructions.

## 2. Sandbox Happy Path
- **Action**: Pass a valid unified diff that correctly fixes a known broken test in the local Airflow repo mount.
- **Expected**: `validate_patch_in_sandbox()` posts to port `5001`. The sandbox copies the repo to a tmpfs, applies the diff via `patch -p1`, runs pytest, and returns `passed=True` within ~30 seconds.

## 3. Sandbox Syntax Error Handling
- **Action**: Pass a unified diff containing a deliberate Python `SyntaxError`.
- **Expected**: The sandbox applies the patch but pytest fails immediately with an import/syntax error. `validate_patch_in_sandbox()` returns `passed=False` and accurately captures the traceback in the `output` field.

## 4. Sandbox Timeout Handling
- **Action**: Pass a valid unified diff, but modify the Sandbox container environment `PYTEST_TIMEOUT` to `1` second. 
- **Expected**: The sandbox gracefully terminates the test run after 1 second, returning `passed=False` and a `Timeout: test run exceeded limit` output string, ensuring the orchestrator doesn't hang forever.

## 5. End-to-End Retry Loop
- **Action**: Use a stub LLM response that fails the test on attempt 1, but fixes it on attempt 2 based on the feedback.
- **Expected**: The orchestrator automatically resubmits to the Patcher LLM, successfully integrating the traceback from attempt 1.
