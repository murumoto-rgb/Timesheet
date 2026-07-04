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
import urllib.parse
from datetime import date, timedelta

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

app = FastAPI(title="QBO Timesheet")
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
_PUBLIC_PATHS = {"/", "/login", "/api/status", "/eula", "/privacy"}


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


def _load_tokens():
    if SUPABASE_URL and SUPABASE_KEY:
        resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/qbo_tokens",
            params={"id": "eq.1", "select": "data"},
            headers=_sb_headers(),
            timeout=15,
        )
        resp.raise_for_status()
        rows = resp.json()
        return rows[0]["data"] if rows else None
    if not os.path.exists(TOKENS_FILE):
        return None
    with open(TOKENS_FILE) as f:
        return json.load(f)


def _save_tokens(data):
    if SUPABASE_URL and SUPABASE_KEY:
        resp = requests.post(
            f"{SUPABASE_URL}/rest/v1/qbo_tokens",
            headers={**_sb_headers(), "Prefer": "resolution=merge-duplicates"},
            json=[{"id": 1, "data": data}],
            timeout=15,
        )
        resp.raise_for_status()
        return
    with open(TOKENS_FILE, "w") as f:
        json.dump(data, f, indent=2)


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


def get_access_token():
    """Return (access_token, realm_id), refreshing the access token if stale."""
    tokens = _load_tokens()
    if not tokens:
        raise HTTPException(401, "Not connected. Open / and click Connect QuickBooks.")
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
    # no-cache so a restarted server always serves the freshly pulled page
    return FileResponse(
        os.path.join(BASE_DIR, "index.html"),
        headers={"Cache-Control": "no-cache"},
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
@app.get("/api/projects")
def list_projects():
    """Return QBO Projects (with their parent customer) and all other customers
    — top-level clients AND sub-customers/jobs (shown by qualified name)."""
    rows = qbo_query("SELECT * FROM Customer WHERE Active = true MAXRESULTS 1000")
    customers = rows.get("Customer", [])
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
    rows = qbo_query("SELECT * FROM Employee WHERE Active = true MAXRESULTS 1000")
    return [{"id": e["Id"], "name": e.get("DisplayName")} for e in rows.get("Employee", [])]


@app.get("/api/vendors")
def list_vendors():
    rows = qbo_query("SELECT * FROM Vendor WHERE Active = true MAXRESULTS 1000")
    return [{"id": v["Id"], "name": v.get("DisplayName")} for v in rows.get("Vendor", [])]


@app.get("/api/items")
def list_items():
    rows = qbo_query(
        "SELECT * FROM Item WHERE Type = 'Service' AND Active = true MAXRESULTS 1000"
    )
    return [{"id": i["Id"], "name": i.get("Name")} for i in rows.get("Item", [])]


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
    # With Projects enabled, set ProjectRef + the parent CustomerRef.
    if entry.project_id:
        payload["ProjectRef"] = {"value": entry.project_id}
        if entry.customer_id:
            payload["CustomerRef"] = {"value": entry.customer_id}
    elif entry.customer_id:
        payload["CustomerRef"] = {"value": entry.customer_id}

    if entry.billable and entry.customer_id:
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
def create_time(entry: TimeEntry):
    if not (entry.employee_id or entry.vendor_id):
        raise HTTPException(400, "Pick an employee or vendor.")
    return _post_timeactivity(_timeactivity_payload(entry))


@app.put("/api/timeactivity/{entry_id}")
def update_time(entry_id: str, entry: TimeEntry):
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
    payload = _timeactivity_payload(entry)
    payload["Id"] = entry_id
    payload["SyncToken"] = read.json()["TimeActivity"]["SyncToken"]
    return _post_timeactivity(payload)


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
    rows = qbo_query(
        f"SELECT * FROM TimeActivity WHERE {where} ORDERBY TxnDate DESC MAXRESULTS 1000"
    )
    entries = []
    for t in rows.get("TimeActivity", []):
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
                "billable": t.get("BillableStatus") == "Billable",
                "hourlyRate": t.get("HourlyRate"),
            }
        )
    return entries


@app.delete("/api/timeactivity/{entry_id}")
def delete_time(entry_id: str):
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
    sync_token = read.json()["TimeActivity"]["SyncToken"]

    resp = requests.post(
        f"{API_BASE}/v3/company/{realm_id}/timeactivity",
        headers={**headers, "Content-Type": "application/json"},
        params={"operation": "delete", "minorversion": MINOR_VERSION},
        json={"Id": entry_id, "SyncToken": sync_token},
        timeout=30,
    )
    if resp.status_code >= 400:
        raise _qbo_error(resp)
    return {"deleted": entry_id}
