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


# ── update_time: a full-replace edit must not corrupt invoiced/rate data ────
def _mock_update(monkeypatch, before):
    """Wire update_time so it uses `before` as the existing entry and captures
    the payload actually sent to QBO (returned as `sent`)."""
    sent = {}
    monkeypatch.setattr(main, "_read_timeactivity", lambda _id: before)
    monkeypatch.setattr(main, "_post_timeactivity",
                        lambda payload, params=None: (sent.update(payload), {"Id": "77", **payload})[1])
    monkeypatch.setattr(main, "_audit", lambda *a, **k: None)
    return sent


def test_update_preserves_hasbeenbilled_and_rate(monkeypatch):
    before = {"Id": "77", "SyncToken": "3", "BillableStatus": "HasBeenBilled", "HourlyRate": 250}
    sent = _mock_update(monkeypatch, before)
    # form edits an already-invoiced billable entry; sends no rate, billable=True
    e = TimeEntry(item_id="5", employee_id="55", billable=True, customer_id="10", hours=2)
    main.update_time("77", e, request=None)
    assert sent["BillableStatus"] == "HasBeenBilled"   # not flipped back to Billable
    assert sent["HourlyRate"] == 250                    # prior rate carried, not wiped


def test_update_honors_explicit_unbill(monkeypatch):
    # unchecking billable on a previously-billed entry is a real intent → NotBillable
    before = {"Id": "77", "SyncToken": "3", "BillableStatus": "HasBeenBilled", "HourlyRate": 250}
    sent = _mock_update(monkeypatch, before)
    e = TimeEntry(item_id="5", employee_id="55", billable=False, customer_id="10", hours=2)
    main.update_time("77", e, request=None)
    assert sent["BillableStatus"] == "NotBillable"


def test_update_carries_rate_on_plain_billable_edit(monkeypatch):
    before = {"Id": "77", "SyncToken": "3", "BillableStatus": "Billable", "HourlyRate": 180}
    sent = _mock_update(monkeypatch, before)
    e = TimeEntry(item_id="5", employee_id="55", billable=True, customer_id="10", hours=1)
    main.update_time("77", e, request=None)
    assert sent["BillableStatus"] == "Billable"
    assert sent["HourlyRate"] == 180                    # not zeroed by the edit


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


# ── receivables: aging buckets, past-due, per-client, DSO ───────────────────
def test_receivables_summary_ages_and_totals():
    from datetime import date
    as_of = date(2026, 7, 5)
    invoices = [
        # 10 days old, not due yet
        {"Id": "1", "DocNumber": "1001", "TxnDate": "2026-06-25", "DueDate": "2026-07-25",
         "TotalAmt": 1000, "Balance": 1000, "CustomerRef": {"name": "Alpha"}},
        # 45 days old, past due
        {"Id": "2", "DocNumber": "1002", "TxnDate": "2026-05-21", "DueDate": "2026-06-20",
         "TotalAmt": 2000, "Balance": 2000, "CustomerRef": {"name": "Beta"}},
        # 120 days old, past due, same client as #1
        {"Id": "3", "DocNumber": "1003", "TxnDate": "2026-03-07", "DueDate": "2026-04-06",
         "TotalAmt": 500, "Balance": 500, "CustomerRef": {"name": "Alpha"}},
        # fully paid — must be ignored even if the query returns it
        {"Id": "4", "TxnDate": "2026-07-01", "Balance": 0, "TotalAmt": 900,
         "CustomerRef": {"name": "Gamma"}},
    ]
    s = main._receivables_summary(invoices, billed_365=35000, as_of=as_of)
    assert s["outstanding"] == 3500
    assert s["aging"] == {"0-30": 1000, "31-60": 2000, "61-90": 0, "90+": 500}
    assert s["pastDue"] == 2500                     # Beta + Alpha's 120-day one
    assert s["asOf"] == "2026-07-05"
    # who owes you, largest balance first; Alpha aggregates its two invoices
    assert [c["customer"] for c in s["byClient"]] == ["Beta", "Alpha"]
    alpha = next(c for c in s["byClient"] if c["customer"] == "Alpha")
    assert alpha["balance"] == 1500 and alpha["count"] == 2
    assert len(s["invoices"]) == 3                  # paid invoice dropped
    assert s["dso"] == round(3500 / 35000 * 365)    # ≈ 36


def test_receivables_dso_none_without_billing():
    from datetime import date
    s = main._receivables_summary([], billed_365=0, as_of=date(2026, 7, 5))
    assert s["dso"] is None and s["outstanding"] == 0


# ── projects/customers: parentId exposed for hierarchy roll-up ──────────────
def test_list_projects_exposes_parent_for_rollup(monkeypatch):
    customers = [
        {"Id": "1", "DisplayName": "Acme Corp", "FullyQualifiedName": "Acme Corp"},          # top-level client
        {"Id": "2", "FullyQualifiedName": "Acme Corp:East Wing", "Job": True,
         "ParentRef": {"value": "1"}},                                                        # sub-customer/job
        {"Id": "3", "FullyQualifiedName": "Acme Corp:East Wing:Roof", "IsProject": True,
         "ParentRef": {"value": "2"}},                                                        # project under the job
    ]
    monkeypatch.setattr(main, "qbo_query_all", lambda *a, **k: customers)
    out = main.list_projects()
    clients = {c["id"]: c for c in out["clients"]}
    projects = {p["id"]: p for p in out["projects"]}
    assert clients["1"]["parentId"] is None          # top-level → no parent
    assert clients["2"]["parentId"] == "1"           # job rolls up to Acme
    assert projects["3"]["parentId"] == "2"          # project rolls up to the job → Acme


# ── bills: Bill + Purchase merge, credits excluded ──────────────────────────
def test_list_bills_merges_and_drops_credits(monkeypatch):
    def q(entity, where="", key=None):
        if entity == "Bill":
            return [{"Id": "b1", "TxnDate": "2026-06-01", "TotalAmt": 3000, "VendorRef": {"name": "Sub A"}}]
        if entity == "Purchase":
            return [
                {"Id": "p1", "TxnDate": "2026-06-02", "TotalAmt": 500, "EntityRef": {"name": "Sub B"}},
                {"Id": "p2", "TxnDate": "2026-06-03", "TotalAmt": 200, "EntityRef": {"name": "Sub B"}, "Credit": True},
            ]
        return []

    monkeypatch.setattr(main, "qbo_query_all", q)
    out = main.list_bills(start="2026-01-01", end="2026-12-31")
    assert len(out) == 2                          # the credit/refund is dropped
    assert sum(x["amount"] for x in out) == 3500
    assert {x["kind"] for x in out} == {"bill", "purchase"}
    assert {x["vendor"] for x in out} == {"Sub A", "Sub B"}


# ── date-range validation ───────────────────────────────────────────────────
def test_list_time_rejects_bad_dates(monkeypatch):
    monkeypatch.setattr(main, "qbo_query_all", lambda *a, **k: [])
    with pytest.raises(HTTPException):
        main.list_time(start="2026-13-99")
    with pytest.raises(HTTPException):
        main.list_time(start="not-a-date")
    # a valid range must NOT raise
    assert main.list_time(start="2026-01-01", end="2026-12-31") == []
