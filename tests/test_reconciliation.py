"""Independent expected results for scoped, read-only reconciliation."""
import copy
import uuid

import pytest

import main
from time_reconciliation import report
from write_journal import digest


SCOPE = "test-api|realm"


def row(entry_id="q1", day="2026-08-15", **kw):
    return {"Id": entry_id, "TxnDate": day, "Hours": 1, "Minutes": 15, "Description": "Review",
            "EmployeeRef": {"value": "e"}, "ItemRef": {"value": "s"}, "CustomerRef": {"value": "p"},
            "BillableStatus": "Billable", "HourlyRate": 0, **kw}


def receipt(data=None, action="create", state="completed", at="2026-08-15T12:00:00Z", **kw):
    data = copy.deepcopy(data or row())
    payload = {k: v for k, v in data.items() if k != "Id"} if action == "create" else data.copy()
    return {"operationId": str(uuid.uuid4()), "scope": SCOPE, "payload": payload, "digest": digest(payload),
            "status": state, "action": action, "createdAt": at, "updatedAt": at, "result": data,
            "owner": "owner", "leaseUntil": 0, **kw}


def run(rows, operations=(), events=()):
    return report(rows, operations, events, "2026-08-01", "2026-08-31", SCOPE)


def test_reconciliation_uses_range_and_company_for_counts():
    inside, outside, another_company = receipt(), receipt(row(day="2026-07-01")), receipt(scope="other|realm")
    pending = receipt(row("unknown"), state="uncertain")
    events = [{"scope": SCOPE, "entryId": "q1", "action": "create", "summary": main._ta_summary(row()), "ts": "2026-08-15"},
              {"scope": SCOPE, "entryId": "old", "action": "create", "summary": main._ta_summary(row("old", day="2026-07-01")), "ts": "2026-07-01"}]
    result = run([row(), row("old", day="2026-07-01")], [inside, outside, another_company, pending], events)
    assert result["freshQbo"] == {"entries": 1, "minutes": 75}
    assert result["journal"]["operations"] == 2 and result["journal"]["completed"] == 1
    assert result["auditEvents"] == 1
    assert len(result["journal"]["unresolved"]) == 1
    assert result["journal"]["unresolved"][0]["candidateIds"] == ["q1"]
    assert [f["code"] for f in result["flags"]] == ["OUTCOME_UNCONFIRMED"]
    assert result["readOnly"] is True


def test_latest_update_and_delete_supersede_create_snapshots():
    first = receipt()
    update = receipt(row(Hours=2), action="update", at="2026-08-15T13:00:00Z")
    deletion = receipt(row("q2"), action="delete", at="2026-08-15T14:00:00Z")
    # Old create/update receipts must not cause false missing/changed flags.
    assert run([row(Hours=2)], [first, update, receipt(row("q2")), deletion])["flags"] == []
    changed = run([row(Hours=3), row("q2")], [first, update, deletion])
    assert {f["code"] for f in changed["flags"]} == {"ENTRY_CHANGED", "DELETED_ENTRY_PRESENT"}
    assert next(f for f in changed["flags"] if f["code"] == "ENTRY_CHANGED")["fields"] == ["Hours"]


def test_receipt_order_uses_store_revision_despite_host_clock_skew():
    later_clock_but_earlier_write = receipt(at="2026-08-15T15:00:00Z", completedRevision=3)
    earlier_clock_but_later_write = receipt(row(Hours=2), action="update", at="2026-08-15T12:00:00Z", completedRevision=6)
    assert run([row(Hours=2)], [later_clock_but_earlier_write, earlier_clock_but_later_write])["flags"] == []


def test_missing_confirmed_and_possible_duplicates_are_review_flags():
    result = run([row("a"), row("b")], [receipt(row("missing"))])
    assert {f["code"] for f in result["flags"]} == {"CONFIRMED_ENTRY_MISSING", "POSSIBLE_DUPLICATE"}
    assert next(f for f in result["flags"] if f["code"] == "POSSIBLE_DUPLICATE")["entryIds"] == ["a", "b"]


def test_latest_audit_event_and_moved_date_do_not_report_obsolete_state():
    create = {"scope": SCOPE, "entryId": "q1", "action": "create", "summary": main._ta_summary(row()), "ts": "2026-08-15T12:00:00Z"}
    update = {"scope": SCOPE, "entryId": "q1", "action": "update", "summary": main._ta_summary(row(Hours=2)), "ts": "2026-08-15T13:00:00Z"}
    assert run([row(Hours=2)], [], [create, update])["flags"] == []
    assert run([row(Hours=3)], [], [create, update])["flags"][0]["code"] == "AUDIT_DIFFERENCE"
    moved = receipt(row(day="2026-09-01"), action="update", at="2026-08-16T12:00:00Z", before=row())
    result = run([], [receipt(), moved], [create])
    assert result["flags"] == [] and result["journal"]["operations"] == 2


