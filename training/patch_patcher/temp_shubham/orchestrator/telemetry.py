from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional


class JsonlTelemetryLogger:
    """Simple append-only structured logger for orchestrator traces."""

    def __init__(self, output_path: Optional[str]):
        self.output_path = output_path

    def enabled(self) -> bool:
        return bool(self.output_path)

    def log(self, event: Dict[str, Any]) -> None:
        if not self.output_path:
            return
        row = dict(event)
        row["ts_utc"] = datetime.now(timezone.utc).isoformat()
        with open(self.output_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
