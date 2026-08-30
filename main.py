"""
QBO Timesheet — minimal single-user FastAPI backend.

Logs a time entry straight into QuickBooks Online with the right
duration, employee, project/client, and service item.

Run:
    pip install -r requirements.txt
    uvicorn main:app --reload
    # then open http://localhost:8000  and click "Connect QuickBooks"
"""
import os
import hmac
import json
import time
import base64
import struct
import hashlib
import logging
import math
import secrets
import threading
import tempfile
import uuid
import urllib.parse
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse, FileResponse, JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator
from dotenv import load_dotenv

from write_journal import Journal, receipt_order
from time_reconciliation import report as _reconciliation_report

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CLIENT_ID = os.environ.get("QBO_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("QBO_CLIENT_SECRET", "")
REDIRECT_URI = os.environ.get("QBO_REDIRECT_URI", "http://localhost:8000/callback")
ENVIRONMENT = os.environ.get("QBO_ENVIRONMENT", "sandbox").lower()

SCOPE = "com.intuit.quickbooks.accounting"

# OAuth endpoints come from Intuit's OpenID discovery document (best practice —
# they stay current if Intuit ever moves an endpoint). The documented values
# below are the fallback if the discovery fetch fails, so auth never breaks.
DISCOVERY_URL = (
    "https://developer.api.intuit.com/.well-known/openid_sandbox_configuration"
    if ENVIRONMENT == "sandbox"
    else "https://developer.api.intuit.com/.well-known/openid_configuration"
)
_FALLBACK_AUTH_URL = "https://appcenter.intuit.com/connect/oauth2"
_FALLBACK_TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
_discovery_cache: dict[str, str] = {}


def _discover():
    """Fetch + cache authorization/token endpoints from Intuit's discovery doc."""
    if not _discovery_cache:
        try:
            doc = requests.get(
                DISCOVERY_URL, headers={"Accept": "application/json"}, timeout=15
            ).json()
            _discovery_cache["auth"] = doc["authorization_endpoint"]
            _discovery_cache["token"] = doc["token_endpoint"]
        except Exception:
            # Network hiccup or schema change — fall back to the documented URLs.
            _discovery_cache["auth"] = _FALLBACK_AUTH_URL
            _discovery_cache["token"] = _FALLBACK_TOKEN_URL
    return _discovery_cache


def auth_url():
    return _discover()["auth"]


def token_url():
    return _discover()["token"]

API_BASE = (
    "https://sandbox-quickbooks.api.intuit.com"
    if ENVIRONMENT == "sandbox"
    else "https://quickbooks.api.intuit.com"
)
# Since 2025-08-01 Intuit ignores minorversion < 75; 75 is the base version.
MINOR_VERSION = "75"
TOKENS_FILE = os.path.join(BASE_DIR, os.environ.get("QBO_TOKENS_FILE", "qbo_tokens.json"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("qbo-timesheet")

@asynccontextmanager
async def _lifespan(app):
    try:
        workers = int(os.environ.get("WEB_CONCURRENCY", "1"))
    except ValueError:
        workers = 0
    if workers != 1:
        raise RuntimeError("WEB_CONCURRENCY must be 1. Token refresh, OAuth, audit and reminder state require one application worker.")
    if HOSTED and not APP_PASSWORD:
        raise RuntimeError(
            "APP_PASSWORD is required when the Timesheet app is hosted or connected to production."
        )
    # Start the daily-reminder background thread on startup (lifespan replaces
    # the deprecated @app.on_event("startup")). _reminder_loop is defined below.
    threading.Thread(target=_reminder_loop, daemon=True).start()
    yield


app = FastAPI(title="QBO Timesheet", lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
_pending_states: dict[str, float] = {}  # OAuth CSRF state -> creation time


def _qbo_error(resp):
    """Log full QBO diagnostics, but return a small, safe, readable error."""
    tid = resp.headers.get("intuit_tid", "")
    log.error("QBO error %s tid=%s: %s", resp.status_code, tid, resp.text[:1000])
    code, message = "", "QuickBooks could not complete this request."
    try:
        errors = (resp.json().get("Fault") or {}).get("Error") or []
        if errors:
            first = errors[0]
            code = str(first.get("code") or "")
            message = first.get("Detail") or first.get("Message") or message
    except (ValueError, TypeError, AttributeError):
        pass
    return HTTPException(
        resp.status_code,
        {"message": message, "code": code, "supportId": tid or None},
    )


# ---------------------------------------------------------------------------
# Password gate (only active when APP_PASSWORD is set — required for hosting)
# ---------------------------------------------------------------------------
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
_redirect_host = (urllib.parse.urlparse(REDIRECT_URI).hostname or "").lower()
HOSTED = (
    ENVIRONMENT == "production"
    or urllib.parse.urlparse(REDIRECT_URI).scheme == "https"
    or _redirect_host not in {"", "localhost", "127.0.0.1", "::1"}
)
# Sliding-window session: the cookie is valid for SESSION_MAX_AGE since it was
# last issued, and any authenticated request past the halfway mark re-issues a
# fresh one — so an actively-used session never expires, while an idle one lapses
# after SESSION_MAX_AGE of inactivity.
SESSION_MAX_AGE = 7 * 24 * 3600          # 7-day idle window
SESSION_REFRESH_AFTER = SESSION_MAX_AGE // 2   # re-issue once past the halfway point
# Optional TOTP two-factor. When TOTP_SECRET is set, login also requires the
# current 6-digit code from an authenticator app (1Password, Google
# Authenticator, etc). Unset = password-only.
TOTP_SECRET = os.environ.get("TOTP_SECRET", "").replace(" ", "").upper()
if TOTP_SECRET and not APP_PASSWORD:
    # MFA is layered on top of the password gate; without APP_PASSWORD the gate
    # is off entirely, so TOTP would be silently inert and the app fully open.
    log.warning("TOTP_SECRET is set but APP_PASSWORD is empty — the app is UNGATED "
                "and two-factor is inactive. Set APP_PASSWORD to enable auth.")
_PUBLIC_PATHS = {"/", "/login", "/api/status", "/eula", "/privacy", "/sw.js"}


def _totp(secret_b32, when=None, step=30, digits=6):
    """RFC 6238 TOTP — pure stdlib, no dependencies."""
    counter = int((when if when is not None else time.time()) // step)
    key = base64.b32decode(secret_b32 + "=" * (-len(secret_b32) % 8))
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10 ** digits)).zfill(digits)


def _totp_valid(code, window=1):
    code = (code or "").strip()
    if not (TOTP_SECRET and code.isdigit()):
        return False
    now = time.time()
    # accept the neighbouring steps too, to tolerate clock drift
    return any(
        hmac.compare_digest(_totp(TOTP_SECRET, now + drift * 30), code)
        for drift in range(-window, window + 1)
    )


def _auth_signature(payload):
    key = hashlib.sha256(f"{APP_PASSWORD}:{CLIENT_SECRET}:{TOTP_SECRET}".encode()).digest()
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


def _auth_cookie_value(now=None):
    issued = int(now if now is not None else time.time())
    payload = f"{issued}.{secrets.token_urlsafe(18)}"
    return f"{payload}.{_auth_signature(payload)}"


def _auth_issued_at(request: Request):
    """Return the issue timestamp of a valid session cookie, else None."""
    raw = request.cookies.get("ts_auth", "")
    try:
        issued_s, nonce, signature = raw.split(".", 2)
        issued = int(issued_s)
    except (ValueError, TypeError):
        return None
    if not nonce or issued > time.time() + 60 or time.time() - issued > SESSION_MAX_AGE:
        return None
    payload = f"{issued_s}.{nonce}"
    return issued if hmac.compare_digest(signature, _auth_signature(payload)) else None


def _is_authed(request: Request) -> bool:
    if not APP_PASSWORD:
        return not HOSTED
    return _auth_issued_at(request) is not None


def _set_auth_cookie(resp, request, now=None):
    resp.set_cookie(
        "ts_auth",
        _auth_cookie_value(now),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )


@app.middleware("http")
async def require_password(request: Request, call_next):
    path = request.url.path
    if HOSTED and not APP_PASSWORD and path != "/api/status":
        return JSONResponse(
            {"detail": "This hosted app is locked because APP_PASSWORD is not configured."},
            status_code=503,
        )
    if APP_PASSWORD and path not in _PUBLIC_PATHS and not path.startswith("/static/"):
        issued = _auth_issued_at(request)
        if issued is None:
            # Browser page navigations get sent to the sign-in screen; API
            # calls get a JSON 401 the frontend can handle.
            if request.method == "GET" and not path.startswith("/api/"):
                return RedirectResponse("/")
            return JSONResponse({"detail": "Sign in required."}, status_code=401)
        response = await call_next(request)
        # Sliding window: any request past the halfway mark refreshes the cookie,
        # so an actively-used session never lapses.
        if time.time() - issued > SESSION_REFRESH_AFTER:
            _set_auth_cookie(response, request)
        return response
    return await call_next(request)


@app.middleware("http")
async def browser_security(request: Request, call_next):
    """Browser hardening plus a same-origin guard for every data-changing call."""
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
            return JSONResponse({"detail": "Cross-site request blocked."}, status_code=403)
        origin = request.headers.get("origin")
        if origin:
            origin_host = urllib.parse.urlparse(origin).netloc.lower()
            request_host = request.headers.get("host", "").lower()
            if origin_host and origin_host != request_host:
                return JSONResponse({"detail": "Request origin did not match this app."}, status_code=403)
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
    )
    if request.url.scheme == "https":
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    if request.url.path.startswith("/api/") or request.url.path in {"/mfa-setup", "/audit", "/ratecheck"}:
        response.headers.setdefault("Cache-Control", "no-store")
    return response


class Login(BaseModel):
    password: str
    code: str = ""  # TOTP 6-digit code, when MFA is enabled


_login_attempts: dict[str, list[float]] = {}
_login_lock = threading.Lock()
LOGIN_WINDOW = 15 * 60
LOGIN_MAX_FAILURES = 8


def _login_key(request):
    return request.client.host if request.client else "unknown"


def _login_failures(request):
    key, cutoff = _login_key(request), time.time() - LOGIN_WINDOW
    with _login_lock:
        recent = [t for t in _login_attempts.get(key, []) if t >= cutoff]
        _login_attempts[key] = recent
        return len(recent)


def _record_login_failure(request):
    key = _login_key(request)
    with _login_lock:
        _login_attempts.setdefault(key, []).append(time.time())


@app.post("/login")
def login(body: Login, request: Request):
    if _login_failures(request) >= LOGIN_MAX_FAILURES:
        raise HTTPException(429, "Too many sign-in attempts. Wait 15 minutes and try again.")
    if APP_PASSWORD and not hmac.compare_digest(body.password, APP_PASSWORD):
        _record_login_failure(request)
        raise HTTPException(401, "The password or authentication code was not accepted.")
    if TOTP_SECRET and not _totp_valid(body.code):
        _record_login_failure(request)
        raise HTTPException(401, "The password or authentication code was not accepted.")
    with _login_lock:
        _login_attempts.pop(_login_key(request), None)
    resp = JSONResponse({"ok": True})
    if APP_PASSWORD:
        _set_auth_cookie(resp, request)
    return resp


@app.post("/logout")
def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("ts_auth")
    return resp


# ---------------------------------------------------------------------------
# Token storage + OAuth
# Local default: a JSON file. If SUPABASE_URL + SUPABASE_SERVICE_KEY are set
# (hosted on a diskless free tier), tokens live in a Supabase table instead:
#   create table if not exists qbo_tokens (id int primary key, data jsonb);
# ---------------------------------------------------------------------------
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")


def _sb_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }


# Push data (VAPID keys + subscriptions) persists next to the tokens.
PUSH_FILE = os.path.join(os.path.dirname(TOKENS_FILE) or BASE_DIR, "qbo_push.json")


def _load_blob(path, sb_id):
    if SUPABASE_URL and SUPABASE_KEY:
        resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/qbo_tokens",
            params={"id": f"eq.{sb_id}", "select": "data"},
            headers=_sb_headers(),
            timeout=15,
        )
        resp.raise_for_status()
        rows = resp.json()
        if (not isinstance(rows, list) or len(rows) > 1
                or (rows and (not isinstance(rows[0], dict) or not isinstance(rows[0].get("data"), dict)))):
            raise HTTPException(503, "Stored app data could not be verified. Preserve it and review recovery before continuing.")
        return rows[0]["data"] if rows else None
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("expected an app data object")
        return data
    except (json.JSONDecodeError, ValueError) as exc:
        # Existing unreadable data is not an empty store. In particular, audit
        # and push callers must not replace corrupted history/keys with defaults.
        raise HTTPException(503, "Stored app data is unreadable. Keep the existing files and review recovery before continuing.") from exc


