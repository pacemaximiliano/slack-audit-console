"""
Slack Audit Console — FastAPI backend
Run: uvicorn main:app --reload --host 127.0.0.1 --port 8000
"""

import logging
import logging.handlers
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

import database as db
import slack_client as sc

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR      = Path(__file__).parent
ENV_FILE      = BASE_DIR / ".env"
TEMPLATES_DIR = BASE_DIR / "templates"
LOG_FILE      = BASE_DIR / "audit.log"

# ── Logging ───────────────────────────────────────────────────────────────────
_LOG_FORMAT = "%(asctime)s  %(levelname)-8s  %(name)-20s  %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

def _setup_logging():
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # Console handler (same as before)
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(_LOG_FORMAT, _DATE_FORMAT))
    root.addHandler(console)

    # Rotating file handler — 5 MB per file, keep last 7 files
    rotating = logging.handlers.RotatingFileHandler(
        LOG_FILE,
        maxBytes=5 * 1024 * 1024,
        backupCount=7,
        encoding="utf-8",
    )
    rotating.setFormatter(logging.Formatter(_LOG_FORMAT, _DATE_FORMAT))
    root.addHandler(rotating)

_setup_logging()
logger = logging.getLogger("main")

# ── FastAPI ───────────────────────────────────────────────────────────────────
app = FastAPI(title="Slack Audit Console")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

http_logger = logging.getLogger("http")

@app.middleware("http")
async def log_requests(request: Request, call_next):
    import time
    start = time.monotonic()
    response = await call_next(request)
    elapsed_ms = (time.monotonic() - start) * 1000
    http_logger.info(
        "%s %s → %d  (%.0fms)  client=%s",
        request.method,
        request.url.path + (f"?{request.url.query}" if request.url.query else ""),
        response.status_code,
        elapsed_ms,
        request.client.host if request.client else "-",
    )
    return response

# In-memory app cache — populated by /api/apps, used by /api/refresh
_app_cache: list[dict] = []


# ── .env helpers ──────────────────────────────────────────────────────────────

def _read_token() -> Optional[str]:
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("SLACK_BOT_TOKEN="):
                val = line.split("=", 1)[1].strip().strip('"').strip("'")
                return val or None
    return os.getenv("SLACK_BOT_TOKEN") or None


def _write_token(token: str):
    existing: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                existing[k.strip()] = v.strip()
    existing["SLACK_BOT_TOKEN"] = token
    ENV_FILE.write_text(
        "\n".join(f"{k}={v}" for k, v in existing.items()) + "\n",
        encoding="utf-8",
    )


def _parse_ids(app_ids: str) -> list:
    """Parse comma-separated app_ids query param into a list. None/empty → []."""
    if not app_ids:
        return []
    return [i.strip() for i in app_ids.split(",") if i.strip()]


def _ts_range(period: str) -> tuple[Optional[str], Optional[str]]:
    """Return (oldest_iso, latest_iso). Both None for 'all'."""
    now = datetime.now(timezone.utc)
    if period == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "7d":
        start = now - timedelta(days=7)
    elif period == "30d":
        start = now - timedelta(days=30)
    else:
        return None, None
    return start.isoformat(), now.isoformat()


# ── Startup ───────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    db.init_db()
    logger.info("Database ready at %s", db.DB_PATH)
    # Pre-load app cache from SQLite so the sidebar shows data immediately
    global _app_cache
    _app_cache = db.get_all_apps()
    logger.info("Loaded %d cached apps from DB", len(_app_cache))


# ── Frontend ──────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "token_set": bool(_read_token())},
    )


# ── Config ────────────────────────────────────────────────────────────────────

class TokenPayload(BaseModel):
    token: str


@app.post("/api/config/token")
async def save_token(payload: TokenPayload):
    token = payload.token.strip()
    if not token:
        raise HTTPException(400, "Token cannot be empty")
    if not (token.startswith("xoxp-") or token.startswith("xoxb-")):
        raise HTTPException(400, "Token must start with xoxp- (User) or xoxb- (Bot)")

    try:
        info = await sc.validate_token(token)
    except sc.SlackAuthError as exc:
        raise HTTPException(401, f"Invalid token: {exc}")
    except Exception as exc:
        raise HTTPException(502, f"Could not reach Slack: {exc}")

    if not info.get("is_enterprise"):
        logger.warning("Token validated but workspace is not Enterprise Grid — "
                       "audit log API may be unavailable")

    _write_token(token)
    return {
        "ok":            True,
        "team":          info.get("team"),
        "user":          info.get("user"),
        "is_enterprise": info.get("is_enterprise"),
        "token_type":    info.get("token_type"),
    }


@app.get("/api/config/status")
async def config_status():
    token = _read_token()
    if not token:
        return {"configured": False}
    try:
        info = await sc.validate_token(token)
        return {"configured": True, **info}
    except sc.SlackAuthError:
        return {"configured": False, "error": "Token invalid or revoked"}
    except Exception as exc:
        return {"configured": True, "warning": str(exc)}


# ── App discovery ─────────────────────────────────────────────────────────────