def test_audit_company_mismatch_is_excluded_and_legacy_scope_is_disclosed():
    legacy = {"entryId": "q1", "action": "delete", "summary": main._ta_summary(row()), "ts": "2026-08-15"}
    other_company = {**legacy, "scope": "another-company"}
    result = run([row()], [], [legacy, other_company])
    assert result["flags"] == [] and result["auditEvents"] == 0 and result["auditLegacyEvents"] == 1


def test_endpoint_reconciliation_does_not_mutate_journal_or_qbo(tmp_path, monkeypatch):
    scope = main._write_scope()
    rec = receipt(scope=scope)
    main._journal().change(lambda data: data["operations"].append(rec))
    original = tmp_path.joinpath("operations.json").read_bytes()
    calls = []
    def query(entity, where):
        calls.append((entity, where))
        return [row()]
    monkeypatch.setattr(main, "qbo_query_all", query)
    result = main.reconciliation("2026-08-01", "2026-08-31")
    assert result["flags"] == []
    assert tmp_path.joinpath("operations.json").read_bytes() == original
    assert calls == [("TimeActivity", "WHERE TxnDate >= '2026-08-01' AND TxnDate <= '2026-08-31'")]


def test_project_payment_attribution_excludes_ambiguous_invalid_and_wrong_scope(monkeypatch):
    invoices = [
        {"Id": "i1", "TxnDate": "2026-08-01", "CustomerRef": {"value": "p"}, "TotalAmt": 1000, "Balance": 200},
        {"Id": "i2", "TxnDate": "2026-08-01", "CustomerRef": {"value": "parent"}, "ProjectRef": {"value": "p"}, "TotalAmt": 9000},
        {"Id": "old", "TxnDate": "2026-07-01", "CustomerRef": {"value": "p"}, "TotalAmt": 500},
    ]
    def link(invoice): return {"TxnId": invoice, "TxnType": "Invoice"}
    lines = [
        {"Amount": 100, "LinkedTxn": [link("i1")]},
        {"Amount": 800, "LinkedTxn": [link("i1"), link("i2")]},
        {"Amount": 400, "LinkedTxn": [link("i1"), {"TxnId": "credit", "TxnType": "CreditMemo"}]},
        {"Amount": 500, "LinkedTxn": [link("i2")]},
        {"Amount": 500, "LinkedTxn": [link("old")]},
        *[{"Amount": amount, "LinkedTxn": [link("i1")]} for amount in [None, "", float("nan"), float("inf"), -1, True]],
        {"Amount": 0, "LinkedTxn": [link("i1")]},
    ]
    payment = {"Id": "pay", "TxnDate": "2026-08-10", "Line": lines, "CurrencyRef": {"value": "USD"}}
    monkeypatch.setattr(main, "qbo_query_all", lambda entity, *a, **k: invoices if entity == "Invoice" else [payment])
    result = main.project_financials("p", "2026-08-01", "2026-08-31")
    assert [r["id"] for r in result["invoices"]] == ["i1"]
    assert [r["amount"] for r in result["payments"]] == [100, 0]
    assert all(r["invoiceIds"] == ["i1"] for r in result["payments"])
    assert result["skippedAmbiguousPaymentLines"] == 2
    assert result["skippedInvalidPaymentLines"] == 6
    assert "dated in this range" in result["scope"]
    assert any("not complete" in caveat for caveat in result["caveats"])


def test_project_invoice_amounts_preserve_unknowns_and_explicit_zero(monkeypatch):
    amounts = [None, "broken", False, float("nan"), float("inf"), 0, "12.50"]
    balances = [None, "", True, float("-inf"), {"bad": 1}, 0, "3.25"]
    invoices = [{"Id": str(i), "TxnDate": "2026-08-01", "CustomerRef": {"value": "p"},
                 "TotalAmt": amount, "Balance": balance}
                for i, (amount, balance) in enumerate(zip(amounts, balances))]
    monkeypatch.setattr(main, "qbo_query_all", lambda entity, *a, **k: invoices if entity == "Invoice" else [])
    result = main.project_financials("p", "2026-08-01", "2026-08-31")
    assert [r["amount"] for r in result["invoices"]] == [None, None, None, None, None, 0, 12.5]
    assert [r["balance"] for r in result["invoices"]] == [None, None, None, None, None, 0, 3.25]
    assert result["payments"] == []
