"""
Local mock for AutoBot orchestrator HTTP API (stdlib only — no pip install).

Run: python3 app.py
Then point the VS Code extension at http://localhost:5000 (default).

POST /api/orchestrate  JSON body: { "command": "ask_issue"|"plan_patch"|"accept_plan"|"open_pr", ... }
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse


def handle_orchestrate(body: dict) -> tuple[int, dict]:
    command = body.get("command")
    if not command:
        return 400, {"error": "missing command"}

    if command == "ask_issue":
        n = body.get("issue_number", 0)
        return 200, {
            "issue_number": n,
            "title": f"[MOCK] Issue #{n}",
            "body": "Stub issue body for extension testing.\n\n**No orchestrator** — safe to ignore.",
            "state": "open",
            "html_url": f"https://github.com/example/repo/issues/{n}",
        }

    if command == "plan_patch":
        n = body.get("issue_number")
        repo = body.get("repo_path", "")
        plan = {
            "summary": "Mock plan: adjust config and add a regression test.",
            "files": ["src/example.py", "tests/test_example.py"],
            "steps": [
                "Open src/example.py and locate the handler.",
                "Add a guard clause for the edge case.",
                "Add tests/test_example.py coverage.",
            ],
        }
        code_spans = [
            {
                "file": "src/example.py",
                "symbol": "handle_request",
                "start_line": 10,
                "end_line": 40,
            }
        ]
        return 200, {
            "issue_number": n,
            "repo_path": repo,
            "plan": plan,
            "code_spans": code_spans,
            "note": "MOCK planner — replace with Vertex-backed Planner when ready.",
        }

    if command == "accept_plan":
        plan = body.get("plan")
        diff = """diff --git a/src/example.py b/src/example.py
--- a/src/example.py
+++ b/src/example.py
@@ -12,6 +12,8 @@ def handle_request(req):
     if not req:
         return None
+    # MOCK: extension wiring test
+    assert req is not None
     return process(req)
"""
        return 200, {
            "diff": diff,
            "verdict": "ACCEPT",
            "reasoning": "MOCK critic: structure looks fine for a demo.",
            "plan_echo": plan,
            "iterations_used": 1,
        }

    if command == "open_pr":
        diff = body.get("diff", "")
        return 200, {
            "status": "ok",
            "title": "[MOCK] AutoBot draft PR",
            "body": f"Mock PR body.\n\n```diff\n{diff[:500]}{'...' if len(diff) > 500 else ''}\n```",
            "html_url": "https://github.com/example/repo/compare/mock-branch?expand=1",
            "note": "MOCK — no GitHub API call was made.",
        }

    return 400, {"error": f"unknown command: {command}"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:
        print(f"[mock] {self.address_string()} - {format % args}")

    def _send(self, code: int, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._send(200, {"status": "ok", "service": "mock_autobot_orchestrator"})
            return
        if parsed.path == "/api/orchestrate":
            self._send(
                200,
                {
                    "message": "This endpoint expects POST with JSON (same as the VS Code extension).",
                    "try": 'curl -s -X POST http://127.0.0.1:5000/api/orchestrate -H "Content-Type: application/json" -d \'{"command":"ask_issue","issue_number":124}\'',
                    "commands": [
                        "ask_issue",
                        "plan_patch",
                        "accept_plan",
                        "open_pr",
                    ],
                },
            )
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/api/orchestrate":
            self._send(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            self._send(400, {"error": "invalid JSON"})
            return
        code, out = handle_orchestrate(body)
        self._send(code, out)


def main() -> None:
    host = "127.0.0.1"
    port = 5000
    server = HTTPServer((host, port), Handler)
    print(f"Mock orchestrator: POST http://{host}:{port}/api/orchestrate  (GET shows usage)")
    print(f"Health: GET http://{host}:{port}/health")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
