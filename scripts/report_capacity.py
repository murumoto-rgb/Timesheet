#!/usr/bin/env python3
"""Build and measure a synthetic-only Timesheet report capacity bundle.

No QuickBooks or Postgres service is contacted. The generated ``main.py``
preserves the app's real password gate and report code while restricting routes
and supplying deterministic paginated provider data. Run with
``python -m scripts.report_capacity build`` or ``local``.

The optional ``local`` subcommand uses FastAPI TestClient. With the pinned
Starlette release, install ``httpx2==2.13.1`` into the local test environment if
TestClient reports that its transport is missing; this is not a runtime dependency.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any
import warnings

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_FILES = {
    "main.py", "write_journal.py", "time_reconciliation.py", "postgres_store.py",
    "requirements.txt", "requirements.lock", "index.html", ".python-version",
}
MAX_ROWS = 100_001
MAX_DESCRIPTION_BYTES = 4_000
MAX_DELAY_MS = 1_000
REPORT_PATHS = {
    "/api/timeactivities", "/api/payments", "/api/bills", "/api/receivables",
    "/api/project-financials", "/api/reconciliation", "/api/audit", "/api/ratecheck",
    "/api/projects", "/api/company", "/api/employees", "/api/vendors", "/api/items",
    "/audit", "/ratecheck",
}
PUBLIC_GET_PATHS = {
    "/", "/login", "/api/status", "/api/health", "/eula", "/privacy", "/sw.js",
}
DEFAULT_ROWS = (999, 1000, 1001, 5000, 10000, 25000)


class CapacityError(ValueError):
    pass


def _tracked_files() -> list[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "ls-files", "-z"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
    except Exception:
        raise CapacityError("could not list tracked runtime files") from None
    tracked = {item.decode("utf-8") for item in result.stdout.split(b"\0") if item}
    selected = set(RUNTIME_FILES)
    selected.update(name for name in tracked if name.startswith("static/"))
    missing = sorted(selected - tracked)
    if missing:
        raise CapacityError("tracked runtime input is missing")
    return sorted(selected)


def _vercel_config() -> dict[str, Any]:
    return {
        "$schema": "https://openapi.vercel.sh/vercel.json",
        "framework": "fastapi",
        "regions": ["cle1"],
        "functions": {"main.py": {"maxDuration": 300}},
        "crons": [],
    }


def _bundle_wrapper() -> str:
    return r'''"""Synthetic report-capacity wrapper. Never connects to QBO or Postgres."""
from __future__ import annotations
import contextvars
import math
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from urllib.parse import urlparse

import requests
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

# Never load credentials from dotenv or inherited QBO/database settings.
os.environ["PYTHON_DOTENV_DISABLED"] = "true"
for _name in ("QBO_CLIENT_ID", "QBO_CLIENT_SECRET", "TIMESHEET_ENCRYPTION_KEY",
              "SUPABASE_URL", "SUPABASE_SERVICE_KEY", "QBO_TOKENS_FILE"):
    os.environ.pop(_name, None)
os.environ["QBO_ENVIRONMENT"] = "sandbox"
os.environ["DATABASE_URL"] = "postgresql://capacity.invalid/timesheet"
os.environ.setdefault("VERCEL", "1")
os.environ.setdefault("VERCEL_ENV", "preview")
if os.environ.get("CAPACITY_LOCAL_RUN") == "1":
    os.environ["VERCEL"] = "1"
    os.environ["VERCEL_ENV"] = "preview"
    os.environ["TIMESHEET_PREVIEW_READ_ONLY"] = "1"
    if not os.environ.get("APP_PASSWORD"):
        os.environ["APP_PASSWORD"] = "capacity-test-only"

import capacity_source as _source

_REALM = "CAPACITY-SYNTHETIC-REALM"
_ACCESS = "capacity-synthetic-access-token"
_PROJECTS = tuple(f"PROJECT-{i:02d}" for i in range(1, 11))
_PEOPLE = tuple(f"Person {i:02d}" for i in range(1, 33))
_SERVICES = tuple(f"Service {i:02d}" for i in range(1, 13))
_REPORT_PATHS = {
    "/api/timeactivities", "/api/payments", "/api/bills", "/api/receivables",
    "/api/project-financials", "/api/reconciliation", "/api/audit", "/api/ratecheck",
    "/api/projects", "/api/company", "/api/employees", "/api/vendors", "/api/items",
    "/audit", "/ratecheck",
}
_PUBLIC_GET_PATHS = {"/", "/login", "/api/status", "/api/health", "/eula", "/privacy", "/sw.js"}
_CONFIG = contextvars.ContextVar("capacity_config", default=None)

@dataclass
class _RequestConfig:
    rows: int
    description_bytes: int
    delay_ms: int
    calls: int = 0
    provider_ms: float = 0.0
    cache: dict = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)
    def add_call(self, elapsed):
        with self.lock:
            self.calls += 1
            self.provider_ms += elapsed
    def totals(self):
        with self.lock:
            return self.calls, round(self.provider_ms, 2)

def _int_header(request, name, default, maximum):
    raw = request.headers.get(name)
    if raw is None:
        return default
    if not re.fullmatch(r"\d{1,7}", raw):
        raise ValueError
    number = int(raw)
    if number > maximum:
        raise ValueError
    return number

def _allowed(method, path):
    if method == "POST" and path == "/login":
        return True
    if method != "GET":
        return False
    return path in _PUBLIC_GET_PATHS or path in _REPORT_PATHS or path.startswith("/static/")

@_source.app.middleware("http")
async def capacity_guard(request: Request, call_next):
    if not _allowed(request.method, request.url.path):
        return JSONResponse({"detail": "Synthetic capacity harness route blocked."}, status_code=403)
    try:
        config = _RequestConfig(
            _int_header(request, "x-capacity-rows", 1000, 100001),
            _int_header(request, "x-capacity-description-bytes", 128, 4000),
            _int_header(request, "x-capacity-delay-ms", 0, 1000),
        )
    except ValueError:
        return JSONResponse({"detail": "Capacity header is outside its permitted range."}, status_code=400)
    token = _CONFIG.set(config)
    try:
        response = await call_next(request)
        calls, provider_ms = config.totals()
        response.headers["x-capacity-provider-calls"] = str(calls)
        response.headers["x-capacity-provider-milliseconds"] = str(provider_ms)
        return response
    finally:
        _CONFIG.reset(token)

def _date(index):
    return (date(2026, 1, 1) + timedelta(days=(index - 1) % 273)).isoformat()

def _description(index, requested):
    prefix = f"Synthetic work {index:08d}"
    wanted = max(len(prefix), requested)
    return prefix + ("x" * (wanted - len(prefix)))

def _time_rows(count, description_bytes):
    rows = []
    for index in range(1, count + 1):
        person = _PEOPLE[(index - 1) % len(_PEOPLE)]
        person_id = f"EMP-{((index - 1) % len(_PEOPLE)) + 1:02d}"
        project_index = ((index - 1) % len(_PROJECTS))
        project = _PROJECTS[project_index]
        service_index = (index - 1) % len(_SERVICES)
        row = {
            "Id": f"TA-{index:08d}", "SyncToken": "0", "TxnDate": _date(index),
            "Hours": 1, "Minutes": 17, "Description": _description(index, description_bytes),
            "EmployeeRef": {"value": person_id, "name": person},
            "ItemRef": {"value": f"ITEM-{service_index + 1:02d}", "name": _SERVICES[service_index]},
            "CustomerRef": {"value": project, "name": f"Synthetic Client {project_index + 1:02d}: {project}"},
            "ProjectRef": {"value": project}, "BillableStatus": ("Billable", "HasBeenBilled", "NotBillable")[(index - 1) % 3],
            "HourlyRate": 125.0,
        }
        rows.append(row)
    return rows

def _invoice_rows(count):
    rows = []
    for index in range(1, count + 1):
        project_index = (index - 1) % len(_PROJECTS)
        amount = 100.0 + (index % 100)
        rows.append({
            "Id": f"INV-{index:08d}", "DocNumber": f"SYN-{index:08d}", "TxnDate": _date(index),
            "DueDate": (date.fromisoformat(_date(index)) + timedelta(days=30)).isoformat(),
            "TotalAmt": amount, "Balance": 50.0 + (index % 25) if index % 2 == 0 else 0.0,
            "CustomerRef": {"value": _PROJECTS[project_index], "name": f"Synthetic Project {project_index + 1:02d}"},
            "CurrencyRef": {"value": "USD"},
        })
    return rows

def _rows_for(entity, config):
    cached = config.cache.get(entity)
    if cached is not None:
        return cached
    n = config.rows
    if entity == "TimeActivity":
        rows = _time_rows(n, config.description_bytes)
    elif entity == "Invoice":
        rows = _invoice_rows(n)
    elif entity == "Payment":
        rows = []
        for index in range(1, n + 1):
            invoice_id = f"INV-{index:08d}"
            rows.append({
                "Id": f"PAY-{index:08d}", "TxnDate": _date(index), "TotalAmt": float(10 + index % 97),
                "CustomerRef": {"value": _PROJECTS[(index - 1) % 10]}, "CurrencyRef": {"value": "USD"},
                "Line": [{"Id": f"LINE-{index:08d}", "Amount": float(40 + index % 21),
                          "LinkedTxn": [{"TxnId": invoice_id, "TxnType": "Invoice"}]}],
            })
    elif entity == "SalesReceipt":
        rows = [{"Id": f"SR-{i:08d}", "TxnDate": _date(i), "TotalAmt": float(5 + i % 53),
                 "CustomerRef": {"value": _PROJECTS[(i - 1) % 10]}} for i in range(1, n + 1)]
    elif entity == "Bill":
        rows = [{"Id": f"BILL-{i:08d}", "TxnDate": _date(i), "TotalAmt": float(20 + i % 41),
                 "VendorRef": {"value": f"VEN-{((i - 1) % 32) + 1:02d}", "name": _PEOPLE[(i - 1) % 32]}}
                for i in range(1, n + 1)]
    elif entity == "Purchase":
        rows = [{"Id": f"PUR-{i:08d}", "TxnDate": _date(i), "TotalAmt": float(7 + i % 37),
                 "Credit": i % 11 == 0,
                 "EntityRef": {"value": f"VEN-{((i - 1) % 32) + 1:02d}", "name": _PEOPLE[(i - 1) % 32]}}
                for i in range(1, n + 1)]
    elif entity == "Customer":
        rows = []
        for i, project in enumerate(_PROJECTS, 1):
            rows.append({"Id": project, "DisplayName": project, "FullyQualifiedName": f"Synthetic Client {i:02d}: {project}",
                         "IsProject": True, "Active": True, "ParentRef": {"value": f"CLIENT-{i:02d}"}})
        rows.extend({"Id": f"CLIENT-{i:02d}", "DisplayName": f"Synthetic Client {i:02d}",
                     "FullyQualifiedName": f"Synthetic Client {i:02d}", "IsProject": False, "Active": True}
                    for i in range(1, 11))
    elif entity == "Employee":
        rows = [{"Id": f"EMP-{i:02d}", "DisplayName": _PEOPLE[i - 1], "Active": True} for i in range(1, 33)]
    elif entity == "Vendor":
        rows = [{"Id": f"VEN-{i:02d}", "DisplayName": _PEOPLE[i - 1], "Active": True} for i in range(1, 33)]
    elif entity == "Item":
        rows = [{"Id": f"ITEM-{i:02d}", "Name": _SERVICES[i - 1], "Type": "Service", "Active": True}
                for i in range(1, 13)]
    elif entity == "CompanyInfo":
        rows = [{"CompanyName": "Synthetic Capacity Practice", "LegalName": "Synthetic Capacity Practice LLC"}]
    else:
        rows = []
    config.cache[entity] = rows
    return rows

_SELECT = re.compile(r"\bSELECT\s+\*\s+FROM\s+([A-Za-z][A-Za-z0-9]*)", re.I)
_DATE_GTE = re.compile(r"\bTxnDate\s*>=\s*'([^']+)'", re.I)
_DATE_LTE = re.compile(r"\bTxnDate\s*<=\s*'([^']+)'", re.I)
_START = re.compile(r"\bSTARTPOSITION\s+(\d+)", re.I)
_MAX = re.compile(r"\bMAXRESULTS\s+(\d+)", re.I)

def _fake_get(url, *, params=None, **_kwargs):
    began = time.perf_counter()
    config = _CONFIG.get()
    if config is None or not url.startswith(_source.API_BASE + "/v3/company/"):
        raise requests.RequestException("external GET disabled in capacity harness")
    query = (params or {}).get("query", "")
    match = _SELECT.search(query)
    if not match:
        raise requests.RequestException("unrecognized synthetic QBO query")
    entity = match.group(1)
    rows = _rows_for(entity, config)
    lower = _DATE_GTE.search(query)
    upper = _DATE_LTE.search(query)
    if lower:
        rows = [row for row in rows if row.get("TxnDate", "") >= lower.group(1)]
    if upper:
        rows = [row for row in rows if row.get("TxnDate", "") <= upper.group(1)]
    if re.search(r"\bBalance\s*>\s*'0'", query, re.I):
        rows = [row for row in rows if float(row.get("Balance", 0)) > 0]
    if re.search(r"\bORDERBY\s+TxnDate\s+DESC", query, re.I):
        rows = sorted(rows, key=lambda row: row.get("TxnDate", ""), reverse=True)
    start = int((_START.search(query) or [None, "1"])[1])
    limit = int((_MAX.search(query) or [None, "1000"])[1])
    page = rows[start - 1:start - 1 + limit]
    if config.delay_ms:
        time.sleep(config.delay_ms / 1000)
    config.add_call((time.perf_counter() - began) * 1000)
    response = type("SyntheticResponse", (), {})()
    response.status_code = 200
    response.headers = {}
    response.json = lambda: {"QueryResponse": {entity: page}}
    return response

def _blocked_post(*_args, **_kwargs):
    raise requests.RequestException("external POST disabled in capacity harness")

_source.requests.get = _fake_get
_source.requests.post = _blocked_post
requests.sessions.Session.request = _blocked_post
_source._postgres = lambda: None
_source._load_tokens = lambda: {
    "realm_id": _REALM, "access_token": _ACCESS, "refresh_token": "capacity-synthetic-refresh",
    "access_expires_at": time.time() + 3600,
}
_source._save_tokens = lambda _value: None
_source.get_access_token = lambda: (_ACCESS, _REALM)
_source._write_scope = lambda: f"{_source.API_BASE}|{_REALM}"
_source._load_audit = lambda: {"events": []}
_source._load_operations = lambda: {"revision": 0, "operations": []}
_source._today = lambda: date(2026, 10, 6)
_login_memory = {}
_source._login_state = lambda: _login_memory
_source._save_login_state = lambda value: (_login_memory.clear(), _login_memory.update(value))
from contextlib import contextmanager as _contextmanager
@_contextmanager
def _memory_lock(_name, local_lock):
    with local_lock:
        yield
_source._storage_lock = _memory_lock
app = _source.app
APP_PASSWORD = _source.APP_PASSWORD
'''


def build_bundle(destination: Path, *, copy_vercel_link: bool = False) -> dict[str, Any]:
    """Copy tracked runtime inputs into a new, sanitized Vercel bundle."""
    destination = Path(destination)
    if destination.is_symlink():
        raise CapacityError("bundle destination must not be a symbolic link")
    resolved = destination.resolve()
    try:
        resolved.relative_to(REPO_ROOT)
    except ValueError:
        pass
    else:
        raise CapacityError("bundle destination must be outside the repository")
    if resolved.exists():
        raise CapacityError("bundle destination must be new and empty")
    try:
        resolved.mkdir(mode=0o700, parents=True, exist_ok=False)
        paths = _tracked_files()
        hashes = {}
        for relative in paths:
            source = REPO_ROOT / relative
            if source.is_symlink() or not source.is_file():
                raise CapacityError("runtime input is missing or unsafe")
            data = source.read_bytes()
            output_name = "capacity_source.py" if relative == "main.py" else relative
            target = resolved / output_name
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            target.write_bytes(data)
            hashes[output_name] = hashlib.sha256(data).hexdigest()
        wrapper = _bundle_wrapper().encode("utf-8")
        (resolved / "main.py").write_bytes(wrapper)
        hashes["main.py"] = hashlib.sha256(wrapper).hexdigest()
        vercel = (json.dumps(_vercel_config(), indent=2) + "\n").encode("utf-8")
        (resolved / "vercel.json").write_bytes(vercel)
        hashes["vercel.json"] = hashlib.sha256(vercel).hexdigest()
        if copy_vercel_link:
            link_source = REPO_ROOT / ".vercel" / "project.json"
            if link_source.is_symlink() or not link_source.is_file():
                raise CapacityError("Vercel preview project link is not available")
            link = json.loads(link_source.read_text())
            if not isinstance(link, dict) or not link.get("projectId") or not link.get("orgId"):
                raise CapacityError("Vercel preview project link is invalid")
            link_dir = resolved / ".vercel"
            link_dir.mkdir(mode=0o700)
            link_data = (json.dumps(link, indent=2) + "\n").encode()
            (link_dir / "project.json").write_bytes(link_data)
            hashes[".vercel/project.json"] = hashlib.sha256(link_data).hexdigest()
        manifest = {
            "format": 1,
            "source_main_sha256": hashes["capacity_source.py"],
            "files": [{"name": name, "sha256": hashes[name]} for name in sorted(hashes)],
            "crons": [],
        }
        manifest_data = (json.dumps(manifest, indent=2) + "\n").encode()
        (resolved / "capacity_manifest.json").write_bytes(manifest_data)
        return {"files": len(hashes), "source_sha256": hashes["capacity_source.py"],
                "manifest_sha256": hashlib.sha256(manifest_data).hexdigest()}
    except CapacityError:
        shutil.rmtree(resolved, ignore_errors=True)
        raise
    except Exception:
        shutil.rmtree(resolved, ignore_errors=True)
        raise CapacityError("bundle could not be created") from None


def _parse_rows(raw: str) -> list[int]:
    try:
        values = [int(part.strip()) for part in raw.split(",")]
    except (TypeError, ValueError):
        raise CapacityError("rows must be comma-separated integers") from None
    if not values or any(value < 0 or value > MAX_ROWS for value in values):
        raise CapacityError("rows must be between zero and 100001")
    return values


def _load_bundled_app(bundle: Path):
    sys.path.insert(0, str(bundle))
    os.environ["CAPACITY_LOCAL_RUN"] = "1"
    os.environ["PYTHON_DOTENV_DISABLED"] = "true"
    try:
        sys.modules.pop("main", None)
        sys.modules.pop("capacity_source", None)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            logging.getLogger("httpx").setLevel(logging.WARNING)
            return importlib.import_module("main")
    finally:
        os.environ.pop("CAPACITY_LOCAL_RUN", None)
        sys.path.pop(0)


def _json_response(client: TestClient, path: str, headers: dict[str, str], *, params=None):
    began = time.perf_counter()
    response = client.get(path, headers=headers, params=params)
    wall_ms = round((time.perf_counter() - began) * 1000, 2)
    if response.status_code != 200:
        raise CapacityError("synthetic report request failed")
    try:
        data = response.json()
        calls = int(response.headers.get("x-capacity-provider-calls", "0"))
        provider_ms = float(response.headers.get("x-capacity-provider-milliseconds", "0"))
    except Exception:
        raise CapacityError("synthetic report response was invalid") from None
    return response, data, calls, provider_ms, wall_ms


def _sum_range(function, rows: int) -> float:
    return float(sum(function(index) for index in range(1, rows + 1)))


def _exercise(client: TestClient, rows: int, description_bytes: int, delay_ms: int,
              include_financial: bool) -> dict[str, Any]:
    headers = {
        "x-capacity-rows": str(rows),
        "x-capacity-description-bytes": str(description_bytes),
        "x-capacity-delay-ms": str(delay_ms),
    }
    date_range = {"start": "2026-01-01", "end": "2026-12-31"}
    summaries = {}

    def record(name, path, params=None):
        response, data, calls, provider_ms, wall_ms = _json_response(client, path, headers, params=params)
        summaries[name] = {"rows": len(data) if isinstance(data, list) else None,
                           "bytes": len(response.content), "provider_calls": calls,
                           "provider_ms": provider_ms, "wall_ms": wall_ms}
        return data

    time_rows = record("timeactivities", "/api/timeactivities", date_range)
    if len(time_rows) != rows:
        raise CapacityError("time activity count check failed")
    returned_ids = {row["id"] for row in time_rows}
    expected_ids = {f"TA-{index:08d}" for index in range(1, rows + 1)}
    if returned_ids != expected_ids:
        raise CapacityError("time activity ID boundary check failed")
    if sum(int(row["hours"]) * 60 + int(row["minutes"]) for row in time_rows) != rows * 77:
        raise CapacityError("time activity minute total check failed")
    if any(len(row["description"].encode("utf-8")) < description_bytes for row in time_rows):
        raise CapacityError("description length check failed")
    summaries["timeactivities"].update({"minutes": rows * 77, "first_id_checked": bool(rows),
                                         "last_id_checked": bool(rows)})

    if include_financial:
        payments = record("payments", "/api/payments", date_range)
        expected_payment_amount = _sum_range(lambda i: 10 + i % 97, rows) + _sum_range(lambda i: 5 + i % 53, rows)
        if len(payments) != rows * 2 or round(sum(float(row["amount"]) for row in payments), 2) != expected_payment_amount:
            raise CapacityError("payment count or amount total check failed")
        summaries["payments"]["amount_total"] = round(expected_payment_amount, 2)

        bills = record("bills", "/api/bills", date_range)
        purchase_count = rows - rows // 11
        expected_bills = rows + purchase_count
        expected_bill_amount = _sum_range(lambda i: 20 + i % 41, rows) + sum(
            7 + i % 37 for i in range(1, rows + 1) if i % 11 != 0
        )
        if len(bills) != expected_bills or round(sum(float(row["amount"]) for row in bills), 2) != expected_bill_amount:
            raise CapacityError("bill/purchase count or amount total check failed")
        summaries["bills"].update({"amount_total": round(expected_bill_amount, 2), "credit_purchases_excluded": rows // 11})

        receivables = record("receivables", "/api/receivables")
        expected_open = rows // 2
        if len(receivables["invoices"]) != expected_open:
            raise CapacityError("open receivable count check failed")
        billed_total = _sum_range(lambda i: 100 + i % 100, rows)
        if round(receivables["billed365"], 2) != billed_total:
            raise CapacityError("receivable billed amount total check failed")
        summaries["receivables"].update({"open_invoices": expected_open, "billed365": round(billed_total, 2)})

        project = record("project_financials", "/api/project-financials", {
            **date_range, "project_id": "PROJECT-01",
        })
        expected_project_rows = (rows + 9) // 10
        expected_project_amount = sum(40 + i % 21 for i in range(1, rows + 1) if (i - 1) % 10 == 0)
        if len(project["invoices"]) != expected_project_rows or len(project["payments"]) != expected_project_rows:
            raise CapacityError("project financial invoice/payment link check failed")
        if round(sum(float(line["amount"]) for line in project["payments"]), 2) != expected_project_amount:
            raise CapacityError("project payment amount check failed")
        summaries["project_financials"].update({"matched_invoices": expected_project_rows,
                                                 "linked_payments": expected_project_rows,
                                                 "payment_amount_total": expected_project_amount})

        reconciliation = record("reconciliation", "/api/reconciliation", date_range)
        if reconciliation["freshQbo"] != {"entries": rows, "minutes": rows * 77} or not reconciliation["readOnly"]:
            raise CapacityError("reconciliation count or minute total check failed")
        summaries["reconciliation"].update({"top_level_fields": len(reconciliation), "entries": rows, "minutes": rows * 77})
        ratecheck = record("ratecheck", "/api/ratecheck", {"days": 365})
        if ratecheck["examined"] != rows:
            raise CapacityError("ratecheck count check failed")
        summaries["ratecheck"]["examined"] = rows

    return {"requested_rows": rows, "description_bytes": description_bytes,
            "delay_ms_per_provider_call": delay_ms, "endpoints": summaries}


def run_local(rows: list[int], description_bytes: int = 128, delay_ms: int = 0,
              include_financial: bool = False, copy_vercel_link: bool = False) -> dict[str, Any]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from fastapi.testclient import TestClient
    with tempfile.TemporaryDirectory(prefix="timesheet-capacity-") as temp:
        bundle = Path(temp) / "bundle"
        build_info = build_bundle(bundle, copy_vercel_link=copy_vercel_link)
        module = _load_bundled_app(bundle)
        try:
            with TestClient(module.app) as client:
                credentials = {"password": module.APP_PASSWORD}
                if module._source.TOTP_SECRET:
                    credentials["code"] = module._source._totp(module._source.TOTP_SECRET)
                login = client.post("/login", json=credentials)
                if login.status_code != 200:
                    raise CapacityError("capacity test sign-in failed")
                for path in ("/", "/api/health", "/api/status", "/static/workspace.js"):
                    response = client.get(path)
                    if response.status_code != 200:
                        raise CapacityError("allowed app surface check failed")
                result = []
                for count in rows:
                    result.append(_exercise(client, count, description_bytes, delay_ms, include_financial))
                for path in ("/api/timeactivity", "/api/push/test", "/api/cron/reminders", "/connect"):
                    response = client.get(path)
                    if response.status_code != 403:
                        raise CapacityError("blocked route policy check failed")
                for path in ("/api/timeactivity", "/api/push/test", "/api/cron/reminders"):
                    response = client.post(path, json={})
                    if response.status_code != 403:
                        raise CapacityError("blocked write route policy check failed")
            return {"bundle": build_info, "runs": result}
        finally:
            sys.modules.pop("main", None)
            sys.modules.pop("capacity_source", None)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="create a fresh synthetic-only Vercel bundle")
    build.add_argument("--destination", type=Path, required=True)
    build.add_argument("--copy-vercel-link", action="store_true")
    local = sub.add_parser("local", help="exercise report APIs through TestClient")
    local.add_argument("--rows", default=",".join(map(str, DEFAULT_ROWS)))
    local.add_argument("--description-bytes", type=int, default=128)
    local.add_argument("--delay-ms", type=int, default=0)
    local.add_argument("--financial", action="store_true", help="also test financial and reconciliation endpoints")
    local.add_argument("--copy-vercel-link", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "build":
            result = build_bundle(args.destination, copy_vercel_link=args.copy_vercel_link)
        else:
            rows = _parse_rows(args.rows)
            if not 0 <= args.description_bytes <= MAX_DESCRIPTION_BYTES:
                raise CapacityError("description bytes must be between zero and 4000")
            if not 0 <= args.delay_ms <= MAX_DELAY_MS:
                raise CapacityError("delay milliseconds must be between zero and 1000")
            result = run_local(rows, args.description_bytes, args.delay_ms, args.financial, args.copy_vercel_link)
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except CapacityError as exc:
        print(f"capacity error: {exc}", file=sys.stderr)
        return 2
    except Exception:
        print("capacity error: operation could not be completed safely", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
