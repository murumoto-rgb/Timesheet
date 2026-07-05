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
import re
import hmac
import json
import time
import base64
import struct
import hashlib
import logging
import secrets
import threading
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
from pydantic import BaseModel
from dotenv import load_dotenv

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
    # Start the daily-reminder background thread on startup (lifespan replaces
    # the deprecated @app.on_event("startup")). _reminder_loop is defined below.
    threading.Thread(target=_reminder_loop, daemon=True).start()
    yield


app = FastAPI(title="QBO Timesheet", lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
_pending_states: set[str] = set()  # CSRF state (fine for a single local user)


def _qbo_error(resp):
    """Log a QBO error with its intuit_tid (support trace id) and return an
    HTTPException that surfaces both to the client."""
    tid = resp.headers.get("intuit_tid", "")
    log.error("QBO error %s tid=%s: %s", resp.status_code, tid, resp.text[:1000])
    detail = resp.text
    if tid:
        detail += f"\n(intuit_tid: {tid})"
    return HTTPException(resp.status_code, detail)


# ---------------------------------------------------------------------------
# Password gate (only active when APP_PASSWORD is set — required for hosting)
# ---------------------------------------------------------------------------
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
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


def _auth_cookie_value():
    key = hashlib.sha256(f"{APP_PASSWORD}:{CLIENT_SECRET}:{TOTP_SECRET}".encode()).digest()
    return hmac.new(key, b"qbo-timesheet-auth-v1", hashlib.sha256).hexdigest()


def _is_authed(request: Request) -> bool:
    if not APP_PASSWORD:
        return True
    return hmac.compare_digest(request.cookies.get("ts_auth", ""), _auth_cookie_value())


@app.middleware("http")
async def require_password(request: Request, call_next):
    path = request.url.path
    if APP_PASSWORD and path not in _PUBLIC_PATHS and not path.startswith("/static/"):
        if not _is_authed(request):
            # Browser page navigations get sent to the sign-in screen; API
            # calls get a JSON 401 the frontend can handle.
            if request.method == "GET" and not path.startswith("/api/"):
                return RedirectResponse("/")
            return JSONResponse({"detail": "Sign in required."}, status_code=401)
    return await call_next(request)


class Login(BaseModel):
    password: str
    code: str = ""  # TOTP 6-digit code, when MFA is enabled


@app.post("/login")
def login(body: Login, request: Request):
    if APP_PASSWORD and not hmac.compare_digest(body.password, APP_PASSWORD):
        raise HTTPException(401, "Wrong password.")
    if TOTP_SECRET and not _totp_valid(body.code):
        raise HTTPException(401, "Wrong or missing authentication code.")
    resp = JSONResponse({"ok": True})
    if APP_PASSWORD:
        resp.set_cookie(
            "ts_auth",
            _auth_cookie_value(),
            max_age=180 * 24 * 3600,
            httponly=True,
            samesite="lax",
            secure=request.url.scheme == "https",
        )
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
        return rows[0]["data"] if rows else None
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, ValueError):
        return None  # empty/corrupt file — treat as absent


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
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _load_tokens():
    return _load_blob(TOKENS_FILE, 1)


def _save_tokens(data):
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


def _audit(action, entry_id, ta, request=None, before=None):
    """Append one audit event. Never raises — auditing must not break a write."""
    try:
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "action": action,
            "entryId": str(entry_id) if entry_id is not None else None,
            "summary": _ta_summary(ta),
        }
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
    except Exception:
        logging.exception("audit write failed")


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
        raise HTTPException(resp.status_code, f"Intuit token endpoint: {resp.text}")
    return resp.json()


