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
import hashlib
import secrets
import urllib.parse
from datetime import date, timedelta

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CLIENT_ID = os.environ.get("QBO_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("QBO_CLIENT_SECRET", "")
REDIRECT_URI = os.environ.get("QBO_REDIRECT_URI", "http://localhost:8000/callback")
ENVIRONMENT = os.environ.get("QBO_ENVIRONMENT", "sandbox").lower()

AUTH_URL = "https://appcenter.intuit.com/connect/oauth2"
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
SCOPE = "com.intuit.quickbooks.accounting"

API_BASE = (
    "https://sandbox-quickbooks.api.intuit.com"
    if ENVIRONMENT == "sandbox"
    else "https://quickbooks.api.intuit.com"
)
# Since 2025-08-01 Intuit ignores minorversion < 75; 75 is the base version.
MINOR_VERSION = "75"
TOKENS_FILE = os.path.join(BASE_DIR, os.environ.get("QBO_TOKENS_FILE", "qbo_tokens.json"))

app = FastAPI(title="QBO Timesheet")
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
_pending_states: set[str] = set()  # CSRF state (fine for a single local user)


# ---------------------------------------------------------------------------
# Password gate (only active when APP_PASSWORD is set — required for hosting)
# ---------------------------------------------------------------------------
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
_PUBLIC_PATHS = {"/", "/login", "/api/status", "/eula", "/privacy"}


def _auth_cookie_value():
    key = hashlib.sha256(f"{APP_PASSWORD}:{CLIENT_SECRET}".encode()).digest()
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
            return JSONResponse({"detail": "Sign in required."}, status_code=401)
    return await call_next(request)


class Login(BaseModel):
    password: str


@app.post("/login")
def login(body: Login, request: Request):
    if APP_PASSWORD and not hmac.compare_digest(body.password, APP_PASSWORD):
        raise HTTPException(401, "Wrong password.")
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
        TOKEN_URL,
        headers={
            "Authorization": _basic_auth_header(),
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        data=payload,
        timeout=30,
    )
    if resp.status_code >= 400:
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
        raise HTTPException(resp.status_code, resp.text)
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
        "authed": _is_authed(request),
    }


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
    return RedirectResponse(f"{AUTH_URL}?{urllib.parse.urlencode(params)}")


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
    """Return QBO Projects (with their parent customer) and top-level clients."""
    rows = qbo_query("SELECT * FROM Customer WHERE Active = true MAXRESULTS 1000")
    customers = rows.get("Customer", [])
    projects, clients = [], []
    for c in customers:
        if c.get("IsProject"):
            parent = c.get("ParentRef", {})
            projects.append(
                {
                    "id": c["Id"],
                    "name": c.get("FullyQualifiedName", c.get("DisplayName")),
                    "parentId": parent.get("value"),
                }
            )
        elif not c.get("Job"):  # a plain top-level customer, not a sub-customer/job
            clients.append({"id": c["Id"], "name": c.get("DisplayName")})
    return {"projects": projects, "clients": clients}


@app.get("/api/employees")
def list_employees():
    rows = qbo_query("SELECT * FROM Employee WHERE Active = true MAXRESULTS 1000")
    return [{"id": e["Id"], "name": e.get("DisplayName")} for e in rows.get("Employee", [])]


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
    employee_id: str
    item_id: str
    hours: int = 0
    minutes: int = 0
    description: str = ""
    txn_date: str | None = None
    billable: bool = False
    hourly_rate: float | None = None
    project_id: str | None = None
    customer_id: str | None = None  # project's parent, or the client itself


@app.post("/api/timeactivity")
def create_time(entry: TimeEntry):
    access_token, realm_id = get_access_token()

    payload = {
        "NameOf": "Employee",
        "EmployeeRef": {"value": entry.employee_id},
        "ItemRef": {"value": entry.item_id},  # required on create
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

    resp = requests.post(
        f"{API_BASE}/v3/company/{realm_id}/timeactivity",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        params={"minorversion": MINOR_VERSION},
        json=payload,
        timeout=30,
    )
    if resp.status_code >= 400:
        # Surface QBO's fault message so validation errors are readable.
        raise HTTPException(resp.status_code, resp.text)
    return resp.json().get("TimeActivity", resp.json())


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
                # CustomerRef carries the project's name when the entry is on a project.
                "customer": (t.get("CustomerRef") or {}).get("name"),
                "projectId": (t.get("ProjectRef") or {}).get("value"),
                "billable": t.get("BillableStatus") == "Billable",
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
        raise HTTPException(read.status_code, read.text)
    sync_token = read.json()["TimeActivity"]["SyncToken"]

    resp = requests.post(
        f"{API_BASE}/v3/company/{realm_id}/timeactivity",
        headers={**headers, "Content-Type": "application/json"},
        params={"operation": "delete", "minorversion": MINOR_VERSION},
        json={"Id": entry_id, "SyncToken": sync_token},
        timeout=30,
    )
    if resp.status_code >= 400:
        raise HTTPException(resp.status_code, resp.text)
    return {"deleted": entry_id}
