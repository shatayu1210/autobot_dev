"""Weekly DPO batch runner utilities for the slackbot package."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import requests

from dpo_feedback import database_url, ensure_schema


@dataclass
class WeeklyResult:
    run_id: str
    week_start: str
    week_end: str
    loaded_rows: int
    labeled_rows: int
    exported_rows: int
    jsonl_path: str
    new_revision: str
    validation_ok: bool
    errors: list[str]
    progress: list[str]


TEACHER_SYSTEM_PROMPT = """You are an expert teacher model labeling data for DPO.
Given prompt + rejected + metadata, produce a better chosen response.

Rules:
- Keep grounded to input and metadata.
- Improve logical quality vs rejected output.
- Keep style/length similar to rejected (roughly +/-20%).
- Output format:
CHOSEN_REASONING:
<text>
"""


def _connect() -> psycopg.Connection:
    url = database_url()
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    return psycopg.connect(url)


def week_bounds() -> tuple[str, str]:
    ws = os.getenv("DPO_WEEK_START", "").strip()
    we = os.getenv("DPO_WEEK_END", "").strip()
    if ws and we:
        return ws, we
    today = date.today()
    start = today - timedelta(days=7)
    return start.isoformat(), today.isoformat()


def load_feedback_rows(week_start: str, week_end: str) -> list[dict[str, Any]]:
    q = """
    SELECT fe.id, fe.prompt_text, fe.rejected_text, fe.metadata
    FROM feedback_events fe
    WHERE fe.created_at::date >= %s::date
      AND fe.created_at::date <= %s::date
      AND NOT EXISTS (
        SELECT 1 FROM teacher_labels tl
        WHERE tl.feedback_id = fe.id AND tl.qc_status = 'accepted'
      )
    ORDER BY fe.id
    """
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(q, (week_start, week_end))
            rows = cur.fetchall()
    out = []
    for rid, prompt, rejected, metadata in rows:
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except json.JSONDecodeError:
                metadata = {}
        out.append(
            {
                "id": int(rid),
                "prompt_text": str(prompt or ""),
                "rejected_text": str(rejected or ""),
                "metadata": metadata or {},
            }
        )
    return out


def _openai_chat_completion(messages: list[dict[str, str]], *, model: str) -> str:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set")
    base = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    body = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 1024,
    }
    r = requests.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=body,
        timeout=120,
    )
    r.raise_for_status()
    data = r.json()
    return data["choices"][0]["message"]["content"]


def _extract_chosen(raw: str) -> str:
    marker = "CHOSEN_REASONING:"
    if marker in raw:
        return raw.split(marker, 1)[1].strip()
    return raw.strip()


def _qc_choice(chosen: str, rejected: str) -> tuple[bool, str]:
    if len(chosen.strip()) < 8:
        return False, "too_short"
    if chosen.strip() == rejected.strip():
        return False, "identical"
    return True, "ok"


def teacher_label(rows: list[dict[str, Any]], run_id: str) -> tuple[int, list[str]]:
    model = os.getenv("DPO_TEACHER_MODEL", "gpt-4o")
    errors: list[str] = []
    labeled = 0
    for row in rows:
        payload = {
            "prompt": row["prompt_text"],
            "rejected": row["rejected_text"],
            "metadata": row["metadata"],
        }
        try:
            raw = _openai_chat_completion(
                [
                    {"role": "system", "content": TEACHER_SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                model=model,
            )
            chosen = _extract_chosen(raw)
            ok, _reason = _qc_choice(chosen, row["rejected_text"])
            qc = "accepted" if ok else "rejected"
            with _connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM teacher_labels WHERE feedback_id = %s AND qc_status <> 'accepted'", (row["id"],))
                    cur.execute(
                        """
                        INSERT INTO teacher_labels (feedback_id, chosen_text, teacher_model, teacher_run_id, qc_status)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (row["id"], chosen, model, run_id, qc),
                    )
                conn.commit()
            labeled += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(f"teacher_label feedback_id={row['id']}: {exc}")
    return labeled, errors


