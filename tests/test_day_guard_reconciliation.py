"""Daily guard versus stale QBO queries and newer external edits; no live HTTP."""
import copy
import uuid

import pytest
from fastapi import HTTPException

import main


DAY = "2026-08-30"
NEXT_DAY = "2026-08-31"


def entry(**changes):
    return main.TimeEntry(**{
        "item_id": "service", "employee_id": "employee", "hours": 1,
        "txn_date": DAY, "description": "New work", "operation_id": str(uuid.uuid4()),
        "company_key": main._company_key("test-realm"), **changes,
    })


def row(**changes):
    return {"Id": "q1", "SyncToken": "2", "NameOf": "Employee",
            "EmployeeRef": {"value": "employee"}, "ItemRef": {"value": "service"},
            "TxnDate": DAY, "Hours": 1, "Minutes": 0,
            "Description": "Original work", "BillableStatus": "NotBillable", **changes}


def committed(before, after, *, action="update", result_token="3"):
    """Persist real journal transitions, including the pre-write snapshot."""
    payload = {"Id": before["Id"], "SyncToken": before.get("SyncToken")}
    if action != "delete":
        payload.update({key: value for key, value in after.items() if key != "SyncToken"})
    result = {"Id": before["Id"]}
    if result_token is not None:
        result["SyncToken"] = result_token
    journal = main._journal()
    rec = journal.claim(str(uuid.uuid4()), action, payload, main._write_scope())
    journal.transition(rec["operationId"], rec["owner"], "in_flight", before=before)
    journal.transition(rec["operationId"], rec["owner"], "completed", result=result)


def query(monkeypatch, rows):
    monkeypatch.setattr(main, "qbo_query_all", lambda *a, **k: copy.deepcopy(rows))


def assert_blocked(new, code="DAY_TOTAL"):
    with pytest.raises(HTTPException) as caught:
        main._guard_new_time(new)
    assert caught.value.detail["code"] == code
    return caught.value


def test_completed_24h_update_overrides_visible_stale_1h_before_new_create(monkeypatch):
    before = row()
    calls = []
    monkeypatch.setattr(main, "_read_timeactivity", lambda _: before)
    monkeypatch.setattr(main, "_audit", lambda *a, **k: True)
    def post(payload, params=None):
        calls.append(copy.deepcopy(payload))
        return {**payload, "Id": "q1", "SyncToken": "3"}
    monkeypatch.setattr(main, "_post_timeactivity", post)
    main.update_time("q1", entry(hours=24, sync_token="2"), None)
    query(monkeypatch, [before])
    with pytest.raises(HTTPException) as caught:
        main.create_time(entry(hours=23, description="Additional work"), None)
    assert caught.value.detail["code"] == "DAY_TOTAL"
    assert len(calls) == 1, "the new 23h request must never reach QBO"


@pytest.mark.parametrize("visible_hours,new_hours,blocked", [(1, 23, False), (24, 1, True)])
def test_newer_external_version_wins_in_both_directions(monkeypatch, visible_hours, new_hours, blocked):
    committed(row(SyncToken="1"), row(Hours=24), result_token="2")
    # Numeric version 12 is newer than 2; lexical ordering would be wrong.
    query(monkeypatch, [row(SyncToken="12", Hours=visible_hours)])
    if blocked:
        assert_blocked(entry(hours=new_hours))
    else:
        main._guard_new_time(entry(hours=new_hours))


def test_updated_duplicate_fields_are_used_instead_of_stale_description(monkeypatch):
    committed(row(), row(Description="New work", Hours=2))
    query(monkeypatch, [row()])
    assert_blocked(entry(hours=2), "DUPLICATE_ENTRY")


def test_move_to_another_date_removes_source_and_reserves_destination(monkeypatch):
    committed(row(Hours=24), row(TxnDate=NEXT_DAY, Hours=24))
    query(monkeypatch, [row(Hours=24)])
    main._guard_new_time(entry(hours=24))
    query(monkeypatch, [])
    assert_blocked(entry(txn_date=NEXT_DAY))
    # A later external edit back to the original day is authoritative there.
    query(monkeypatch, [row(Hours=24, SyncToken="4")])
    assert_blocked(entry())


