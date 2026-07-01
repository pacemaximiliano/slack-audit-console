"""
Slack API client — Enterprise Grid edition.

Endpoints:
  auth.test                  → validate token, get org info
  admin.apps.list            → full list of installed apps (xoxp- + admin.apps:read)
  audit/v1/logs              → audit events (xoxp- + auditlogs:read)

Token requirement:
  Both admin.apps.list and audit/v1/logs require a User OAuth Token (xoxp-...)
  belonging to an Org Admin. A bot token (xoxb-) will return missing_scope.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger("slack_client")

SLACK_API  = "https://slack.com/api"
AUDIT_API  = "https://api.slack.com/audit/v1"

RATE_LIMIT_SLEEP = 5
MAX_RETRIES      = 4
PAGE_SIZE        = 200   # max items per paginated request


class SlackError(Exception):
    pass

class SlackAuthError(SlackError):
    pass

class SlackRateLimitError(SlackError):
    pass


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _make_client(token: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type":  "application/json",
        },
        timeout=30,
    )


async def _get(client: httpx.AsyncClient, url: str,
               params: dict = None) -> dict:
    """GET with automatic retry on 429 and Slack-level error handling."""
    for attempt in range(1, MAX_RETRIES + 1):
        resp = await client.get(url, params=params)

        # ── HTTP-level errors ─────────────────────────────────────────────────
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", RATE_LIMIT_SLEEP))
            logger.warning("Rate limited by Slack — waiting %ss (attempt %d)", wait, attempt)
            await asyncio.sleep(wait)
            continue

        if resp.status_code in (401, 403):
            # Slack sometimes returns a real 401/403 (not just {"ok":false})
            # for the Audit API when the token lacks the required scope.
            try:
                body = resp.json()
                error = body.get("error", f"http_{resp.status_code}")
            except Exception:
                error = f"http_{resp.status_code}"
            logger.error("Auth/permission error %s — url=%s error=%s",
                         resp.status_code, url, error)
            raise SlackAuthError(
                f"{error} (HTTP {resp.status_code}) — "
                "ensure the token is xoxp- (User Token) from an Org Admin "
                "with scopes: admin.apps:read and auditlogs:read"
            )

        if resp.status_code >= 400:
            # Convert any remaining 4xx/5xx to SlackError so callers' except blocks catch it
            try:
                body  = resp.json()
                error = body.get("error") or body.get("message") or f"http_{resp.status_code}"
            except Exception:
                error = f"http_{resp.status_code}"
            logger.error("HTTP %s for %s — %s", resp.status_code, url, error)
            raise SlackError(f"HTTP {resp.status_code}: {error}")

        data = resp.json()

        # The Audit Logs API (api.slack.com/audit/v1/...) returns
        # {"entries": [...]} with NO "ok" field — only the Web API uses "ok".
        # Skip the ok-check for audit responses; treat presence of "entries"
        # or "response_metadata" as success.
        is_audit = AUDIT_API in url
        if is_audit:
            return data

        # ── Slack Web API application-level errors (HTTP 200 with ok:false) ──
        if not data.get("ok"):
            error = data.get("error", "unknown_error")
            logger.error("Slack error: %s — url=%s params=%s body=%s",
                         error, url, params, json.dumps(data)[:300])
            if error in ("invalid_auth", "token_revoked", "not_authed",
                         "account_inactive", "token_expired"):
                raise SlackAuthError(error)
            if error in ("missing_scope", "not_allowed_token_type"):
                raise SlackAuthError(
                    f"{error} — token needs scopes: admin.apps:read, auditlogs:read"
                )
            if error == "ratelimited":
                await asyncio.sleep(RATE_LIMIT_SLEEP)
                continue
            raise SlackError(
                f"{error}: {data.get('response_metadata', {}).get('messages', [])}"
            )

        return data

    raise SlackRateLimitError("Exceeded max retries after rate limiting")


# ── Token validation ──────────────────────────────────────────────────────────

async def validate_token(token: str) -> dict:
    """Validate token and return workspace/org metadata."""
    async with _make_client(token) as client:
        data = await _get(client, f"{SLACK_API}/auth.test")

    token_type = "user" if token.startswith("xoxp-") else "bot"
    return {
        "user_id":         data.get("user_id"),
        "user":            data.get("user"),
        "team":            data.get("team"),
        "team_id":         data.get("team_id"),
        "is_enterprise":   data.get("enterprise_id") is not None,
        "enterprise_id":   data.get("enterprise_id"),
        "enterprise_name": data.get("enterprise_name"),
        "token_type":      token_type,
    }


# ── App discovery ─────────────────────────────────────────────────────────────

async def list_installed_apps(token: str) -> list[dict]:
    """
    Return all apps installed in the org via admin.apps.list.

    Requires: xoxp- token with admin.apps:read scope.

    The endpoint returns pages via response_metadata.next_cursor.
    Each page item shape:
      {
        "app": { "id": "A...", "name": "...", "description": "..." },
        "scopes": [...],
        ...
      }

    Falls back to the audit log approach (scraping distinct app actors)
    if admin.apps.list returns a permission error.
    """
    # Merge results from both sources — apps can exist in one but not the other:
    # - admin.apps.approved.list → apps that went through formal approval flow
    # - audit/v1/logs actor scan → apps that made API calls (regardless of approval)
    apps: dict[str, dict] = {}

    async with _make_client(token) as client:

        # ── Source 1: admin.apps.approved.list (all pages) ───────────────────
        try:
            cursor = None
            page   = 0
            while True:
                params: dict[str, Any] = {"limit": PAGE_SIZE}
                if cursor:
                    params["cursor"] = cursor

                data  = await _get(client, f"{SLACK_API}/admin.apps.approved.list", params)
                page += 1
                items = data.get("approved_apps", [])
                logger.info("admin.apps.approved.list page %d → %d items", page, len(items))

                for item in items:
                    a   = item.get("app") or item
                    aid = a.get("id", "")
                    if aid:
                        apps[aid] = {
                            "id":          aid,
                            "name":        a.get("name", "Unknown"),
                            "description": a.get("description", ""),
                        }

                cursor = (data.get("response_metadata") or {}).get("next_cursor") or ""
                if not cursor:
                    break

            logger.info("admin.apps.approved.list total: %d apps", len(apps))

        except SlackError as exc:
            logger.warning("admin.apps.approved.list failed (%s)", exc)

        # ── Source 2: audit log actor scan (bounded) ─────────────────────────
        # Reads recent pages only — enough to discover active apps without
        # paging through the entire org history.
        DISCOVERY_MAX_PAGES = 10   # 10 pages × 200 entries = 2 000 recent events
        try:
            cursor = None
            page   = 0
            before = len(apps)

            while True:
                params = {"limit": PAGE_SIZE}
                if cursor:
                    params["cursor"] = cursor

                data    = await _get(client, f"{AUDIT_API}/logs", params)
                page   += 1
                entries = data.get("entries", [])
                logger.info("audit/v1/logs actor scan page %d → %d entries", page, len(entries))

                for entry in entries:
                    # Apps appear in two places depending on the event type:
                    #   actor.app  → app performing an admin action (rare)
                    #   entity.app → app being acted upon (installed, scopes changed, etc.)
                    candidates = []
                    actor  = entry.get("actor", {})
                    entity = entry.get("entity", {})
                    if actor.get("type") == "app":
                        candidates.append(actor.get("app", {}))
                    if entity.get("type") == "app":
                        candidates.append(entity.get("app", {}))

                    for app_info in candidates:
                        aid = app_info.get("id", "")
                        if aid and aid not in apps:
                            apps[aid] = {
                                "id":          aid,
                                "name":        app_info.get("name", "Unknown"),
                                "description": app_info.get("description", ""),
                            }
                            logger.debug("discovered app %s (%s) via %s entity=%s",
                                         aid, app_info.get("name"), entry.get("action"), entity.get("type"))

                cursor = (data.get("response_metadata") or {}).get("next_cursor") or ""
                if not cursor or page >= DISCOVERY_MAX_PAGES:
                    if page >= DISCOVERY_MAX_PAGES:
                        logger.info("audit log scan stopped at page limit (%d)", DISCOVERY_MAX_PAGES)
                    break

            logger.info("audit log scan added %d new apps (total now %d)",
                        len(apps) - before, len(apps))

        except SlackError as exc:
            logger.warning("audit log actor scan failed (%s)", exc)

    if apps:
        return list(apps.values())

    # Last resort — return the token's own identity
    async with _make_client(token) as client:
        auth = await _get(client, f"{SLACK_API}/auth.test")
    return [{
        "id":          auth.get("app_id", "self"),
        "name":        auth.get("user", "This App") + " (token owner)",
        "description": "",
    }]


# ── Audit log fetch ───────────────────────────────────────────────────────────

# Actions that represent an app making a Slack API call.
# Extend this list as needed for your Deno apps.
API_REQUEST_ACTIONS = {
    "api_request",          # explicit api_request audit action
}

# When action filter is too restrictive, we also accept any event where
# the actor type is "app" (catch-all for Slack plans that log differently).
ACCEPT_ANY_APP_ACTOR = True


async def fetch_audit_logs(token: str, app_ids: list[str],
                           oldest: str = None,
                           latest: str = None) -> list[dict]:
    """
    Download audit log entries from audit/v1/logs and return normalised dicts.

    Each dict contains:
      audit_id   — Slack's unique event ID (used as dedup key in SQLite)
      app_id     — Slack App ID
      app_name   — Human-readable app name
      endpoint   — The Slack API method the app called (from details.api_app_id
                   or action field)
      ts         — ISO-8601 timestamp
      status     — "200" | "500" derived from details.response.status_code
      raw_event  — Full JSON string of the original entry
    """
    app_id_set = set(app_ids)
    results: list[dict] = []

    async with _make_client(token) as client:
        cursor = None
        page   = 0

        while True:
            params: dict[str, Any] = {"limit": PAGE_SIZE}
            if oldest:
                params["oldest"] = _to_unix_ts(oldest)
            if latest:
                params["latest"] = _to_unix_ts(latest)
            if cursor:
                params["cursor"] = cursor

            data  = await _get(client, f"{AUDIT_API}/logs", params)
            page += 1
            entries = data.get("entries", [])

            matched = 0
            skipped_no_app = 0
            skipped_filter = 0
            for entry in entries:
                norm = _normalise(entry, app_id_set)
                if norm:
                    results.append(norm)
                    matched += 1
                else:
                    # Distinguish why it was skipped for diagnostics
                    action = entry.get("action", "")
                    entity = entry.get("entity", {})
                    actor  = entry.get("actor", {})
                    has_app = (entity.get("type") == "app" or actor.get("type") == "app")
                    if not has_app:
                        skipped_no_app += 1
                    else:
                        skipped_filter += 1

            logger.info(
                "audit/v1/logs page %d → %d entries | matched=%d skipped(no_app=%d filtered=%d)",
                page, len(entries), matched, skipped_no_app, skipped_filter,
            )

            meta   = data.get("response_metadata", {})
            cursor = meta.get("next_cursor") or ""
            if not cursor:
                break

    logger.info("Fetch complete: %d matching records across %d pages", len(results), page)
    return results


def _to_unix_ts(iso_or_unix) -> int:
    """Accept either ISO-8601 string or unix int and return unix int."""
    if isinstance(iso_or_unix, (int, float)):
        return int(iso_or_unix)
    try:
        dt = datetime.fromisoformat(str(iso_or_unix).replace("Z", "+00:00"))
        return int(dt.timestamp())
    except Exception:
        return 0


def _normalise(entry: dict, app_id_set: set) -> dict | None:
    """
    Map a raw Slack Audit Log entry to our internal schema.

    The Audit Logs API records *administrative security events*, not individual
    API calls.  Every event where an app is involved (installed, scopes changed,
    token used, etc.) counts as one auditable activity for that app.

    Apps appear in two places:
      actor.app  → the app that performed the action (rare: app-to-app admin ops)
      entity.app → the app that was acted upon (common: installs, scope changes…)

    We accept both so no event is missed.

    Returned dict fields:
      audit_id  — Slack UUID (dedup key)
      app_id    — Slack App ID  (A...)
      app_name  — Human-readable name
      endpoint  — Meaningful label: action name + entity type  e.g. "app_installed"
      ts        — ISO-8601 UTC timestamp
      status    — "200" for normal events, "500" for events flagged as failures
      raw_event — Full JSON for inspection
    """
    action = entry.get("action", "unknown_action")
    actor  = entry.get("actor", {})
    entity = entry.get("entity", {})

    # ── Resolve which app this event belongs to ───────────────────────────────
    app_info = {}
    if entity.get("type") == "app":
        app_info = entity.get("app", {})
    elif actor.get("type") == "app":
        app_info = actor.get("app", {})

    app_id = app_info.get("id", "")

    # Skip entries with no app identity
    if not app_id:
        return None

    # Filter to the apps selected in the UI (empty set = accept all)
    if app_id_set and app_id not in app_id_set:
        return None

    # ── Build a human-readable endpoint/action label ──────────────────────────
    # Map the raw Slack action name to something meaningful for the dashboard.
    ACTION_LABELS = {
        "app_installed":                "App instalada",
        "app_uninstalled":              "App desinstalada",
        "app_scopes_expanded":          "Scopes ampliados",
        "app_scopes_dropped":           "Scopes reducidos",
        "app_approved":                 "App aprobada",
        "app_restricted":               "App restringida",
        "app_token_rotated":            "Token rotado",
        "user_app_install":             "Instalación por usuario",
        "bot_token_used":               "Token de bot usado",
        "api_request":                  "API Request",
        "app_resources_added":          "Recursos añadidos",
        "app_resources_removed":        "Recursos eliminados",
        "app_resources_granted":        "Recursos otorgados",
        "app_connection_created":       "Conexión creada",
        "app_connection_deleted":       "Conexión eliminada",
    }
    endpoint = ACTION_LABELS.get(action, action)

    # ── Timestamp ─────────────────────────────────────────────────────────────
    ts_unix = entry.get("date_create", 0)
    ts_iso  = datetime.fromtimestamp(ts_unix, tz=timezone.utc).isoformat()

    # ── Status: treat flagged/error events as 500, everything else as 200 ─────
    details = entry.get("details", {})
    is_error = (
        details.get("error")
        or details.get("failed")
        or str(details.get("status_code", "200")).startswith(("4", "5"))
        or action in ("app_uninstalled", "app_restricted")
    )
    status = "500" if is_error else "200"

    return {
        "audit_id":  entry.get("id", ""),
        "app_id":    app_id,
        "app_name":  app_info.get("name", "Unknown"),
        "endpoint":  endpoint,
        "ts":        ts_iso,
        "status":    status,
        "raw_event": json.dumps(entry),
    }