_token_lock = threading.Lock()   # serialize refresh (request thread vs reminder thread)


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
    return {
        "connected": _load_tokens() is not None,
        "environment": ENVIRONMENT,
        "configured": bool(CLIENT_ID and CLIENT_SECRET),
        "auth_required": bool(APP_PASSWORD),
        "mfa_required": bool(TOTP_SECRET),
        "authed": _is_authed(request),
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
    state = secrets.token_urlsafe(16)
    _pending_states.add(state)
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
    if state not in _pending_states:
        raise HTTPException(400, "Invalid or expired state.")
    _pending_states.discard(state)
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
        if len(batch) < page or start > 100000:  # last page, or safety cap
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
        if c.get("IsProject"):
            parent = c.get("ParentRef", {})
            projects.append({"id": c["Id"], "name": name, "parentId": parent.get("value")})
        else:
            clients.append({"id": c["Id"], "name": name})
    return {"projects": projects, "clients": clients}


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
        "TxnDate": entry.txn_date or date.today().isoformat(),
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

    if entry.billable and billable_ref:
        payload["BillableStatus"] = "Billable"
        if entry.hourly_rate:
            payload["HourlyRate"] = entry.hourly_rate
    else:
        payload["BillableStatus"] = "NotBillable"
    return payload


def _post_timeactivity(payload, params=None):
    access_token, realm_id = get_access_token()
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


@app.post("/api/timeactivity")
def create_time(entry: TimeEntry, request: Request):
    if not (entry.employee_id or entry.vendor_id):
        raise HTTPException(400, "Pick an employee or vendor.")
    ta = _post_timeactivity(_timeactivity_payload(entry))
    _audit("create", ta.get("Id"), ta, request)
    return ta


@app.put("/api/timeactivity/{entry_id}")
def update_time(entry_id: str, entry: TimeEntry, request: Request):
    if not (entry.employee_id or entry.vendor_id):
        raise HTTPException(400, "Pick an employee or vendor.")
    access_token, realm_id = get_access_token()
    # Update needs the current SyncToken — read the entity first.
    read = requests.get(
        f"{API_BASE}/v3/company/{realm_id}/timeactivity/{entry_id}",
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        params={"minorversion": MINOR_VERSION},
        timeout=30,
    )
    if read.status_code >= 400:
        raise _qbo_error(read)
    before = read.json()["TimeActivity"]
    payload = _timeactivity_payload(entry)
    payload["Id"] = entry_id
    payload["SyncToken"] = before["SyncToken"]
    result = _post_timeactivity(payload)
    _audit("update", entry_id, result, request, before=before)
    return result


# ---------------------------------------------------------------------------
# Routes: recent entries + delete
# ---------------------------------------------------------------------------
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@app.get("/api/timeactivities")
def list_time(days: int = 14, start: str | None = None, end: str | None = None):
    """Entries in a date range. Either ?days=N back from today, or ?start=&end=."""
    for d in (start, end):
        if d and not _DATE_RE.match(d):
            raise HTTPException(400, "Dates must be YYYY-MM-DD.")
    if not start:
        start = (date.today() - timedelta(days=days)).isoformat()
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
    for d in (start, end):
        if d and not _DATE_RE.match(d):
            raise HTTPException(400, "Dates must be YYYY-MM-DD.")
    if not start:
        start = (date.today() - timedelta(days=days)).isoformat()
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


@app.delete("/api/timeactivity/{entry_id}")
def delete_time(entry_id: str, request: Request):
    access_token, realm_id = get_access_token()
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}

    # Delete requires the current SyncToken, so read the entity first.
    read = requests.get(
        f"{API_BASE}/v3/company/{realm_id}/timeactivity/{entry_id}",
        headers=headers,
        params={"minorversion": MINOR_VERSION},
        timeout=30,
    )
    if read.status_code >= 400:
        raise _qbo_error(read)
    deleted = read.json()["TimeActivity"]

    resp = requests.post(
        f"{API_BASE}/v3/company/{realm_id}/timeactivity",
        headers={**headers, "Content-Type": "application/json"},
        params={"operation": "delete", "minorversion": MINOR_VERSION},
        json={"Id": entry_id, "SyncToken": deleted["SyncToken"]},
        timeout=30,
    )
    if resp.status_code >= 400:
        raise _qbo_error(resp)
    _audit("delete", entry_id, deleted, request)
    return {"deleted": entry_id}


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
    start = (date.today() - timedelta(days=days)).isoformat()
    rows = qbo_query_all("TimeActivity", f"WHERE TxnDate >= '{start}' ORDERBY TxnDate DESC")
    total = with_rate = 0
    combos = {}  # (person, service) -> {"n":int, "withrate":int, "rates":set}
    for t in rows:
        who = (t.get("EmployeeRef") or t.get("VendorRef") or {}).get("name") or "(none)"
        svc = (t.get("ItemRef") or {}).get("name") or "(none)"
        rate = t.get("HourlyRate")
        try:
            rate = float(rate) if rate not in (None, "") else 0.0
        except (TypeError, ValueError):
            rate = 0.0
        has = rate > 0
        total += 1
        with_rate += 1 if has else 0
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
            "rates": sorted(c["rates"]),
        })
    return {
        "days": days,
        "examined": total,
        "withRate": with_rate,
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
# REMINDER_TZ (default America/Los_Angeles), REMINDER_WEEKDAYS_ONLY (default 1).
# ---------------------------------------------------------------------------
REMINDER_HOUR = int(os.environ.get("REMINDER_HOUR", "17"))
REMINDER_TZ = os.environ.get("REMINDER_TZ", "America/Los_Angeles")
REMINDER_WEEKDAYS_ONLY = os.environ.get("REMINDER_WEEKDAYS_ONLY", "1") != "0"
_push_lock = threading.Lock()


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
    endpoint = sub.get("endpoint")
    if not endpoint:
        return True
    try:
        resp = requests.post(
            endpoint,
            headers={
                "Authorization": f"vapid t={_vapid_jwt(endpoint)},k={_vapid()['app_key']}",
                "TTL": "86400",
            },
            timeout=15,
        )
        if resp.status_code in (404, 410):
            return False
        if resp.status_code >= 400:
            log.warning("push send %s: %s", resp.status_code, resp.text[:200])
        return True
    except Exception as e:
        log.warning("push send error: %s", e)
        return True


def _notify_all():
    """Send the reminder to every subscribed device; prune dead subs."""
    push = _load_push()
    subs = push.get("subs", [])
    if not subs:
        return 0
    alive = [s for s in subs if _send_push(s)]
    if len(alive) != len(subs):
        push["subs"] = alive
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
    push = _load_push()
    if not push.get("subs"):
        return
    now = datetime.now(ZoneInfo(REMINDER_TZ))
    today = now.date().isoformat()
    if push.get("last_reminder") == today:
        return
    if now.hour < REMINDER_HOUR:
        return
    if REMINDER_WEEKDAYS_ONLY and now.weekday() >= 5:
        return
    # one QBO check per day, inside the window
    push["last_reminder"] = today
    _save_push(push)
    if not _logged_time_today(today):
        n = _notify_all()
        log.info("daily reminder sent to %d device(s)", n)


def _reminder_loop():
    while True:
        try:
            _reminder_tick()
        except Exception as e:
            log.warning("reminder tick error: %s", e)
        time.sleep(900)  # every 15 min


class PushSub(BaseModel):
    endpoint: str
    keys: dict | None = None


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
