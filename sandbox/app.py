"""
Sandbox HTTP Server — Patcher Validation

Accepts a unified diff + list of test files, applies the diff to the
read-only Airflow repo in a temporary working copy, runs pytest, and
returns the result.

POST /run_tests
  Body: { "diff": "<unified diff string>", "test_files": ["tests/foo/test_bar.py"] }
  Response: { "passed": bool, "returncode": int, "output": "...", "duration_s": float }

POST /health
  Response: { "status": "ok" }
"""

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

app = FastAPI(title="AutoBot Patcher Sandbox", version="1.0.0")

WORKSPACE = Path(os.environ.get("AIRFLOW_REPO_PATH", "/workspace"))
PYTEST_TIMEOUT = int(os.environ.get("PYTEST_TIMEOUT", "120"))


class RunTestsRequest(BaseModel):
    diff: str
    test_files: list[str] = []


@app.get("/health")
def health():
    return {"status": "ok", "workspace": str(WORKSPACE), "exists": WORKSPACE.is_dir()}


@app.post("/run_tests")
def run_tests(req: RunTestsRequest):
    """
    1. Copy the workspace to a writable tmpdir.
    2. Apply the diff with `patch -p1`.
    3. Run pytest on the specified test files.
    4. Return result and clean up.
    """
    start = time.time()

    if not WORKSPACE.is_dir():
        return JSONResponse(
            {"error": f"Workspace not found: {WORKSPACE}"}, status_code=500
        )

    # Create a writable scratch copy of the repo
    tmpdir = tempfile.mkdtemp(prefix="autobot_sandbox_")
    try:
        scratch = Path(tmpdir) / "repo"
        shutil.copytree(str(WORKSPACE), str(scratch), symlinks=True)

        # Write the diff to a temp file
        diff_file = Path(tmpdir) / "patch.diff"
        diff_file.write_text(req.diff, encoding="utf-8")

        # Apply the diff
        patch_result = subprocess.run(
            ["patch", "-p1", "--input", str(diff_file)],
            cwd=str(scratch),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if patch_result.returncode != 0:
            return {
                "passed": False,
                "returncode": patch_result.returncode,
                "output": f"PATCH FAILED:\n{patch_result.stdout}\n{patch_result.stderr}",
                "duration_s": round(time.time() - start, 2),
            }

        # Run pytest
        test_targets = req.test_files if req.test_files else ["tests/"]
        pytest_result = subprocess.run(
            ["python", "-m", "pytest", "--timeout", str(PYTEST_TIMEOUT), "-x", "--tb=short", "-q"]
            + test_targets,
            cwd=str(scratch),
            capture_output=True,
            text=True,
            timeout=PYTEST_TIMEOUT + 30,
            env={**os.environ, "PYTHONPATH": str(scratch)},
        )

        combined_output = pytest_result.stdout + pytest_result.stderr
        return {
            "passed": pytest_result.returncode == 0,
            "returncode": pytest_result.returncode,
            "output": combined_output[:8000],  # cap at 8k chars
            "duration_s": round(time.time() - start, 2),
        }

    except subprocess.TimeoutExpired:
        return {
            "passed": False,
            "returncode": -1,
            "output": "Timeout: test run exceeded limit.",
            "duration_s": round(time.time() - start, 2),
        }
    except Exception as e:
        return {
            "passed": False,
            "returncode": -1,
            "output": f"Sandbox error: {e}",
            "duration_s": round(time.time() - start, 2),
        }
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