def _save_blob(path, sb_id, data):
    if SUPABASE_URL and SUPABASE_KEY:
        resp = requests.post(
            f"{SUPABASE_URL}/rest/v1/qbo_tokens",
            headers={**_sb_headers(), "Prefer": "resolution=merge-duplicates"},
            json=[{"id": sb_id, "data": data}],
            timeout=15,
        )
        resp.raise_for_status()
        return
    # Write to a temp file then atomically replace, so a crash mid-write can't
    # corrupt the blob (a corrupt qbo_tokens.json would drop the rotating
    # refresh token and force a full re-OAuth).
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, mode=0o700, exist_ok=True)
    descriptor, tmp = tempfile.mkstemp(prefix=".timesheet-", dir=directory)
    try:
        with os.fdopen(descriptor, "w") as f:
            json.dump(data, f, indent=2, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _load_tokens():
    return _load_blob(TOKENS_FILE, 1)


def _save_tokens(data):
    # Also used by OAuth callback: a company cannot change while a time write
    # is validating its browser identity, preflighting, or reaching QBO.
    with _token_lock:
        _save_blob(TOKENS_FILE, 1, data)


def _load_push():
    return _load_blob(PUSH_FILE, 2) or {}


def _save_push(data):
    _save_blob(PUSH_FILE, 2, data)


# ---------------------------------------------------------------------------
# Audit trail — an append-only record of every timesheet change (create,
# update, delete). Persists alongside tokens/push: a local JSON file by
# default, or the Supabase `qbo_tokens` table as id=3 on a diskless host.
# Capped to the most recent AUDIT_MAX events so the blob stays small.
# ---------------------------------------------------------------------------
AUDIT_FILE = os.path.join(os.path.dirname(TOKENS_FILE) or BASE_DIR, "qbo_audit.json")
AUDIT_MAX = 2000
_audit_lock = threading.Lock()


def _load_audit():
    return _load_blob(AUDIT_FILE, 3) or {}


def _save_audit(data):
    _save_blob(AUDIT_FILE, 3, data)


def _ta_summary(ta):
    """A readable snapshot of a QBO TimeActivity for the audit log."""
    ta = ta or {}
    who = ta.get("EmployeeRef") or ta.get("VendorRef") or {}
    item = ta.get("ItemRef") or {}
    cust = ta.get("CustomerRef") or {}
    return {
        "date": ta.get("TxnDate"),
        "hours": ta.get("Hours", 0),
        "minutes": ta.get("Minutes", 0),
        "who": who.get("name") or who.get("value"),
        "service": item.get("name") or item.get("value"),
        "customer": cust.get("name") or cust.get("value"),
        "billableStatus": ta.get("BillableStatus"),
        "description": ta.get("Description", ""),
    }


def _audit(action, entry_id, ta, request=None, before=None, scope=None):
    """Append one audit event. Never raises — auditing must not break a write."""
    try:
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "action": action,
            "entryId": str(entry_id) if entry_id is not None else None,
            "summary": _ta_summary(ta),
            "scope": scope or _write_scope(),
        }
        if (ta or {}).get("operationId"):
            rec["operationId"] = ta["operationId"]
        if before is not None:
            rec["before"] = _ta_summary(before)
        if request is not None and request.client:
            rec["ip"] = request.client.host
        with _audit_lock:
            data = _load_audit()
            events = data.get("events", [])
            events.append(rec)
            data["events"] = events[-AUDIT_MAX:]
            _save_audit(data)
        return True
    except Exception:
        logging.exception("audit write failed")
        return False


def _basic_auth_header():
    raw = f"{CLIENT_ID}:{CLIENT_SECRET}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def _token_request(payload):
    resp = requests.post(
        token_url(),
        headers={
            "Authorization": _basic_auth_header(),
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        data=payload,
        timeout=30,
    )
    if resp.status_code >= 400:
        tid = resp.headers.get("intuit_tid", "")
        log.error("Token endpoint error %s tid=%s: %s", resp.status_code, tid, resp.text[:500])
        raise HTTPException(
            resp.status_code,
            {"message": "QuickBooks sign-in could not be completed. Try reconnecting.", "supportId": tid or None},
        )
    return resp.json()


_token_lock = threading.RLock()  # token refresh, OAuth replacement, and time-write company fence


def get_access_token():
    """Return (access_token, realm_id), refreshing the access token if stale."""
    tokens = _load_tokens()
    if not tokens:
        raise HTTPException(401, "Not connected. Open / and click Connect QuickBooks.")
    if time.time() > tokens.get("access_expires_at", 0) - 60:
        with _token_lock:
            # Re-read under the lock: another thread may have just refreshed
            # (and rotated the refresh token) while we waited.
            tokens = _load_tokens() or tokens
            if time.time() > tokens.get("access_expires_at", 0) - 60:
                try:
                    fresh = _token_request(
                        {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]}
                    )
                except HTTPException:
                    # Refresh token revoked or expired — the only fix is to reconnect.
                    raise HTTPException(401, "QuickBooks connection expired. Reconnect from the home screen.")
                tokens["access_token"] = fresh["access_token"]
                # Intuit rotates the refresh token — always persist the latest one.
                tokens["refresh_token"] = fresh.get("refresh_token", tokens["refresh_token"])
                tokens["access_expires_at"] = time.time() + fresh["expires_in"]
                _save_tokens(tokens)
    return tokens["access_token"], tokens["realm_id"]


def qbo_query(statement):
    access_token, realm_id = get_access_token()
    resp = requests.get(
        f"{API_BASE}/v3/company/{realm_id}/query",
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        params={"query": statement, "minorversion": MINOR_VERSION},
        timeout=30,
    )
    if resp.status_code >= 400:
        raise _qbo_error(resp)
    return resp.json().get("QueryResponse", {})