@pytest.mark.parametrize("vendor", [False, True])
def test_move_person_removes_old_reference_and_counts_destination(monkeypatch, vendor):
    after = row(Hours=24)
    if vendor:
        after.pop("EmployeeRef")
        after.update(NameOf="Vendor", VendorRef={"value": "employee"})
        destination = entry(employee_id=None, vendor_id="employee")
    else:
        after["EmployeeRef"] = {"value": "another-person"}
        destination = entry(employee_id="another-person")
    committed(row(Hours=24), after)
    query(monkeypatch, [row(Hours=24)])
    main._guard_new_time(entry(hours=24))
    assert_blocked(destination)
    query(monkeypatch, [])  # before query indexing catches up, include receipt
    main._guard_new_time(entry(hours=24))
    assert_blocked(destination)


@pytest.mark.parametrize("query_token,result_token", [("2", "3"), ("3", "3"), ("2", None), ("opaque", None)])
def test_confirmed_delete_suppresses_only_proven_stale_query_row(monkeypatch, query_token, result_token):
    before = row(Hours=24, SyncToken="opaque" if query_token == "opaque" else "2")
    committed(before, {}, action="delete", result_token=result_token)
    query(monkeypatch, [row(Hours=24, SyncToken=query_token)])
    main._guard_new_time(entry(hours=24))


def test_newer_visible_qbo_row_is_not_hidden_by_older_delete_receipt(monkeypatch):
    committed(row(Hours=24), {}, action="delete", result_token="3")
    query(monkeypatch, [row(Hours=24, SyncToken="4")])
    assert_blocked(entry())


@pytest.mark.parametrize("query_token,result_token", [("3", "3"), (None, "3"), ("2", None), ("9-1700000000", "10-1700000001")])
def test_conflicting_equal_or_unordered_versions_fail_closed_even_with_overrides(monkeypatch, query_token, result_token):
    committed(row(), row(Hours=24), result_token=result_token)
    query(monkeypatch, [row(SyncToken=query_token)])
    error = assert_blocked(entry(allow_duplicate=True, allow_day_overflow=True), "DAY_STATE_UNCERTAIN")
    assert error.status_code == 503
    assert "original save ID" in error.detail["message"]


def test_unordered_delete_conflict_is_not_assumed_to_be_stale(monkeypatch):
    committed(row(SyncToken="older-opaque"), {}, action="delete", result_token="newer-opaque")
    query(monkeypatch, [row(SyncToken="other-opaque")])
    assert_blocked(entry(), "DAY_STATE_UNCERTAIN")


def test_identical_legacy_guard_fields_count_once_without_version_tokens(monkeypatch):
    committed(row(Hours=12), row(Hours=12), result_token=None)
    query(monkeypatch, [row(Hours=12, SyncToken=None)])
    main._guard_new_time(entry())


def test_uncertain_preflight_keeps_original_uuid_and_can_retry_after_query_catches_up(monkeypatch):
    committed(row(), row(Hours=12))
    query(monkeypatch, [row(SyncToken=None)])
    calls = []
    monkeypatch.setattr(main, "_audit", lambda *a, **k: True)
    def post(payload, params=None):
        calls.append(params["requestid"])
        return {**payload, "Id": "new-entry", "SyncToken": "0"}
    monkeypatch.setattr(main, "_post_timeactivity", post)
    new = entry()
    with pytest.raises(HTTPException) as caught:
        main.create_time(new, None)
    assert caught.value.status_code == 503
    assert caught.value.detail["code"] == "DAY_STATE_UNCERTAIN"
    assert calls == []
    pending = [r for r in main._load_operations()["operations"] if r["operationId"] == new.operation_id]
    assert len(pending) == 1 and pending[0]["status"] == "reserved"
    query(monkeypatch, [row(Hours=12, SyncToken="3")])
    assert main.create_time(new, None)["operationId"] == new.operation_id
    assert calls == [new.operation_id]


def test_latest_completion_supersedes_earlier_receipt_before_version_comparison(monkeypatch):
    committed(row(SyncToken="0"), row(Hours=24), result_token="1")
    committed(row(Hours=24, SyncToken="1"), row(Hours=1), result_token="2")
    query(monkeypatch, [row(Hours=24, SyncToken="1")])
    main._guard_new_time(entry(hours=23))
