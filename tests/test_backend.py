"""Backend unit tests. QBO HTTP is never hit — the query layer is monkeypatched.
Run: pytest -q  (from the repo root)."""
import pytest
from fastapi import HTTPException

import main
from main import TimeEntry


# ── _timeactivity_payload: the field rules that matter for QBO ──────────────
def test_payload_attaches_time_to_project_not_parent():
    e = TimeEntry(item_id="5", employee_id="55", hours=2, minutes=30,
                  txn_date="2026-07-03", billable=True, project_id="416", customer_id="10")
    p = main._timeactivity_payload(e)
    assert p["NameOf"] == "Employee"
    assert p["EmployeeRef"] == {"value": "55"}
    assert p["ItemRef"] == {"value": "5"}            # ItemRef required on create
    assert p["CustomerRef"] == {"value": "416"}      # project id, NOT the parent customer_id
    assert "ProjectRef" not in p                     # never send ProjectRef (9341)
    assert p["BillableStatus"] == "Billable"
    assert (p["Hours"], p["Minutes"]) == (2, 30)
    assert p["TxnDate"] == "2026-07-03"


def test_payload_vendor_and_nonbillable():
    e = TimeEntry(item_id="5", vendor_id="9", hours=1, billable=False, customer_id="10")
    p = main._timeactivity_payload(e)
    assert p["NameOf"] == "Vendor"
    assert p["VendorRef"] == {"value": "9"}
    assert "EmployeeRef" not in p
    assert p["BillableStatus"] == "NotBillable"


def test_payload_billable_requires_a_customer_ref():
    # billable=True but no project/customer → cannot be Billable (QBO rejects)
    e = TimeEntry(item_id="5", employee_id="55", billable=True)
    p = main._timeactivity_payload(e)
    assert "CustomerRef" not in p
    assert p["BillableStatus"] == "NotBillable"


def test_payload_hourly_rate_only_when_billable():
    e = TimeEntry(item_id="5", employee_id="55", billable=True, customer_id="10", hourly_rate=250)
    p = main._timeactivity_payload(e)
    assert p["HourlyRate"] == 250


# ── qbo_query_all: pagination past the 1000-row page cap ────────────────────
def test_qbo_query_all_paginates_past_1000(monkeypatch):
    import re
    TOTAL = 2300

    def fake_query(stmt):
        sp = int(re.search(r"STARTPOSITION (\d+)", stmt).group(1))
        mr = int(re.search(r"MAXRESULTS (\d+)", stmt).group(1))
        rows = [{"Id": str(i)} for i in range(sp - 1, min(sp - 1 + mr, TOTAL))]
        return {"TimeActivity": rows}

    monkeypatch.setattr(main, "qbo_query", fake_query)
    out = main.qbo_query_all("TimeActivity", "WHERE TxnDate >= '2025-01-01'")
    assert len(out) == TOTAL
    assert len({r["Id"] for r in out}) == TOTAL     # no dupes, nothing dropped


# ── audit trail ─────────────────────────────────────────────────────────────
def test_ta_summary_reads_names():
    ta = {"TxnDate": "2026-07-05", "Hours": 2, "Minutes": 15,
          "EmployeeRef": {"value": "55", "name": "Murat Baykal"},
          "ItemRef": {"name": "PR"}, "CustomerRef": {"name": "Alpha"},
          "BillableStatus": "Billable", "Description": "review"}
    s = main._ta_summary(ta)
    assert s == {"date": "2026-07-05", "hours": 2, "minutes": 15, "who": "Murat Baykal",
                 "service": "PR", "customer": "Alpha", "billableStatus": "Billable",
                 "description": "review"}


def test_audit_appends_and_never_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "AUDIT_FILE", str(tmp_path / "aud.json"))
    ta = {"TxnDate": "2026-07-05", "Hours": 1, "EmployeeRef": {"name": "Murat"},
          "ItemRef": {"name": "PR"}, "CustomerRef": {"name": "A"}, "BillableStatus": "Billable"}
    main._audit("create", "101", ta)
    main._audit("delete", "101", ta)
    events = main._load_audit()["events"]
    assert [e["action"] for e in events] == ["create", "delete"]
    assert events[0]["entryId"] == "101" and "summary" in events[0]
    # a broken summary object must not raise
    main._audit("create", "x", None)


# ── rate check aggregation ──────────────────────────────────────────────────
def test_ratecheck_counts_rate_coverage(monkeypatch):
    rows = [
        {"Id": "1", "TxnDate": "2026-06-01", "Hours": 2, "EmployeeRef": {"name": "Murat"}, "ItemRef": {"name": "PR"}, "HourlyRate": 250},
        {"Id": "2", "TxnDate": "2026-06-02", "Hours": 1, "EmployeeRef": {"name": "Murat"}, "ItemRef": {"name": "PR"}, "HourlyRate": 250},
        {"Id": "3", "TxnDate": "2026-06-03", "Hours": 1, "EmployeeRef": {"name": "Murat"}, "ItemRef": {"name": "MTG"}},  # no rate
    ]
    monkeypatch.setattr(main, "qbo_query_all", lambda *a, **k: rows)
    d = main._ratecheck()
    assert d["examined"] == 3
    assert d["withRate"] == 2
    assert d["distinctRates"] == [250.0]
    combos = {(r["person"], r["service"]): r for r in d["byPersonService"]}
    assert combos[("Murat", "MTG")]["withRate"] == 0


# ── payments: Payment + SalesReceipt merge ──────────────────────────────────
def test_list_payments_merges_payment_and_salesreceipt(monkeypatch):
    def q(entity, where="", key=None):
        if entity == "Payment":
            return [{"Id": "p1", "TxnDate": "2026-06-01", "TotalAmt": 5000, "CustomerRef": {"name": "A"}}]
        if entity == "SalesReceipt":
            return [{"Id": "s1", "TxnDate": "2026-06-02", "TotalAmt": 800, "CustomerRef": {"name": "B"}}]
        return []

    monkeypatch.setattr(main, "qbo_query_all", q)
    out = main.list_payments(start="2026-01-01", end="2026-12-31")
    assert len(out) == 2
    assert sum(x["amount"] for x in out) == 5800
    assert {x["kind"] for x in out} == {"payment", "salesreceipt"}


# ── date-range validation ───────────────────────────────────────────────────
def test_list_time_rejects_bad_dates(monkeypatch):
    monkeypatch.setattr(main, "qbo_query_all", lambda *a, **k: [])
    with pytest.raises(HTTPException):
        main.list_time(start="2026-13-99")
    with pytest.raises(HTTPException):
        main.list_time(start="not-a-date")
    # a valid range must NOT raise
    assert main.list_time(start="2026-01-01", end="2026-12-31") == []
