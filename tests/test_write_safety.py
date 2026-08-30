"""Write safety tests; all QuickBooks calls are mocked."""
import asyncio
import threading
import time

import pytest
import requests
from fastapi import HTTPException

import main
from main import TimeEntry


def _entry(operation_id="00000000-0000-0000-0000-000000000001", **kw):
    values = dict(item_id="svc", employee_id="emp", hours=1,
                  txn_date="2026-08-30", customer_id="client",
                  operation_id=operation_id, company_key=main._company_key((main._load_tokens() or {}).get("realm_id", "test-realm")))
    values.update(kw)
    return TimeEntry(**values)


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "OP_FILE", str(tmp_path / "operations.json"))
    monkeypatch.setattr(main, "_audit", lambda *a, **k: True)
    monkeypatch.setattr(main, "_guard_new_time", lambda *a, **k: None)
    monkeypatch.setattr(main, "_load_tokens", lambda: {"realm_id": "r"})
    return tmp_path


def test_same_id_replays_same_result_and_payload_change_is_409(isolated, monkeypatch):
    calls = []
    monkeypatch.setattr(main, "_post_timeactivity", lambda payload, params=None: (calls.append((payload, params)) or {"Id": "q1", **payload}))
    first = main.create_time(_entry(), None)
    second = main.create_time(_entry(), None)
    assert first["Id"] == second["Id"] == "q1"
    assert len(calls) == 1 and calls[0][1]["requestid"] == first["operationId"]
    with pytest.raises(HTTPException) as exc:
        main.create_time(_entry(hours=2), None)
    assert exc.value.status_code == 409


def test_concurrent_identical_create_has_one_qbo_post(isolated, monkeypatch):
    calls = []
    monkeypatch.setattr(main, "_post_timeactivity", lambda payload, params=None: (time.sleep(.03), calls.append(params["requestid"]), {"Id": "q1", **payload})[-1])
    results, errors = [], []
    def run():
        try: results.append(main.create_time(_entry(), None))
        except Exception as e: errors.append(e)
    ts = [threading.Thread(target=run) for _ in range(2)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert len(calls) == 1
    assert len(results) == 2 or len(errors) == 1


def test_timeout_retry_preserves_original_request_id(isolated, monkeypatch):
    calls = []
    def timeout(payload, params=None):
        calls.append(params["requestid"])
        raise requests.Timeout("socket closed")
    monkeypatch.setattr(main, "_post_timeactivity", timeout)
    with pytest.raises(HTTPException) as first:
        main.create_time(_entry(), None)
    assert first.value.status_code == 502
    with pytest.raises(HTTPException) as retry:
        main.create_time(_entry(), None)
    assert retry.value.status_code == 502 and len(calls) == 2
    assert calls[0] == calls[1]


def test_http_create_requires_operation_id(monkeypatch):
    entry = _entry(operation_id=None)
    with pytest.raises(HTTPException) as exc:
        main.create_time(entry, object())
    assert exc.value.status_code == 428


def test_delete_missing_and_stale_token_never_post(monkeypatch):
    before = {"Id": "e1", "SyncToken": "9", "BillableStatus": "Billable"}
    monkeypatch.setattr(main, "_read_timeactivity", lambda _id: before)
    monkeypatch.setattr(main, "_post_timeactivity", lambda *a, **k: pytest.fail("must not post"))
    class Req:
        headers = {"content-type": "application/json"}
        async def json(self): return {"company_key": main._company_key("test-realm"), "operation_id": "00000000-0000-0000-0000-000000000002"}
    with pytest.raises(HTTPException) as missing:
        asyncio.run(main.delete_time("e1", Req()))
    assert missing.value.status_code == 428
    class Stale(Req):
        async def json(self): return {"company_key": main._company_key("test-realm"), "operation_id": "00000000-0000-0000-0000-000000000003", "sync_token": "8"}
    with pytest.raises(HTTPException) as stale:
        asyncio.run(main.delete_time("e1", Stale()))
    assert stale.value.status_code == 409


def test_corrupt_journal_fails_closed(isolated, monkeypatch):
    isolated.joinpath("operations.json").write_text("not json")
    monkeypatch.setattr(main, "_post_timeactivity", lambda *a, **k: pytest.fail("must not post"))
    with pytest.raises(HTTPException) as exc:
        main.create_time(_entry(), None)
    assert exc.value.status_code == 503


def test_zero_rate_is_known_and_default_ranges_close_today(monkeypatch):
    monkeypatch.setattr(main, "_today", lambda: main.date(2026, 8, 30))
    rows = [{"HourlyRate": 0, "EmployeeRef": {"name": "E"}, "ItemRef": {"name": "S"}}]
    monkeypatch.setattr(main, "qbo_query_all", lambda *a, **k: rows)
    assert main._ratecheck()["knownRate"] == 1
    assert main._resolve_range(14, None, None) == ("2026-08-16", "2026-08-30")


def test_project_financials_only_explicit_invoice_links(monkeypatch):
    invoice = {"Id": "i1", "TxnDate": "2026-08-01", "CustomerRef": {"value": "p1"}, "TotalAmt": 1000, "Balance": 200}
    payment = {"Id": "pay", "TxnDate": "2026-08-10", "CurrencyRef": {"value": "USD"}, "Line": [{"Amount": 800, "LinkedTxn": [{"TxnId": "i1", "TxnType": "Invoice"}]}, {"Amount": 99, "LinkedTxn": [{"TxnId": "other", "TxnType": "Invoice"}]}]}
    def query(entity, *args, **kwargs): return [invoice] if entity == "Invoice" else [payment] if entity == "Payment" else []
    monkeypatch.setattr(main, "qbo_query_all", query)
    out = main.project_financials("p1", "2026-08-01", "2026-08-31")
    assert out["payments"][0]["amount"] == 800 and len(out["payments"]) == 1