@app.get("/api/apps")
async def get_apps():
    global _app_cache
    token = _read_token()
    if not token:
        raise HTTPException(401, "No token configured")

    try:
        fresh = await sc.list_installed_apps(token)
    except sc.SlackAuthError as exc:
        raise HTTPException(401, str(exc))
    except Exception as exc:
        logger.exception("App discovery failed")
        raise HTTPException(502, str(exc))

    # Persist to SQLite so they survive server restarts
    for a in fresh:
        db.upsert_app(a["id"], a["name"], a.get("description", ""))

    _app_cache = db.get_all_apps()
    return {"apps": _app_cache}


# ── Refresh ───────────────────────────────────────────────────────────────────

class RefreshPayload(BaseModel):
    app_ids: Optional[list[str]] = None   # None = all apps in cache
    period:  Optional[str]       = "30d"  # today | 7d | 30d | all


@app.post("/api/refresh")
async def refresh(payload: RefreshPayload):
    global _app_cache

    token = _read_token()
    if not token:
        raise HTTPException(401, "No token configured")

    # Ensure app cache is populated
    if not _app_cache:
        try:
            fresh = await sc.list_installed_apps(token)
            for a in fresh:
                db.upsert_app(a["id"], a["name"], a.get("description", ""))
            _app_cache = db.get_all_apps()
        except Exception as exc:
            logger.exception("App discovery during refresh failed")
            raise HTTPException(502, f"App discovery failed: {exc}")

    target_ids = payload.app_ids or [a["id"] for a in _app_cache]
    oldest, latest = _ts_range(payload.period or "30d")

    try:
        events = await sc.fetch_audit_logs(token, target_ids, oldest, latest)
    except sc.SlackAuthError as exc:
        db.log_sync(0, success=False, message=str(exc))
        raise HTTPException(401, str(exc))
    except Exception as exc:
        logger.exception("Audit log fetch failed")
        db.log_sync(0, success=False, message=str(exc))
        raise HTTPException(502, str(exc))

    # Build a name lookup from the discovered apps so newly seen app IDs
    # get a proper name even if admin.apps.list missed them.
    name_map = {a["id"]: a["name"] for a in _app_cache}

    inserted = 0
    for ev in events:
        app_name = ev.get("app_name") or name_map.get(ev["app_id"], ev["app_id"])
        ok = db.insert_api_call(
            app_id    = ev["app_id"],
            app_name  = app_name,
            endpoint  = ev.get("endpoint", ""),
            ts        = ev["ts"],
            status    = ev.get("status", "200"),
            raw_event = ev.get("raw_event"),
            audit_id  = ev.get("audit_id"),
        )
        if ok:
            inserted += 1

    msg = f"{inserted} new records inserted from {len(events)} audit events fetched"
    logger.info(msg)
    db.log_sync(inserted, success=True, message=msg)
    return {"ok": True, "fetched": len(events), "inserted": inserted}


# ── Dashboard endpoints ───────────────────────────────────────────────────────

@app.get("/api/metrics")
async def metrics(period: str = "7d", app_ids: str = None):
    start, end = _ts_range(period)
    ids = _parse_ids(app_ids)
    data = db.get_metrics(start, end, ids)
    last = db.last_sync()
    return {**data, "last_sync": last}


@app.get("/api/timeline")
async def timeline(period: str = "7d", app_ids: str = None):
    start, end = _ts_range(period)
    ids = _parse_ids(app_ids)
    return {"data": db.get_timeline(start, end, ids)}


@app.get("/api/distribution")
async def distribution(period: str = "7d", app_ids: str = None):
    start, end = _ts_range(period)
    ids = _parse_ids(app_ids)
    return {"data": db.get_distribution(start, end, ids)}


@app.get("/api/calls")
async def calls(period: str = "7d", app_ids: str = None, limit: int = 200):
    start, end = _ts_range(period)
    ids = _parse_ids(app_ids)
    rows = db.query_api_calls(start, end, ids)
    return {"data": rows[:limit], "total": len(rows)}


# ── Diagnostics ───────────────────────────────────────────────────────────────

@app.get("/api/debug")
async def debug():
    """Diagnostic endpoint — shows DB state and last raw audit entries."""
    with db.db_conn() as conn:
        total_calls = conn.execute("SELECT COUNT(*) FROM api_calls").fetchone()[0]
        total_apps  = conn.execute("SELECT COUNT(*) FROM apps").fetchone()[0]
        last_5_calls = [
            dict(r) for r in conn.execute(
                "SELECT app_name, endpoint, ts, status FROM api_calls ORDER BY ts DESC LIMIT 5"
            ).fetchall()
        ]
        last_sync_row = conn.execute(
            "SELECT * FROM sync_log ORDER BY id DESC LIMIT 1"
        ).fetchone()

    token = _read_token()
    token_preview = (token[:8] + "...") if token else None

    return {
        "token_preview":    token_preview,
        "apps_in_db":       total_apps,
        "apps_in_cache":    len(_app_cache),
        "api_calls_in_db":  total_calls,
        "last_5_calls":     last_5_calls,
        "last_sync":        dict(last_sync_row) if last_sync_row else None,
    }
