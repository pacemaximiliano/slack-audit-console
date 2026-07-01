import sqlite3
import os
from datetime import datetime
from contextlib import contextmanager

DB_PATH = os.path.join(os.path.dirname(__file__), "database.db")


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def db_conn():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    # Step 1 — create tables (no indexes yet, so partial schemas don't fail)
    with db_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS apps (
                id            TEXT PRIMARY KEY,
                name          TEXT NOT NULL,
                description   TEXT DEFAULT '',
                discovered_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS api_calls (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                app_id    TEXT NOT NULL,
                app_name  TEXT NOT NULL,
                endpoint  TEXT,
                ts        TEXT NOT NULL,
                status    TEXT,
                raw_event TEXT
            );

            CREATE TABLE IF NOT EXISTS sync_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                synced_at  TEXT NOT NULL,
                records_in INTEGER DEFAULT 0,
                success    INTEGER DEFAULT 1,
                message    TEXT
            );
        """)

    # Step 2 — migrate: add audit_id column if missing (idempotent)
    with db_conn() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(api_calls)")}
        if "audit_id" not in cols:
            conn.execute("ALTER TABLE api_calls ADD COLUMN audit_id TEXT")

    # Step 3 — create indexes now that all columns are guaranteed to exist
    with db_conn() as conn:
        conn.executescript("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_audit_id
                ON api_calls(audit_id) WHERE audit_id IS NOT NULL;

            CREATE INDEX IF NOT EXISTS idx_ts     ON api_calls(ts);
            CREATE INDEX IF NOT EXISTS idx_app_id ON api_calls(app_id);
        """)


# ── apps table ────────────────────────────────────────────────────────────────

def upsert_app(app_id: str, name: str, description: str = ""):
    with db_conn() as conn:
        conn.execute(
            """INSERT INTO apps (id, name, description, discovered_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET name=excluded.name,
                                             description=excluded.description,
                                             discovered_at=excluded.discovered_at""",
            (app_id, name, description, datetime.utcnow().isoformat()),
        )


def get_all_apps() -> list[dict]:
    with db_conn() as conn:
        rows = conn.execute("SELECT id, name, description FROM apps ORDER BY name").fetchall()
    return [dict(r) for r in rows]


# ── api_calls ─────────────────────────────────────────────────────────────────

def insert_api_call(app_id: str, app_name: str, endpoint: str,
                    ts: str, status: str,
                    raw_event: str = None, audit_id: str = None) -> bool:
    """Returns True if a new row was inserted, False if it was a duplicate."""
    with db_conn() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO api_calls
               (audit_id, app_id, app_name, endpoint, ts, status, raw_event)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (audit_id, app_id, app_name, endpoint, ts, status, raw_event),
        )
        return conn.execute("SELECT changes()").fetchone()[0] > 0


def query_api_calls(start_ts: str = None, end_ts: str = None,
                    app_ids: list = None):
    """app_ids: list of app IDs to include. None or empty = all apps."""
    clauses, params = [], []
    if start_ts:
        clauses.append("ts >= ?"); params.append(start_ts)
    if end_ts:
        clauses.append("ts <= ?"); params.append(end_ts)
    if app_ids:
        placeholders = ",".join("?" * len(app_ids))
        clauses.append(f"app_id IN ({placeholders})")
        params.extend(app_ids)

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = f"SELECT * FROM api_calls {where} ORDER BY ts DESC"

    with db_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_metrics(start_ts: str = None, end_ts: str = None, app_ids: list = None):
    rows = query_api_calls(start_ts, end_ts, app_ids)
    total = len(rows)
    success = sum(1 for r in rows if str(r.get("status") or "").startswith("2"))
    success_rate = round(success / total * 100, 1) if total else 0

    from collections import Counter
    counts = Counter(r["app_name"] for r in rows)
    top_app = counts.most_common(1)[0][0] if counts else "—"

    return {"total": total, "success_rate": success_rate, "top_app": top_app}


def get_timeline(start_ts: str = None, end_ts: str = None, app_ids: list = None):
    rows = query_api_calls(start_ts, end_ts, app_ids)
    from collections import defaultdict
    daily: dict = defaultdict(int)
    for r in rows:
        day = r["ts"][:10]
        daily[day] += 1
    return [{"date": d, "count": c} for d, c in sorted(daily.items())]


def get_distribution(start_ts: str = None, end_ts: str = None, app_ids: list = None):
    rows = query_api_calls(start_ts, end_ts, app_ids)
    from collections import Counter
    counts = Counter(r["app_name"] for r in rows)
    return [{"app": a, "count": c} for a, c in counts.most_common()]


# ── sync_log ──────────────────────────────────────────────────────────────────

def log_sync(records_in: int, success: bool = True, message: str = ""):
    with db_conn() as conn:
        conn.execute(
            "INSERT INTO sync_log (synced_at, records_in, success, message) VALUES (?, ?, ?, ?)",
            (datetime.utcnow().isoformat(), records_in, int(success), message),
        )


def last_sync():
    with db_conn() as conn:
        row = conn.execute(
            "SELECT synced_at, success, message FROM sync_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None
