"""Persist Slack thumbs-down feedback into Postgres for the DPO pipeline."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import psycopg
import sqlparse
from psycopg.errors import UniqueViolation

_SCHEMA_PATH = Path(__file__).resolve().parent / "schema" / "dpo_tables.sql"


def database_url() -> str | None:
    url = os.getenv("DATABASE_URL", "").strip()
    return url or None


def ensure_schema() -> None:
    """Create DPO tables if missing (idempotent)."""
    url = database_url()
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    sql = _SCHEMA_PATH.read_text()
    with psycopg.connect(url) as conn:
        with conn.cursor() as cur:
            for stmt in sqlparse.split(sql):
                s = stmt.strip()
                if s:
                    cur.execute(s)
        conn.commit()


def insert_feedback_event(
    *,
    slack_team_id: str | None,
    channel_id: str,
    message_ts: str,
    reactor_user_id: str,
    prompt_text: str,
    rejected_text: str,
    metadata: dict[str, Any],
    reaction: str = "thumbsdown",
) -> bool:
    """
    Insert one feedback row. Returns True if inserted, False if duplicate or DB disabled.
    """
    url = database_url()
    if not url:
        print("DPO: DATABASE_URL unset; skipping feedback insert")
        return False

    q = """
    INSERT INTO feedback_events (
        slack_team_id, channel_id, message_ts, user_id,
        prompt_text, rejected_text, metadata, reaction
    ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)
    """

    meta_json = json.dumps(metadata or {}, separators=(",", ":"))
    with psycopg.connect(url) as conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    q,
                    (
                        slack_team_id,
                        channel_id,
                        message_ts,
                        reactor_user_id,
                        prompt_text,
                        rejected_text,
                        meta_json,
                        reaction,
                    ),
                )
            conn.commit()
        except UniqueViolation:
            conn.rollback()
            print(f"DPO: duplicate feedback skipped channel={channel_id} ts={message_ts} user={reactor_user_id}")
            return False
    return True


def slack_message_body_text(message: dict[str, Any]) -> str:
    """Best-effort plain text from a Slack message object."""
    text = (message.get("text") or "").strip()
    if text:
        return text
    blocks = message.get("blocks")
    if not blocks:
        return ""
    chunks: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        texts = []
        elem = block.get("text") or {}
        if isinstance(elem, dict) and elem.get("text"):
            texts.append(str(elem["text"]))
        fields = elem.get("fields") if isinstance(elem, dict) else None
        if isinstance(fields, list):
            texts.extend(str(f.get("text", "")) for f in fields if isinstance(f, dict))
        for part in texts:
            if part.strip():
                chunks.append(part.strip())
    return "\n".join(chunks).strip()


def ts_sort_key(ts: str) -> float:
    try:
        return float(ts.split(".")[0])
    except (ValueError, IndexError):
        return 0.0


def resolve_prompt_and_rejected(
    messages: list[dict[str, Any]],
    *,
    reacted_ts: str,
    bot_user_id: str,
) -> tuple[str, str, dict[str, Any]]:
    """
    From a thread timeline (conversation.replies.messages), derive prompt vs rejected texts.
    Rejected is the reacted message body if authored by bot; prompt is preceding human message.
    """
    ordered = sorted(messages, key=lambda m: ts_sort_key(m.get("ts", "")))
    index = next((i for i, m in enumerate(ordered) if m.get("ts") == reacted_ts), -1)

    rejected_msg = ordered[index] if index >= 0 else {}
    rejected_text = slack_message_body_text(rejected_msg)
    rejected_user = rejected_msg.get("user")
    rejected_bot_id = rejected_msg.get("bot_id")

    is_bot = bool(rejected_bot_id) or (rejected_user and rejected_user == bot_user_id)

    prompt_text = ""
    if index > 0:
        for candidate in reversed(ordered[:index]):
            cid = candidate.get("user")
            if candidate.get("bot_id"):
                continue
            if cid and cid != bot_user_id:
                prompt_text = slack_message_body_text(candidate)
                if prompt_text:
                    break

    meta = {
        "reacted_ts": reacted_ts,
        "rejected_author_user": rejected_user,
        "rejected_from_bot_message": is_bot,
    }
    if not prompt_text.strip():
        prompt_text = "[unknown_prompt]"
    if not rejected_text.strip():
        rejected_text = "[empty_reaction_target]"
    return prompt_text, rejected_text, meta
