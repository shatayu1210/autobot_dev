import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "seen_issues.db")

def init_cache():
    """Create the SQLite cache table if it doesn't exist."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen_issues (
            issue_number INTEGER PRIMARY KEY,
            title        TEXT,
            seen_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()
    print(f"Cache initialized at {DB_PATH}")

def is_seen(issue_number: int) -> bool:
    """Return True if this issue was already processed."""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT 1 FROM seen_issues WHERE issue_number = ?",
        (issue_number,)
    ).fetchone()
    conn.close()
    return row is not None

def mark_seen(issue_number: int, title: str):
    """Mark an issue as processed."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR IGNORE INTO seen_issues (issue_number, title) VALUES (?, ?)",
        (issue_number, title)
    )
    conn.commit()
    conn.close()

def get_seen_count() -> int:
    """How many issues have we processed total."""
    conn = sqlite3.connect(DB_PATH)
    count = conn.execute("SELECT COUNT(*) FROM seen_issues").fetchone()[0]
    conn.close()
    return count