def export_jsonl(week_start: str, week_end: str, cumulative: bool, run_id: str) -> tuple[str, int]:
    if cumulative:
        q = """
        SELECT fe.prompt_text, tl.chosen_text, fe.rejected_text
        FROM feedback_events fe
        JOIN teacher_labels tl ON tl.feedback_id = fe.id
        WHERE tl.qc_status='accepted'
          AND tl.labeled_at::date <= %s::date
        ORDER BY tl.labeled_at, fe.id
        """
        params: tuple[Any, ...] = (week_end,)
    else:
        q = """
        SELECT fe.prompt_text, tl.chosen_text, fe.rejected_text
        FROM feedback_events fe
        JOIN teacher_labels tl ON tl.feedback_id = fe.id
        WHERE tl.qc_status='accepted'
          AND tl.labeled_at::date >= %s::date
          AND tl.labeled_at::date <= %s::date
        ORDER BY tl.labeled_at, fe.id
        """
        params = (week_start, week_end)
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(q, params)
            rows = cur.fetchall()

    out_dir = Path(os.getenv("DPO_JSONL_DIR", "/tmp/dpo_jsonl"))
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{run_id}.jsonl"
    with path.open("w", encoding="utf-8") as fp:
        for prompt, chosen, rejected in rows:
            fp.write(
                json.dumps(
                    {"prompt": str(prompt or ""), "chosen": str(chosen or ""), "rejected": str(rejected or "")},
                    ensure_ascii=False,
                )
                + "\n"
            )
    return str(path), len(rows)


def record_training_run(run_id: str, week_start: str, week_end: str, artifact_uri: str, row_count: int, status: str) -> None:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO training_runs (run_id, week_start, week_end, status, artifact_uri, row_count)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (run_id, week_start, week_end, status, artifact_uri, row_count),
            )
        conn.commit()


def run_weekly(*, skip_teacher: bool, skip_train: bool, skip_deploy: bool, cumulative: bool) -> WeeklyResult:
    ensure_schema()
    week_start, week_end = week_bounds()
    run_id = str(uuid4())
    progress: list[str] = [f"run_id={run_id}", f"window={week_start}..{week_end}"]
    errors: list[str] = []

    rows = load_feedback_rows(week_start, week_end)
    progress.append(f"load_feedback_rows={len(rows)}")

    labeled = 0
    if skip_teacher:
        progress.append("teacher=skipped")
    else:
        labeled, label_errors = teacher_label(rows, run_id)
        progress.append(f"teacher_labeled={labeled}")
        errors.extend(label_errors)

    jsonl_path, exported = export_jsonl(week_start, week_end, cumulative, run_id)
    progress.append(f"jsonl_exported={exported} path={jsonl_path}")
    record_training_run(run_id, week_start, week_end, jsonl_path, exported, "exported")

    revision = os.getenv("DPO_NEW_REVISION", "").strip()
    if skip_train:
        progress.append("train=skipped")
    else:
        webhook = os.getenv("DPO_TRAIN_WEBHOOK_URL", "").strip()
        if webhook:
            try:
                body = {
                    "run_id": run_id,
                    "jsonl_path": jsonl_path,
                    "week_start": week_start,
                    "week_end": week_end,
                    "hub_repo_id": os.getenv("DPO_HUB_REPO_ID", ""),
                }
                resp = requests.post(webhook, json=body, timeout=300)
                resp.raise_for_status()
                payload = resp.json() if resp.content else {}
                revision = str(payload.get("revision", revision)).strip()
                progress.append(f"train_webhook_revision={revision or '<none>'}")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"train_webhook: {exc}")
        else:
            progress.append("train=no webhook configured")

    validation_ok = True
    if skip_deploy:
        progress.append("deploy=skipped")
    else:
        progress.append("deploy=not_implemented_in_slackbot_runner")
        validation_ok = False
        errors.append("deploy step not implemented in slackbot runner yet")

    return WeeklyResult(
        run_id=run_id,
        week_start=week_start,
        week_end=week_end,
        loaded_rows=len(rows),
        labeled_rows=labeled,
        exported_rows=exported,
        jsonl_path=jsonl_path,
        new_revision=revision,
        validation_ok=validation_ok,
        errors=errors,
        progress=progress,
    )