# ---------------------------------------------------------------------------
# Routes: connection
# ---------------------------------------------------------------------------
@app.get("/")
def index():
    # no-store so neither the browser nor an installed PWA holds onto a stale
    # build — the home-screen app always fetches the freshly pulled page.
    return FileResponse(
        os.path.join(BASE_DIR, "index.html"),
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


@app.get("/sw.js")
def service_worker():
    # Served from root so the worker's scope is "/" (covers the whole app);
    # from /static it would only control /static and serviceWorker.ready
    # would never resolve for the page at /.
    return FileResponse(
        os.path.join(BASE_DIR, "static", "sw.js"),
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
    )


@app.get("/eula")
def eula():
    return FileResponse(os.path.join(BASE_DIR, "static", "eula.html"))


@app.get("/privacy")
def privacy():
    return FileResponse(os.path.join(BASE_DIR, "static", "privacy.html"))


@app.get("/api/status")
def status(request: Request):
    tokens = _load_tokens() or {}
    realm = str(tokens.get("realm_id") or "")
    company_key = _company_key(realm) if realm else None
    return {
        "connected": bool(tokens),
        "environment": ENVIRONMENT,
        "configured": bool(CLIENT_ID and CLIENT_SECRET),
        "auth_required": bool(APP_PASSWORD) or HOSTED,
        "mfa_required": bool(TOTP_SECRET),
        "authed": _is_authed(request),
        "companyKey": company_key,
    }


@app.get("/mfa-setup")
def mfa_setup():
    """One-time enrollment helper: generates a secret to add to your
    authenticator app and to Render as TOTP_SECRET. Password-gated."""
    secret = base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")
    label = "QBO%20Timesheet"
    otpauth = f"otpauth://totp/{label}?secret={secret}&issuer=QBO%20Timesheet"
    active = "Currently ACTIVE." if TOTP_SECRET else "Not yet active."
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Set up two-factor</title>
<style>body{{font-family:system-ui,sans-serif;max-width:560px;margin:0 auto;
padding:32px 20px;background:#12161c;color:#e6ebf1;line-height:1.6}}
code{{background:#0e1217;border:1px solid #2b333e;border-radius:6px;padding:2px 6px;
word-break:break-all}} .secret{{font-size:22px;letter-spacing:2px;display:block;
margin:14px 0;padding:14px;background:#0e1217;border:1px solid #4c9be8;border-radius:10px;
text-align:center}} a{{color:#4c9be8}} ol{{padding-left:20px}} li{{margin:10px 0}}
.note{{color:#8a97a6;font-size:14px}}</style></head><body>
<h1>Set up two-factor sign-in</h1>
<p class="note">Two-factor is {active}</p>
<p>Your new authenticator secret:</p>
<code class="secret">{secret}</code>
<ol>
<li>In <b>1Password</b> (or Google Authenticator): add a one-time password /
add TOTP, and paste this secret — or use this setup link:<br>
<code>{otpauth}</code></li>
<li>In <b>Render</b> &rarr; your service &rarr; <b>Environment</b>, add a variable
<code>TOTP_SECRET</code> set to the secret above, and save. Render redeploys.</li>
<li>After it redeploys, sign in: you'll enter your password <b>and</b> the current
6-digit code from your authenticator.</li>
</ol>
<p class="note">Keep this secret private. If you ever lose your authenticator,
delete the <code>TOTP_SECRET</code> variable in Render to disable two-factor,
then repeat this setup. Refreshing this page generates a new secret — use the
one you actually saved in both places.</p>
<p><a href="/">&larr; Back to the app</a></p>
</body></html>"""
    return HTMLResponse(html)


@app.get("/connect")
def connect():
    if not (CLIENT_ID and CLIENT_SECRET):
        raise HTTPException(500, "Set QBO_CLIENT_ID and QBO_CLIENT_SECRET in .env first.")
    now = time.time()
    for old_state, created in list(_pending_states.items()):
        if now - created > 600:
            _pending_states.pop(old_state, None)
    while len(_pending_states) >= 20:
        _pending_states.pop(next(iter(_pending_states)))
    state = secrets.token_urlsafe(24)
    _pending_states[state] = now
    params = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "scope": SCOPE,
        "redirect_uri": REDIRECT_URI,
        "state": state,
    }
    return RedirectResponse(f"{auth_url()}?{urllib.parse.urlencode(params)}")


@app.get("/callback")
def callback(code: str = "", state: str = "", realmId: str = ""):
    created = _pending_states.pop(state, None)
    if created is None or time.time() - created > 600:
        raise HTTPException(400, "Invalid or expired state.")
    data = _token_request(
        {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI}
    )
    _save_tokens(
        {
            "access_token": data["access_token"],
            "refresh_token": data["refresh_token"],
            "access_expires_at": time.time() + data["expires_in"],
            "realm_id": realmId,
        }
    )
    return RedirectResponse("/?connected=1")


# ---------------------------------------------------------------------------
# Routes: dropdown data
# ---------------------------------------------------------------------------
def qbo_query_all(entity, where="", key=None):
    """Query every row of an entity, paginating past QBO's 1000-row page cap
    (a practice can easily have >1000 customers/matters)."""
    key = key or entity
    out, start, page = [], 1, 1000
    while True:
        rows = qbo_query(
            f"SELECT * FROM {entity} {where} STARTPOSITION {start} MAXRESULTS {page}".strip()
        )
        batch = rows.get(key, [])
        out.extend(batch)
        if start > 100000:
            raise HTTPException(413, f"QuickBooks returned more than 100,000 {entity} records. Narrow the date range.")
        if len(batch) < page:
            break
        start += page
    return out


@app.get("/api/projects")
def list_projects():
    """Return QBO Projects (with their parent customer) and all other customers
    — top-level clients AND sub-customers/jobs (shown by qualified name)."""
    customers = qbo_query_all("Customer", "WHERE Active = true")
    projects, clients = [], []
    for c in customers:
        name = c.get("FullyQualifiedName") or c.get("DisplayName")
        # parentId lets the client roll sub-customers/jobs/projects up to their
        # top-level client (e.g. for concentration by main client). Sub-customers
        # and projects carry a ParentRef; a top-level client has none.
        parent_id = (c.get("ParentRef") or {}).get("value")
        if c.get("IsProject"):
            projects.append({"id": c["Id"], "name": name, "parentId": parent_id})
        else:
            clients.append({"id": c["Id"], "name": name, "parentId": parent_id})
    return {"projects": projects, "clients": clients}


@app.get("/api/company")
def company_identity():
    """Small authenticated identity banner so the user can verify the QBO company."""
    rows = qbo_query("SELECT * FROM CompanyInfo").get("CompanyInfo", [])
    company = rows[0] if rows else {}
    _, realm_id = get_access_token()
    return {
        "name": company.get("CompanyName") or company.get("LegalName") or "QuickBooks company",
        "realmSuffix": str(realm_id)[-4:] if realm_id else "",
        "environment": ENVIRONMENT,
    }


@app.get("/api/employees")
def list_employees():
    emps = qbo_query_all("Employee", "WHERE Active = true")
    return [{"id": e["Id"], "name": e.get("DisplayName")} for e in emps]


@app.get("/api/vendors")
def list_vendors():
    vendors = qbo_query_all("Vendor", "WHERE Active = true")
    return [{"id": v["Id"], "name": v.get("DisplayName")} for v in vendors]


@app.get("/api/items")
def list_items():
    items = qbo_query_all("Item", "WHERE Type = 'Service' AND Active = true")
    return [{"id": i["Id"], "name": i.get("Name")} for i in items]


# ---------------------------------------------------------------------------
# Routes: create a time entry
# ---------------------------------------------------------------------------
class TimeEntry(BaseModel):
    item_id: str
    employee_id: str | None = None
    vendor_id: str | None = None  # log under a vendor instead of an employee
    hours: int = 0
    minutes: int = 0
    description: str = ""
    txn_date: str | None = None
    billable: bool = False
    hourly_rate: float | None = None
    project_id: str | None = None
    customer_id: str | None = None  # project's parent, or the client itself
    # The SyncToken seen by the browser. Required for edits so a QuickBooks-side
    # change made after the screen loaded cannot be silently overwritten.
    sync_token: str | None = None
    allow_duplicate: bool = False
    allow_day_overflow: bool = False
    # Stable client supplied id for safe replay after a timeout. It is never
    # used as a QBO entity id; it identifies this intended operation only.
    operation_id: str | None = None
    # The company identity actually displayed by this browser, never inferred
    # from whichever company another tab may have connected since it loaded.
    company_key: str | None = None


def _validate_time_entry(entry: TimeEntry, *, updating=False):
    """Reject ambiguous or impossible writes before they reach QuickBooks."""
    if bool(entry.employee_id) == bool(entry.vendor_id):
        raise HTTPException(400, "Pick exactly one employee or vendor.")
    if not (entry.item_id or "").strip():
        raise HTTPException(400, "Pick a service.")
    if entry.hours < 0 or entry.minutes < 0 or entry.minutes > 59:
        raise HTTPException(400, "Enter a valid duration; minutes must be between 0 and 59.")
    total_minutes = entry.hours * 60 + entry.minutes
    if total_minutes <= 0:
        raise HTTPException(400, "Enter a duration greater than zero.")
    if total_minutes > 24 * 60:
        raise HTTPException(400, "A single entry cannot be longer than 24 hours.")
    if entry.txn_date:
        try:
            date.fromisoformat(entry.txn_date)
        except ValueError:
            raise HTTPException(400, "Choose a valid date.")
    if entry.hourly_rate is not None and (
        entry.hourly_rate < 0 or not math.isfinite(entry.hourly_rate)
    ):
        raise HTTPException(400, "Hourly rate must be zero or greater.")
    if entry.billable and not (entry.project_id or entry.customer_id):
        raise HTTPException(400, "Choose a project or client before marking time billable.")


def _timeactivity_payload(entry: TimeEntry):
    """Build the TimeActivity body shared by create and update."""
    if entry.vendor_id:
        payload = {"NameOf": "Vendor", "VendorRef": {"value": entry.vendor_id}}
    else:
        payload = {"NameOf": "Employee", "EmployeeRef": {"value": entry.employee_id}}
    payload |= {
        "ItemRef": {"value": entry.item_id},  # required
        "Hours": entry.hours,
        "Minutes": entry.minutes,
        "Description": entry.description,
        "TxnDate": entry.txn_date or _today().isoformat(),
    }
    # A QBO Project IS a sub-customer (IsProject=true), so time attaches to it
    # through CustomerRef = the project's own Customer.Id; QBO derives the
    # parent client from the project's ParentRef. We do NOT send ProjectRef:
    # it is gated to US + QBO Advanced/Enterprise and is rejected elsewhere
    # ("Invalid ProjectRef", code 9341). CustomerRef=project id works on every
    # Projects-enabled tier. `customer_id` (the parent) is no longer used to
    # build CustomerRef — only for the billable check below.
    billable_ref = entry.project_id or entry.customer_id
    if billable_ref:
        payload["CustomerRef"] = {"value": billable_ref}

    if entry.billable:
        if not billable_ref:
            raise HTTPException(400, "Choose a project or client before marking time billable.")
        payload["BillableStatus"] = "Billable"
        if entry.hourly_rate is not None:
            payload["HourlyRate"] = entry.hourly_rate
    else:
        payload["BillableStatus"] = "NotBillable"
    return payload


def _same_ref(ref, expected):
    return str((ref or {}).get("value") or "") == str(expected or "")


def _time_guard_snapshot(record):
    snapshot = {**(record.get("before") or {}), **record["payload"], **record["result"]}
    # Sparse employee -> vendor edits must not carry the old person's reference
    # forward from `before` when QBO omits it from the acknowledgment.
    if snapshot.get("NameOf") == "Vendor":
        snapshot.pop("EmployeeRef", None)
    elif snapshot.get("NameOf") == "Employee":
        snapshot.pop("VendorRef", None)
    return snapshot


def _time_guard_fields(row, day):
    """Only the fields that affect duplicate/day-limit decisions."""
    kind = row.get("NameOf") or ("Vendor" if row.get("VendorRef") else "Employee")
    return (row.get("TxnDate", day), kind,
            str((row.get(kind + "Ref") or {}).get("value") or ""),
            str((row.get("ItemRef") or {}).get("value") or ""),
            str((row.get("CustomerRef") or {}).get("value") or ""),
            int(row.get("Hours") or 0), int(row.get("Minutes") or 0),
            (row.get("Description") or "").strip(),
            row.get("BillableStatus") or "NotBillable")


def _time_guard_version(token):
    # QBO documents SyncToken as an incrementing version, represented as a
    # string. Unknown/compound formats are opaque, never sorted lexically or
    # compared by an assumed numeric prefix.
    value = str(token) if token is not None else ""
    return int(value) if value.isascii() and value.isdecimal() else None


def _time_guard_current(row, record, day):
    """Reconcile a query row with its last acknowledged local write once."""
    snapshot = _time_guard_snapshot(record)
    deleted = record["action"] == "delete"
    # An update payload contains the PRE-write token. Only its acknowledgment
    # can establish the updated version. A confirmed delete also proves that
    # its pre-delete version no longer exists, even if the reply lacks a token.
    committed_token = record["result"].get("SyncToken")
    if deleted and committed_token is None:
        committed_token = record["payload"].get("SyncToken")
    query_token = row.get("SyncToken")
    query_version, committed_version = map(_time_guard_version, (query_token, committed_token))
    if query_version is not None and committed_version is not None:
        if query_version > committed_version:
            return row  # A newer external QBO edit outranks a local receipt.
        if query_version < committed_version:
            return None if deleted else snapshot
    if deleted:
        if query_token is not None and str(query_token) == str(committed_token):
            return None
    elif _time_guard_fields(row, day) == _time_guard_fields(snapshot, day):
        # Identical guard inputs are safe even for old receipts without tokens.
        return row
    raise HTTPException(503, {
        "code": "DAY_STATE_UNCERTAIN", "entryId": str(row.get("Id") or ""),
        "message": "QuickBooks time for this date conflicts with a confirmed save, and its newer version cannot be verified. "
                   "No new time was sent. Reload and review QuickBooks and Reconciliation, then retry this original save ID.",
    })


def _guard_new_time(entry: TimeEntry):
    """Catch exact duplicates and impossible per-person day totals."""
    day = entry.txn_date or _today().isoformat()
    rows = list(qbo_query_all("TimeActivity", f"WHERE TxnDate = '{day}'"))
    expected_who = entry.vendor_id or entry.employee_id
    expected_customer = entry.project_id or entry.customer_id
    expected_status = "Billable" if entry.billable else "NotBillable"
    # Query visibility may lag a completed write even when the same ID is
    # present. Reconcile by entity version, then reapply date/person scope.
    # A later local update/delete supersedes the earlier create snapshot.
    latest = {}
    scope = _write_scope()
    for rec in _load_operations()["operations"]:
        if rec["scope"] != scope or rec["status"] != "completed":
            continue
        result = rec["result"]
        entry_id = str(result.get("Id") or rec["payload"].get("Id") or "")
        if not entry_id:
            continue
        if entry_id not in latest or receipt_order(rec) > receipt_order(latest[entry_id]):
            latest[entry_id] = rec
    visible = {str(row.get("Id")) for row in rows if row.get("Id") is not None}
    reconciled = []
    for row in rows:
        rec = latest.get(str(row.get("Id")))
        current = _time_guard_current(row, rec, day) if rec else row
        if current is not None and current.get("TxnDate", day) == day:
            reconciled.append(current)
    for entry_id, rec in latest.items():
        snapshot = _time_guard_snapshot(rec)
        # Absence alone cannot prove a later external move/delete. Retain the
        # confirmed contribution conservatively until QBO supplies that state.
        if entry_id not in visible and rec["action"] != "delete" and snapshot.get("TxnDate") == day:
            reconciled.append(snapshot)
    same_person = [
        row for row in reconciled
        if row.get("NameOf", "Vendor" if entry.vendor_id else "Employee") == ("Vendor" if entry.vendor_id else "Employee")
        and _same_ref(row.get("VendorRef") if entry.vendor_id else row.get("EmployeeRef"), expected_who)
    ]
    duplicate = any(
        _same_ref(row.get("ItemRef"), entry.item_id)
        and _same_ref(row.get("CustomerRef"), expected_customer)
        and int(row.get("Hours") or 0) == entry.hours
        and int(row.get("Minutes") or 0) == entry.minutes
        and (row.get("Description") or "").strip() == (entry.description or "").strip()
        and (row.get("BillableStatus") or "NotBillable") == expected_status
        for row in same_person
    )
    if duplicate and not entry.allow_duplicate:
        raise HTTPException(
            409,
            {"message": "An identical entry already exists for this person and date.", "code": "DUPLICATE_ENTRY"},
        )
    existing_minutes = sum(
        int(row.get("Hours") or 0) * 60 + int(row.get("Minutes") or 0)
        for row in same_person
    )
    if existing_minutes + entry.hours * 60 + entry.minutes > 24 * 60 and not entry.allow_day_overflow:
        raise HTTPException(
            409,
            {"message": "This would put the person over 24 logged hours for that date.", "code": "DAY_TOTAL"},
        )


def _post_timeactivity(payload, params=None):
    access_token, realm_id = get_access_token()
    params = dict(params or {})
    expected_scope = params.pop("_expected_scope", None)
    if expected_scope is not None and expected_scope != f"{API_BASE}|{realm_id}":
        raise _company_changed()
    resp = requests.post(
        f"{API_BASE}/v3/company/{realm_id}/timeactivity",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        params={"minorversion": MINOR_VERSION, **(params or {})},
        json=payload,
        timeout=30,
    )
    if resp.status_code >= 400:
        # Surface QBO's fault message so validation errors are readable.
        raise _qbo_error(resp)
    return resp.json().get("TimeActivity", resp.json())


# ---------------------------------------------------------------------------
# Durable write journal. All transitions use flock or Supabase revision CAS.
# QBO writes are sent only after their immutable identity is persisted.
# ---------------------------------------------------------------------------
OP_FILE = os.path.join(os.path.dirname(TOKENS_FILE) or BASE_DIR, "qbo_operations.json")


def _journal():
    return Journal(OP_FILE, url=SUPABASE_URL if SUPABASE_KEY else "", headers=_sb_headers())


def _operation_id(value):
    if not value:
        raise HTTPException(428, "operation_id is required for safe retries.")
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(400, "operation_id must be a UUID.")


def _write_scope():
    tokens = _load_tokens() or {}
    realm = tokens.get("realm_id")
    if not realm:
        raise HTTPException(401, "Connect QuickBooks before saving time.")
    return f"{API_BASE}|{realm}"


def _company_key(realm):
    return hashlib.sha256(f"{ENVIRONMENT}:{realm}".encode()).hexdigest()[:24]


def _company_changed():
    return HTTPException(409, {"code": "COMPANY_CHANGED", "message":
        "The connected QuickBooks company differs from this screen. No new time write was sent. "
        "Reload and review the company before saving; retain any original uncertain save ID."})


def _browser_write(company_key, operation_id, action, payload, prepare):
    # The single-process deployment constraint makes this the same lock used
    # by token refresh and OAuth replacement, including reentrant refreshes.
    with _token_lock:
        if not isinstance(company_key, str) or not company_key:
            raise HTTPException(428, {"code": "COMPANY_REQUIRED", "message":
                "Reload the app before saving so its QuickBooks company can be verified."})
        tokens = _load_tokens() or {}
        realm = tokens.get("realm_id")
        if not realm:
            raise HTTPException(401, "Connect QuickBooks before saving time.")
        if not hmac.compare_digest(company_key.encode(), _company_key(realm).encode()):
            raise _company_changed()
        scope = f"{API_BASE}|{realm}"
        return _journaled_qbo_write(operation_id, action, payload, scope=scope, prepare=prepare), scope


def _failed_operation_error(operation_id, status, detail):
    # A browser may forget a retry identity only after an acknowledged terminal
    # journal state, not merely an auth/company/validation response before lookup.
    detail = dict(detail) if isinstance(detail, dict) else {"message": str(detail)}
    return HTTPException(status, {**detail, "operationId": operation_id, "operationStatus": "failed"})


def _load_operations():
    return _journal().read()


def _journaled_qbo_write(operation_id, action, payload, *, prepare=None, scope=None):
    """Retry the original request, never reconstruct it from changed QBO data.

    A durable person/day reservation serializes new creates across hosts. A
    lease permits recovery after a process dies, but only for the same UUID.
    QBO requestid provides replay if its response was lost after the write.
    """
    scope = scope or _write_scope()
    journal = _journal()
    record = journal.claim(operation_id, action, payload, scope)
    if record["status"] == "completed":
        return {**record["result"], "_journalReplay": True}
    if record["status"] == "failed":
        raise _failed_operation_error(operation_id, record["errorStatus"], record["error"])
    owner = record["owner"]
    if not record["retryDispatched"]:
        try:
            before = prepare() if prepare else None
        except Exception as exc:
            # No QBO write was dispatched. A failed preflight releases its
            # reservation, while the immutable operation identity is retained.
            status = exc.status_code if isinstance(exc, HTTPException) else 502
            error = exc.detail if isinstance(exc, HTTPException) else "QuickBooks preflight failed; no time was written."
            journal.transition(operation_id, owner, "reserved" if status >= 500 else "failed",
                               leaseUntil=0, errorStatus=status, error=error)
            if status < 500:
                raise _failed_operation_error(operation_id, status, error)
            raise HTTPException(status, error)
        journal.transition(operation_id, owner, "in_flight", before=before)
    else:
        # Fence expired owners immediately before the outbound call as well.
        journal.transition(operation_id, owner, "in_flight")
    try:
        params = {"requestid": operation_id, "_expected_scope": scope}
        if action == "delete":
            params["operation"] = "delete"
        result = _post_timeactivity(record["payload"], params=params)
        if not isinstance(result, dict) or not result.get("Id"):
            raise ValueError("QuickBooks did not return an entry identity")
        result = {**result, "operationId": operation_id}
    except Exception as exc:
        code = exc.status_code if isinstance(exc, HTTPException) else 502
        error = exc.detail if isinstance(exc, HTTPException) else "QuickBooks write outcome is uncertain. Retry the unchanged request with its original save ID."
        uncertain = record["retryDispatched"] or code >= 500 or code in {408, 429}
        journal.transition(operation_id, owner, "uncertain" if uncertain else "failed", errorStatus=code, error=error)
        if uncertain:
            raise HTTPException(502, {"code": "OPERATION_UNCERTAIN", "operationId": operation_id,
                "message": "QuickBooks has not confirmed this save. Retry the unchanged request with its original save ID.",
                "cause": error})
        raise _failed_operation_error(operation_id, code, error)
    # If this commit fails, the durable in-flight reservation remains. Retrying
    # it safely replays QBO's original result with the same requestid.
    journal.transition(operation_id, owner, "completed", result=result)
    return result


def _read_timeactivity(entry_id):
    """Read a single TimeActivity — needed for its current SyncToken before an
    update or delete (both operations require it)."""
    access_token, realm_id = get_access_token()
    read = requests.get(
        f"{API_BASE}/v3/company/{realm_id}/timeactivity/{entry_id}",
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        params={"minorversion": MINOR_VERSION},
        timeout=30,
    )
    if read.status_code >= 400:
        raise _qbo_error(read)
    return read.json()["TimeActivity"]


@app.post("/api/timeactivity")
def create_time(entry: TimeEntry, request: Request):
    _validate_time_entry(entry)
    operation_id = _operation_id(entry.operation_id)
    payload = _timeactivity_payload(entry)
    ta, scope = _browser_write(entry.company_key, operation_id, "create", payload,
                               prepare=lambda: _guard_new_time(entry))
    if ta.pop("_journalReplay", False):
        return ta
    if _audit("create", ta.get("Id"), ta, request, scope=scope) is False:
        ta["appWarning"] = "Time was saved, but the local activity log could not be updated."
    return ta


@app.put("/api/timeactivity/{entry_id}")
def update_time(entry_id: str, entry: TimeEntry, request: Request):
    _validate_time_entry(entry)
    operation_id = _operation_id(entry.operation_id)
    payload = _timeactivity_payload(entry)
    payload.update({"sparse": True, "Id": entry_id, "SyncToken": entry.sync_token})
    before = None
    def prepare():
        nonlocal before
        before = _read_timeactivity(entry_id)
        if before.get("BillableStatus") == "HasBeenBilled":
            raise HTTPException(409, "This entry was already invoiced (billed) in QuickBooks and can't be edited here.")
        if entry.sync_token is None:
            raise HTTPException(428, "Reload the entry before editing it so QuickBooks changes are protected.")
        if str(before.get("SyncToken", "")) != str(entry.sync_token):
            raise HTTPException(409, "This entry changed in QuickBooks after you opened it. Reload and review the latest version before editing.")
        return before
    result, scope = _browser_write(entry.company_key, operation_id, "update", payload, prepare=prepare)
    if result.pop("_journalReplay", False):
        return result
    if _audit("update", entry_id, result, request, before=before, scope=scope) is False:
        result["appWarning"] = "Time was updated, but the local activity log could not be updated."
    return result


# ---------------------------------------------------------------------------
# Routes: recent entries + delete
# ---------------------------------------------------------------------------
def _today():
    """Today's date in the business timezone (REMINDER_TZ). Using the server's
    UTC date would shift ranges and the A/R 'as of' by a day for a Pacific user
    after ~5pm — this keeps every 'today' consistent with the reminder path."""
    try:
        return datetime.now(ZoneInfo(REMINDER_TZ)).date()
    except Exception:
        return date.today()


def _resolve_range(days, start, end):
    """Validate optional ?start/?end (must be real YYYY-MM-DD dates) and, when
    no start is given, default to `days` back from today. Returns (start, end).
    Shared by list_time and list_payments so range handling can't drift apart."""
    for d in (start, end):
        if d is None:
            continue
        try:
            date.fromisoformat(d)  # rejects both bad format and impossible dates (2026-13-99)
        except ValueError:
            raise HTTPException(400, "Dates must be valid YYYY-MM-DD.")
    if days < 0 or days > 3650:
        raise HTTPException(400, "Choose a range of 10 years or less.")
    if start and end and end < start:
        raise HTTPException(400, "End date must be on or after start date.")
    if not start:
        start = (_today() - timedelta(days=days)).isoformat()
    if end is None:
        end = _today().isoformat()
    return start, end


@app.get("/api/timeactivities")
def list_time(days: int = 14, start: str | None = None, end: str | None = None):
    """Entries in a date range. Either ?days=N back from today, or ?start=&end=."""
    start, end = _resolve_range(days, start, end)
    where = f"TxnDate >= '{start}'"
    if end:
        where += f" AND TxnDate <= '{end}'"
    # Paginate past the 1000-row page cap: a long range (quarter/year) or a
    # busy practice can easily exceed one page, and we must not silently drop
    # the oldest entries from totals.
    rows = qbo_query_all("TimeActivity", f"WHERE {where} ORDERBY TxnDate DESC")
    entries = []
    for t in rows:
        entries.append(
            {
                "id": t["Id"],
                "syncToken": t.get("SyncToken"),
                "date": t.get("TxnDate"),
                "hours": t.get("Hours", 0),
                "minutes": t.get("Minutes", 0),
                "description": t.get("Description", ""),
                "employee": (t.get("EmployeeRef") or t.get("VendorRef") or {}).get("name"),
                "nameOf": t.get("NameOf"),
                "employeeId": (t.get("EmployeeRef") or {}).get("value"),
                "vendorId": (t.get("VendorRef") or {}).get("value"),
                "itemId": (t.get("ItemRef") or {}).get("value"),
                "service": (t.get("ItemRef") or {}).get("name"),
                # CustomerRef carries the project's name when the entry is on a project.
                "customer": (t.get("CustomerRef") or {}).get("name"),
                "customerId": (t.get("CustomerRef") or {}).get("value"),
                "projectId": (t.get("ProjectRef") or {}).get("value"),
                # billable work = billable-not-yet-invoiced OR already-invoiced
                "billable": t.get("BillableStatus") in ("Billable", "HasBeenBilled"),
                "billableStatus": t.get("BillableStatus"),
                "hourlyRate": t.get("HourlyRate"),
            }
        )
    return entries


@app.get("/api/payments")
def list_payments(days: int = 14, start: str | None = None, end: str | None = None):
    """Actual money received in a date range: customer Payments + SalesReceipts.
    Each item is {date, amount, customer, kind}. Used by the dashboard's
    'Received' metric (real cash in, vs. billed/invoiced)."""
    start, end = _resolve_range(days, start, end)
    where = f"WHERE TxnDate >= '{start}'"
    if end:
        where += f" AND TxnDate <= '{end}'"
    where += " ORDERBY TxnDate DESC"
    out = []
    # Both entities carry TotalAmt (cash received) + TxnDate + CustomerRef.
    for kind, entity in (("payment", "Payment"), ("salesreceipt", "SalesReceipt")):
        for r in qbo_query_all(entity, where):
            out.append({
                "id": r["Id"],
                "date": r.get("TxnDate"),
                "amount": r.get("TotalAmt", 0),
                "customer": (r.get("CustomerRef") or {}).get("name"),
                "kind": kind,
            })
    return out


@app.get("/api/bills")
def list_bills(days: int = 14, start: str | None = None, end: str | None = None):
    """Vendor costs in a date range: Bills + Purchases (checks / expenses / CC),
    excluding credit-card credits & refunds (those are money in, not a cost).
    Each item is {id, date, amount, vendor, kind}. Used by the dashboard's
    subcontractor-cost / margin section, which scopes cost to the vendors who
    actually log time (the subcontractors)."""
    start, end = _resolve_range(days, start, end)
    where = f"WHERE TxnDate >= '{start}'"
    if end:
        where += f" AND TxnDate <= '{end}'"
    where += " ORDERBY TxnDate DESC"
    out = []
    for r in qbo_query_all("Bill", where):
        out.append({
            "id": r["Id"],
            "date": r.get("TxnDate"),
            "amount": _num(r.get("TotalAmt")),
            "vendor": (r.get("VendorRef") or {}).get("name"),
            "vendorId": (r.get("VendorRef") or {}).get("value"),
            "kind": "bill",
        })
    for r in qbo_query_all("Purchase", where):
        if r.get("Credit"):  # credit-card credit / refund → money in, skip
            continue
        # Purchase payee is EntityRef (may be a Vendor/Customer/Employee).
        out.append({
            "id": r["Id"],
            "date": r.get("TxnDate"),
            "amount": _num(r.get("TotalAmt")),
            "vendor": (r.get("EntityRef") or {}).get("name"),
            "vendorId": (r.get("EntityRef") or {}).get("value"),
            "kind": "purchase",
        })
    return out


# ---------------------------------------------------------------------------
# Routes: accounts receivable (open invoices, aging, DSO)
# ---------------------------------------------------------------------------
_AGING_BUCKETS = ("0-30", "31-60", "61-90", "90+")


def _num(x):
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def _bucket_for_age(days):
    if days <= 30:
        return "0-30"
    if days <= 60:
        return "31-60"
    if days <= 90:
        return "61-90"
    return "90+"


def _receivables_summary(open_invoices, billed_365, as_of):
    """Pure aggregation of open invoices into outstanding total, aging buckets
    (by days since invoice date), past-due total (by DueDate), a who-owes-you
    per-client list, and an approximate DSO. Kept separate from the QBO read so
    it's unit-testable with a fixed as_of date."""
    buckets = {b: 0.0 for b in _AGING_BUCKETS}
    by_client, rows = {}, []
    outstanding = past_due = 0.0
    for inv in open_invoices:
        bal = _num(inv.get("Balance"))
        if bal <= 0:  # backstop in case the query returns fully-paid invoices
            continue
        txn = inv.get("TxnDate")
        try:
            age = (as_of - date.fromisoformat(txn)).days if txn else 0
        except ValueError:
            age = 0
        bucket = _bucket_for_age(age)
        buckets[bucket] += bal
        outstanding += bal
        due = inv.get("DueDate")
        overdue = False
        if due:
            try:
                overdue = date.fromisoformat(due) < as_of
            except ValueError:
                overdue = False
        if overdue:
            past_due += bal
        cref = inv.get("CustomerRef") or {}
        cust = cref.get("name") or "—"
        # key by customer id (not name) so two distinct QBO customers that share
        # a display name don't merge into one "who owes you" row.
        ckey = cref.get("value") or ("n:" + cust)
        c = by_client.setdefault(ckey, {"customer": cust, "customerId": cref.get("value"), "balance": 0.0, "count": 0})
        c["balance"] += bal
        c["count"] += 1
        rows.append({
            "id": inv.get("Id"),
            "docNumber": inv.get("DocNumber"),
            "customer": cust,
            "customerId": cref.get("value"),
            "date": txn,
            "dueDate": due,
            "amount": _num(inv.get("TotalAmt")),
            "balance": bal,
            "daysOld": age,
            "bucket": bucket,
            "overdue": overdue,
        })
    rows.sort(key=lambda r: (r["date"] or ""), reverse=True)
    clients = sorted(by_client.values(), key=lambda c: c["balance"], reverse=True)
    # Standard DSO ≈ outstanding A/R ÷ trailing-year billed × 365.
    dso = round(outstanding / billed_365 * 365) if billed_365 > 0 else None
    return {
        "asOf": as_of.isoformat(),
        "outstanding": round(outstanding, 2),
        "pastDue": round(past_due, 2),
        "aging": {k: round(v, 2) for k, v in buckets.items()},
        "byClient": [{**c, "balance": round(c["balance"], 2)} for c in clients],
        "invoices": rows,
        "dso": dso,
        "billed365": round(billed_365, 2),
    }


@app.get("/api/receivables")
def list_receivables():
    """Open accounts receivable as of today: total outstanding, aging buckets,
    past-due, per-client balances, and an approximate DSO. Reads QBO Invoice
    (Balance / TxnDate / DueDate / CustomerRef)."""
    today = _today()
    today_s = today.isoformat()
    open_invoices = qbo_query_all("Invoice", f"WHERE Balance > '0' AND TxnDate <= '{today_s}'")
    year_ago = (today - timedelta(days=365)).isoformat()
    billed_365 = sum(
        _num(i.get("TotalAmt"))
        for i in qbo_query_all("Invoice", f"WHERE TxnDate >= '{year_ago}' AND TxnDate <= '{today_s}'")
        if not i.get("TxnDate") or i.get("TxnDate") <= today_s
    )
    return _receivables_summary(open_invoices, billed_365, today)


def _project_range(start, end):
    start, end = _resolve_range(3650, start, end)
    if not start or not end:
        raise HTTPException(400, "A bounded start and end date are required.")
    return start, end


def _known_financial_amount(value):
    """Keep an absent or invalid accounting amount distinct from a real zero."""
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        amount = float(value)
        return amount if math.isfinite(amount) else None
    except (TypeError, ValueError, OverflowError):
        return None


@app.get("/api/project-financials")
def project_financials(project_id: str, start: str | None = None, end: str | None = None):
    """Read-only financials attributed only to invoices whose CustomerRef is
    this exact QBO project. Payments count only when a Payment line explicitly
    links to one of those invoices and supplies an amount."""
    start, end = _project_range(start, end)
    invoices = qbo_query_all("Invoice", f"WHERE TxnDate >= '{start}' AND TxnDate <= '{end}'")
    if not project_id.strip():
        raise HTTPException(400, "Choose a project.")
    matched = [i for i in invoices if _same_ref(i.get("CustomerRef"), project_id)
               and start <= (i.get("TxnDate") or "") <= end]
    invoice_ids = {str(i["Id"]) for i in matched if i.get("Id") is not None}
    payments = []
    currencies = set()
    skipped_ambiguous = skipped_invalid = 0
    for p in qbo_query_all("Payment", f"WHERE TxnDate >= '{start}' AND TxnDate <= '{end}'"):
        if not start <= (p.get("TxnDate") or "") <= end:
            continue
        for line_number, line in enumerate(p.get("Line") or []):
            links = line.get("LinkedTxn") or []
            if not any(str(x.get("TxnId")) in invoice_ids and x.get("TxnType") == "Invoice" for x in links):
                continue
            # One aggregate line amount cannot be apportioned across multiple
            # links (even multiple project invoices), credits, or other projects.
            if len(links) != 1 or links[0].get("TxnType") != "Invoice":
                skipped_ambiguous += 1
                continue
            try:
                raw_amount = line.get("Amount")
                if isinstance(raw_amount, bool) or raw_amount in (None, ""):
                    raise ValueError()
                amount = float(raw_amount)
                if not math.isfinite(amount) or amount < 0:
                    raise ValueError()
            except (TypeError, ValueError, OverflowError):
                skipped_invalid += 1
                continue
            currency = (p.get("CurrencyRef") or {}).get("value") or "home"
            currencies.add(currency)
            payments.append({"id": p.get("Id"), "lineId": line.get("Id", str(line_number)), "date": p.get("TxnDate"),
                             "invoiceIds": [str(links[0]["TxnId"])], "amount": amount, "currency": currency})
    invoice_rows = [{"id": i.get("Id"), "date": i.get("TxnDate"), "docNumber": i.get("DocNumber"),
                     "amount": _known_financial_amount(i.get("TotalAmt")), "balance": _known_financial_amount(i.get("Balance")),
                     "currency": (i.get("CurrencyRef") or {}).get("value") or "home"} for i in matched]
    currencies.update(r["currency"] for r in invoice_rows)
    return {"projectId": project_id, "start": start, "end": end,
            "scope": "Invoices dated in this range whose CustomerRef exactly equals this project; payments dated in this range explicitly linked to those invoices.",
            "currencies": sorted(currencies), "mixedCurrency": len(currencies) > 1,
            "invoices": invoice_rows, "payments": payments,
            "skippedAmbiguousPaymentLines": skipped_ambiguous, "skippedInvalidPaymentLines": skipped_invalid,
            "caveats": ["Parent-client invoices and ProjectRef/line-level associations are not assumed to belong to this project.",
                        "Payments against invoices outside the selected invoice date range are not included; this is not complete project payment history.",
                        "Missing or invalid invoice amounts and balances are shown as unknown, not zero.",
                        "Only nonnegative finite amounts with exactly one Invoice link are attributed. Ambiguous or invalid payment lines are excluded."]}


@app.get("/api/reconciliation")
def reconciliation(start: str | None = None, end: str | None = None):
    """Fresh scoped QBO data versus durable receipts; never repairs or writes."""
    start, end = _project_range(start, end)
    scope = _write_scope()
    rows = qbo_query_all("TimeActivity", f"WHERE TxnDate >= '{start}' AND TxnDate <= '{end}'")
    journal = _load_operations()
    audit = _load_audit()
    return _reconciliation_report(rows, journal["operations"], audit.get("events", []), start, end, scope)


@app.delete("/api/timeactivity/{entry_id}")
async def delete_time(entry_id: str, request: Request):
    try:
        body = await request.json() if request is not None else {}
    except Exception:
        raise HTTPException(400, "Supply the original save ID and entry version as JSON.")
    if not isinstance(body, dict):
        raise HTTPException(400, "Supply the original save ID and entry version as JSON.")
    operation_id = _operation_id(body.get("operation_id") or body.get("operationId"))
    supplied_token = body.get("sync_token", body.get("syncToken"))
    deleted = None
    def prepare():
        nonlocal deleted
        deleted = _read_timeactivity(entry_id)
        if deleted.get("BillableStatus") == "HasBeenBilled":
            raise HTTPException(409, "This entry was already invoiced (billed) in QuickBooks and can't be deleted here.")
        if supplied_token is None:
            raise HTTPException(428, "Reload the entry before deleting it so QuickBooks changes are protected.")
        if str(deleted.get("SyncToken", "")) != str(supplied_token):
            raise HTTPException(409, "This entry changed in QuickBooks after you opened it. Reload and review the latest version before deleting.")
        return deleted
    result, scope = _browser_write(body.get("company_key"), operation_id, "delete",
                                   {"Id": entry_id, "SyncToken": supplied_token}, prepare=prepare)
    out = {"deleted": entry_id, "operationId": operation_id}
    if result.pop("_journalReplay", False):
        return out
    if _audit("delete", entry_id, deleted, request, scope=scope) is False:
        out["appWarning"] = "Time was deleted, but the local activity log could not be updated."
    return out


@app.get("/api/audit")
def get_audit(limit: int = 500):
    """Audit events, newest first. Each records the action, entry, a snapshot
    of the entry's fields (and the prior values on updates), time, and source."""
    limit = max(1, min(limit, AUDIT_MAX))
    events = _load_audit().get("events", [])
    return list(reversed(events))[:limit]


def _audit_line(e):
    """Render one audit event as an HTML row for the /audit viewer."""
    import html as _html

    s = e.get("summary") or {}
    dur = f"{s.get('hours', 0)}:{str(s.get('minutes', 0)).zfill(2)}"
    action = e.get("action", "")
    who = _html.escape(str(s.get("who") or "—"))
    cust = _html.escape(str(s.get("customer") or "—"))
    svc = _html.escape(str(s.get("service") or "—"))
    desc = _html.escape(str(s.get("description") or ""))
    ts = _html.escape(str(e.get("ts", "")))
    bill = _html.escape(str(s.get("billableStatus") or ""))
    entry_id = _html.escape(str(e.get("entryId") or ""))
    # Show what changed on an update, if we captured the prior values.
    delta = ""
    before = e.get("before")
    if action == "update" and before:
        changes = []
        for k, lbl in (("date", "date"), ("hours", "h"), ("minutes", "m"),
                       ("who", "who"), ("service", "service"),
                       ("customer", "client"), ("billableStatus", "billable"),
                       ("description", "notes")):
            if before.get(k) != s.get(k):
                changes.append(lbl)
        if changes:
            delta = " · changed: " + _html.escape(", ".join(changes))
    return (
        f'<div class="ev {action}"><div class="row1">'
        f'<span class="act {action}">{action}</span>'
        f'<span class="cust">{cust}</span>'
        f'<span class="dur">{dur}</span></div>'
        f'<div class="row2">{s.get("date") or "—"} · {who} · {svc}'
        f'{" · " + bill if bill else ""}{delta}</div>'
        f'{f"<div class=notes>{desc}</div>" if desc else ""}'
        f'<div class="ts">{ts} · entry #{entry_id}</div></div>'
    )


@app.get("/audit")
def audit_page():
    """Password-gated, human-readable view of the timesheet audit trail."""
    events = _load_audit().get("events", [])
    rows = "".join(_audit_line(e) for e in reversed(events))
    if not rows:
        rows = '<p class="note">No changes recorded yet.</p>'
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Activity log</title>
<style>body{{font-family:system-ui,sans-serif;max-width:640px;margin:0 auto;
padding:24px 16px 60px;background:#12161c;color:#e6ebf1;line-height:1.5}}
h1{{font-size:22px}} a{{color:#4c9be8;text-decoration:none}}
.note{{color:#8a97a6}} .ev{{border:1px solid #2b333e;border-radius:10px;
padding:12px 14px;margin:10px 0;background:#0e1217}}
.row1{{display:flex;align-items:center;gap:10px}}
.act{{font-size:11px;text-transform:uppercase;letter-spacing:.06em;
font-weight:700;padding:2px 8px;border-radius:6px}}
.act.create{{background:rgba(74,200,140,.16);color:#4ac88c}}
.act.update{{background:rgba(76,155,232,.16);color:#4c9be8}}
.act.delete{{background:rgba(232,90,90,.16);color:#e85a5a}}
.cust{{font-weight:600;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.dur{{font-family:ui-monospace,Menlo,monospace}}
.row2{{color:#8a97a6;font-size:13px;margin-top:4px}}
.notes{{font-size:13px;margin-top:4px}}
.ts{{color:#5f6b7a;font-size:11px;font-family:ui-monospace,Menlo,monospace;margin-top:6px}}
.ev.delete{{opacity:.85}}</style></head><body>
<p><a href="/">&larr; Back to Timesheet</a></p>
<h1>Activity log</h1>
<p class="note">Every entry added, edited, or deleted — newest first
({len(events)} recorded).</p>
{rows}
</body></html>"""
    return HTMLResponse(html)


# ---------------------------------------------------------------------------
# Rate check (read-only diagnostic): reports how many recent TimeActivity
# entries carry an HourlyRate, and what rates appear per person × service —
# so we can tell whether real QuickBooks dollar figures are available before
# building any $ feature. Reads only; writes nothing.
# ---------------------------------------------------------------------------
def _ratecheck(days=365):
    start = (_today() - timedelta(days=days)).isoformat()
    rows = qbo_query_all("TimeActivity", f"WHERE TxnDate >= '{start}' ORDERBY TxnDate DESC")
    total = with_rate = known_rate = 0
    combos = {}  # (person, service) -> {"n":int, "withrate":int, "rates":set}
    for t in rows:
        who = (t.get("EmployeeRef") or t.get("VendorRef") or {}).get("name") or "(none)"
        svc = (t.get("ItemRef") or {}).get("name") or "(none)"
        rate = t.get("HourlyRate")
        try:
            rate = float(rate) if rate not in (None, "") else 0.0
        except (TypeError, ValueError):
            rate = 0.0
        # A present zero is a known rate and must not be reported as missing.
        has = t.get("HourlyRate") not in (None, "") and math.isfinite(rate)
        total += 1
        with_rate += 1 if has else 0
        known_rate += 1 if has else 0
        c = combos.setdefault((who, svc), {"n": 0, "withrate": 0, "rates": set()})
        c["n"] += 1
        if has:
            c["withrate"] += 1
            c["rates"].add(round(rate, 2))
    table = []
    for (who, svc), c in sorted(combos.items()):
        table.append({
            "person": who, "service": svc, "entries": c["n"],
            "withRate": c["withrate"],
            "knownRate": c["withrate"],
            "rates": sorted(c["rates"]),
        })
    return {
        "days": days,
        "examined": total,
        "withRate": with_rate,
        "knownRate": known_rate,
        "withoutRate": total - with_rate,
        "coveragePct": round(100 * with_rate / total, 1) if total else 0,
        "distinctRates": sorted({r for row in table for r in row["rates"]}),
        "byPersonService": table,
    }


@app.get("/api/ratecheck")
def api_ratecheck(days: int = 365):
    return _ratecheck(max(1, min(days, 1830)))


@app.get("/ratecheck")
def ratecheck_page(days: int = 365):
    """Password-gated, human-readable rate coverage report."""
    import html as _html
    d = _ratecheck(max(1, min(days, 1830)))
    if not d["examined"]:
        body = '<p class="note">No time entries found in this window.</p>'
    else:
        verdict = ("Every entry carries a rate — real $ figures are fully available."
                   if d["withoutRate"] == 0 else
                   f'{d["coveragePct"]}% of entries carry a rate. '
                   + ("Most do — $ is workable; the rest would be flagged as “no rate.”"
                      if d["coveragePct"] >= 50 else
                      "Only some do — most time has no rate stored on it, so $ is not reliably available yet."))
        rrows = ""
        for r in d["byPersonService"]:
            rates = ", ".join(f"${x:,.2f}" for x in r["rates"]) or "—"
            miss = r["entries"] - r["withRate"]
            flag = "" if miss == 0 else f' <span class="miss">({miss} no rate)</span>'
            rrows += (f'<tr><td>{_html.escape(r["person"])}</td>'
                      f'<td>{_html.escape(r["service"])}</td>'
                      f'<td class="num">{r["entries"]}</td>'
                      f'<td class="rate">{rates}{flag}</td></tr>')
        body = (
            f'<p class="big">{d["withRate"]} of {d["examined"]} entries carry an hourly rate '
            f'<span class="pct">({d["coveragePct"]}%)</span></p>'
            f'<p class="verdict">{_html.escape(verdict)}</p>'
            f'<p class="note">{len(d["distinctRates"])} distinct rate'
            f'{"" if len(d["distinctRates"]) == 1 else "s"} seen'
            f'{": " + ", ".join(f"${x:,.2f}" for x in d["distinctRates"]) if d["distinctRates"] else ""}.</p>'
            f'<table><thead><tr><th>Person</th><th>Service</th><th class="num">Entries</th>'
            f'<th class="rate">Rate(s) on file</th></tr></thead><tbody>{rrows}</tbody></table>'
        )
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Rate check</title>
<style>body{{font-family:system-ui,sans-serif;max-width:680px;margin:0 auto;
padding:24px 16px 60px;background:#12161c;color:#e6ebf1;line-height:1.5}}
h1{{font-size:22px}} a{{color:#4c9be8;text-decoration:none}} .note{{color:#8a97a6;font-size:14px}}
.big{{font-size:20px;font-weight:650;margin:14px 0 4px}} .pct{{color:#4c9be8}}
.verdict{{background:#0e1217;border:1px solid #2b333e;border-radius:10px;padding:12px 14px}}
table{{border-collapse:collapse;width:100%;margin-top:16px;font-size:14px}}
th,td{{text-align:left;padding:8px 10px;border-bottom:1px solid #242c36;vertical-align:top}}
th{{color:#8a97a6;font-size:12px;text-transform:uppercase;letter-spacing:.05em}}
.num{{text-align:right;font-family:ui-monospace,Menlo,monospace}}
.rate{{font-family:ui-monospace,Menlo,monospace}}
.miss{{color:#e0a458}}</style></head><body>
<p><a href="/">&larr; Back to Timesheet</a></p>
<h1>Rate check</h1>
<p class="note">Read-only. Looks at the last {d["days"]} days of QuickBooks time
entries and reports which carry an hourly rate. Nothing is changed.</p>
{body}
</body></html>"""
    return HTMLResponse(html)


# ---------------------------------------------------------------------------
# Daily reminder — Web Push (payloadless VAPID; message lives in the SW).
# Self-contained: VAPID keys are generated once and persisted with the push
# blob, so no manual key setup is needed. Reminders only fire when at least
# one device has subscribed. Config via env: REMINDER_HOUR (default 17),
# REMINDER_TZ (default America/Chicago), REMINDER_WEEKDAYS_ONLY (default 1).
# ---------------------------------------------------------------------------
REMINDER_HOUR = int(os.environ.get("REMINDER_HOUR", "17"))
REMINDER_TZ = os.environ.get("REMINDER_TZ", "America/Chicago")
REMINDER_WEEKDAYS_ONLY = os.environ.get("REMINDER_WEEKDAYS_ONLY", "1") != "0"
# Weekly review nudge (#6): weekday to prompt an end-of-week review on
# (Mon=0 … Fri=4; -1 disables). Fires once that day past REMINDER_HOUR.
WEEKLY_REVIEW_DAY = int(os.environ.get("WEEKLY_REVIEW_DAY", "4"))
_push_lock = threading.Lock()


def _reminders_due(now, push):
    """Which reminders are due given the current time + push state — pure and
    unit-testable. Returns a set drawn from {"weekly", "daily"}."""
    due = set()
    if now.hour < REMINDER_HOUR:
        return due
    today = now.date().isoformat()
    if (WEEKLY_REVIEW_DAY >= 0 and now.weekday() == WEEKLY_REVIEW_DAY
            and push.get("last_weekly") != today):
        due.add("weekly")
    if (push.get("last_reminder") != today
            and not (REMINDER_WEEKDAYS_ONLY and now.weekday() >= 5)):
        due.add("daily")
    return due


def _b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _vapid():
    """Return the persisted VAPID keypair, generating + saving it on first use."""
    with _push_lock:
        push = _load_push()
        if not push.get("vapid"):
            priv = ec.generate_private_key(ec.SECP256R1())
            priv_pem = priv.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ).decode()
            pub_raw = priv.public_key().public_bytes(
                serialization.Encoding.X962,
                serialization.PublicFormat.UncompressedPoint,
            )
            push["vapid"] = {"private_pem": priv_pem, "app_key": _b64u(pub_raw)}
            push.setdefault("subs", [])
            _save_push(push)
        return push["vapid"]


def _vapid_jwt(endpoint):
    """Signed ES256 VAPID JWT scoped to the push endpoint's origin."""
    parts = urllib.parse.urlsplit(endpoint)
    aud = f"{parts.scheme}://{parts.netloc}"
    header = _b64u(json.dumps({"typ": "JWT", "alg": "ES256"}).encode())
    claims = _b64u(json.dumps({
        "aud": aud,
        "exp": int(time.time()) + 12 * 3600,
        "sub": "mailto:mb@baykalconsulting.com",
    }).encode())
    priv = serialization.load_pem_private_key(_vapid()["private_pem"].encode(), password=None)
    der = priv.sign(f"{header}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    sig = _b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return f"{header}.{claims}.{sig}"


def _send_push(sub):
    """POST a payloadless push to one subscription. Returns False if the
    subscription is gone (404/410) so the caller can prune it."""
    try:
        # Stored endpoints may have been accepted by an older build.
        endpoint = _validated_push_endpoint(sub.get("endpoint"))
    except (ValueError, TypeError, AttributeError):
        log.warning("discarding push subscription with an unsupported endpoint")
        return False
    try:
        resp = requests.post(
            endpoint,
            headers={
                "Authorization": f"vapid t={_vapid_jwt(endpoint)},k={_vapid()['app_key']}",
                "TTL": "86400",
            },
            timeout=15,
            allow_redirects=False,
        )
        if 300 <= resp.status_code < 400:
            log.warning("push service redirected; subscription must be renewed")
            return False
        if resp.status_code in (404, 410):
            return False
        if resp.status_code >= 400:
            log.warning("push send %s: %s", resp.status_code, resp.text[:200])
        return True
    except Exception as e:
        log.warning("push send error: %s", e)
        return True


def _notify_all():
    """Send the reminder to every subscribed device; prune dead subs. Reads and
    prunes under _push_lock (network sends happen outside it) and re-reads before
    pruning so a subscription added concurrently isn't clobbered."""
    with _push_lock:
        subs = list(_load_push().get("subs", []))
    if not subs:
        return 0
    alive = [s for s in subs if _send_push(s)]  # network — outside the lock
    dead = [s for s in subs if s not in alive]
    if dead:
        with _push_lock:
            push = _load_push()
            push["subs"] = [s for s in push.get("subs", []) if s not in dead]
            _save_push(push)
    return len(alive)


def _logged_time_today(tz_today):
    try:
        rows = qbo_query(
            f"SELECT * FROM TimeActivity WHERE TxnDate = '{tz_today}' MAXRESULTS 1"
        )
        return bool(rows.get("TimeActivity"))
    except Exception:
        return True  # on any doubt, don't nag


def _reminder_tick():
    now = datetime.now(ZoneInfo(REMINDER_TZ))
    today = now.date().isoformat()
    # Claim the once-per-day/week slots under the lock so this write can't clobber
    # a subscription added concurrently. Release before the QBO check / sends.
    with _push_lock:
        push = _load_push()
        if not push.get("subs"):
            return
        due = _reminders_due(now, push)
        if not due:
            return
        if "weekly" in due:
            push["last_weekly"] = today
        if "daily" in due:
            push["last_reminder"] = today
        _save_push(push)
    # A weekly review prompt fires regardless of whether time is logged, and
    # takes the slot for the day (no duplicate daily nudge on the same tick).
    if "weekly" in due:
        n = _notify_all()
        log.info("weekly review reminder sent to %d device(s)", n)
        return
    if "daily" in due and not _logged_time_today(today):
        n = _notify_all()
        log.info("daily reminder sent to %d device(s)", n)


def _reminder_loop():
    while True:
        try:
            _reminder_tick()
        except Exception as e:
            log.warning("reminder tick error: %s", e)
        time.sleep(900)  # every 15 min


def _validated_push_endpoint(value):
    if not isinstance(value, str) or value != value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value) or "\\" in value:
        raise ValueError("Use a supported browser push service over HTTPS.")
    parsed = urllib.parse.urlsplit(value)
    host = (parsed.hostname or "").lower()
    allowed = host in {"fcm.googleapis.com", "web.push.apple.com", "push.services.mozilla.com"} or host.endswith(".push.services.mozilla.com")
    if (parsed.scheme != "https" or not allowed or parsed.username is not None or parsed.password is not None
            or parsed.port is not None or parsed.fragment or "#" in value):
        raise ValueError("Use a supported browser push service over HTTPS without credentials, ports, or fragments.")
    return value


class PushSub(BaseModel):
    endpoint: str
    keys: dict | None = None

    @field_validator("endpoint")
    @classmethod
    def https_endpoint(cls, value):
        return _validated_push_endpoint(value)


@app.get("/api/push/config")
def push_config():
    return {"appKey": _vapid()["app_key"], "reminderHour": REMINDER_HOUR}


@app.post("/api/push/subscribe")
def push_subscribe(sub: PushSub):
    with _push_lock:
        push = _load_push()
        push.setdefault("subs", [])
        if not any(s.get("endpoint") == sub.endpoint for s in push["subs"]):
            push["subs"].append(sub.model_dump())
            _save_push(push)
    return {"subscribed": True, "devices": len(_load_push().get("subs", []))}


@app.post("/api/push/test")
def push_test():
    n = _notify_all()
    if not n:
        raise HTTPException(400, "No subscribed devices. Enable reminders first.")
    return {"sent": n}
