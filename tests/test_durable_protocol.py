"""Adversarial protocol tests: processes, CAS contention and lost acknowledgments.

No external HTTP or production storage is used. Fake QBO implements requestid
replay independently of the application's journal.
"""
import asyncio
import copy
import json
import multiprocessing
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
import requests
from fastapi import HTTPException

import main
from write_journal import Journal


def entry(**values):
    return main.TimeEntry(**{ "item_id": "s", "employee_id": "e", "hours": 1,
        "txn_date": "2026-08-30", "company_key": main._company_key("test-realm"), "operation_id": str(uuid.uuid4()), **values})


def wire(monkeypatch):
    monkeypatch.setattr(main, "_audit", lambda *a, **k: True)
    monkeypatch.setattr(main, "qbo_query_all", lambda *a, **k: [])


class Response:
    def __init__(self, data, status=200):
        self.data, self.status_code = copy.deepcopy(data), status
    def json(self): return copy.deepcopy(self.data)
    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


class CASStore:
    """Models one Postgres row: predicate and update execute atomically."""
    def __init__(self):
        self.data, self.lock, self.conflicts = None, threading.Lock(), 0
        self.fail_claim = False
        self.timeout_after_commit = False
        self.force_conflict = False
    def get(self, url, **kw):
        assert kw["params"] == {"id": "eq.4", "select": "data"}
        with self.lock:
            result = Response([] if self.data is None else [{"data": self.data}])
        time.sleep(.001)  # encourage independent clients to read the same version
        return result
    def post(self, url, **kw):
        assert kw["json"]["id"] == 4
        assert "merge-duplicates" not in kw["headers"]["Prefer"]
        with self.lock:
            if self.fail_claim: raise requests.Timeout("store unavailable")
            if self.data is not None:
                self.conflicts += 1
                return Response({}, 409)
            self.data = copy.deepcopy(kw["json"]["data"])
            return Response([{"id": 4, "data": self.data}], 201)
    def patch(self, url, **kw):
        assert kw["params"]["id"] == "eq.4"
        assert kw["headers"]["Prefer"] == "return=representation"
        with self.lock:
            if self.fail_claim: raise requests.Timeout("store unavailable")
            revision = self.data.get("revision")
            expected = f"eq.{revision}" if revision is not None else "is.null"
            if self.force_conflict or kw["params"]["data->>revision"] != expected:
                self.conflicts += 1
                return Response([])
            self.data = copy.deepcopy(kw["json"]["data"])
            if self.timeout_after_commit:
                self.timeout_after_commit = False
                raise requests.Timeout("commit response lost")
            return Response([{"id": 4, "data": self.data}])


def fake_remote(monkeypatch, tmp_path):
    store = CASStore()
    # Each call returns an independent client; no shared in-process journal lock.
    monkeypatch.setattr(main, "_journal", lambda: Journal(str(tmp_path / "unused"), url="https://test.invalid", http=store))
    return store


@pytest.mark.parametrize("remote", [False, True])
def test_timeout_after_qbo_commit_replays_exact_payload_once(monkeypatch, tmp_path, remote):
    wire(monkeypatch)
    if remote: fake_remote(monkeypatch, tmp_path)
    qbo, requests_seen = {}, []
    def post(payload, params=None):
        key = params["requestid"]
        requests_seen.append((key, copy.deepcopy(payload)))
        if key not in qbo:
            qbo[key] = {"Id": "q1", "SyncToken": "0", **payload}
            raise requests.Timeout("QBO saved, response lost")
        return qbo[key]
    monkeypatch.setattr(main, "_post_timeactivity", post)
    original = entry()
    with pytest.raises(HTTPException) as first:
        main.create_time(original, None)
    assert first.value.detail["code"] == "OPERATION_UNCERTAIN"
    # A new ID, even with explicit duplicate override, cannot bypass the pending reservation.
    with pytest.raises(HTTPException) as competing:
        main.create_time(entry(allow_duplicate=True), None)
    assert competing.value.detail["code"] == "OPERATION_PENDING"
    assert len(requests_seen) == 1
    result = main.create_time(original, None)
    assert result["Id"] == "q1"
    assert len(qbo) == 1 and len(requests_seen) == 2
    assert requests_seen[0] == requests_seen[1]
    assert main.create_time(original, None) == result
    assert len(requests_seen) == 2


def test_update_and_delete_retries_skip_changed_or_missing_current_entity(monkeypatch):
    wire(monkeypatch)
    current = {"Id": "q1", "SyncToken": "3", "TxnDate": "2026-08-30", "Hours": 1}
    reads, saved = [], {}
    def read(_id):
        reads.append(_id)
        return current.copy()
    def post(payload, params=None):
        key = params["requestid"]
        if key not in saved:
            saved[key] = {**payload, "Id": "q1", "SyncToken": "4"}
            current.update(SyncToken="4", BillableStatus="HasBeenBilled")
            raise requests.Timeout("acknowledgment lost")
        return saved[key]
    monkeypatch.setattr(main, "_read_timeactivity", read)
    monkeypatch.setattr(main, "_post_timeactivity", post)
    original = entry(sync_token="3", description="changed")
    with pytest.raises(HTTPException): main.update_time("q1", original, None)
    assert main.update_time("q1", original, None)["SyncToken"] == "4"
    assert len(reads) == 1
    current.update(SyncToken="4", BillableStatus="Billable")
    class Request:
        client = None
        async def json(self): return {"sync_token": "4", "operation_id": delete_id, "company_key": main._company_key("test-realm")}
    delete_id = str(uuid.uuid4())
    with pytest.raises(HTTPException): asyncio.run(main.delete_time("q1", Request()))
    monkeypatch.setattr(main, "_read_timeactivity", lambda _: pytest.fail("retry must not reread a deleted row"))
    assert asyncio.run(main.delete_time("q1", Request()))["deleted"] == "q1"


def test_cas_concurrent_claims_and_completions_do_not_lose_other_operations(monkeypatch, tmp_path):
    wire(monkeypatch)
    store = fake_remote(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "_post_timeactivity", lambda p, params=None: {"Id": params["requestid"], **p})
    entries = [entry(employee_id=f"person-{i}") for i in range(8)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        # Independent hosts do not share the single-process token lock. Test
        # their journal clients directly to force real CAS contention.
        results = list(pool.map(lambda e: main._journaled_qbo_write(e.operation_id, "create",
                            main._timeactivity_payload(e), prepare=lambda: main._guard_new_time(e)), entries))
    records = main._load_operations()["operations"]
    assert len(records) == 8 and {r["status"] for r in records} == {"completed"}
    assert {r["operationId"] for r in records} == {r["operationId"] for r in results}
    assert store.conflicts > 0


def test_two_hosts_different_ids_same_person_day_cannot_create_duplicates(monkeypatch, tmp_path):
    wire(monkeypatch)
    fake_remote(monkeypatch, tmp_path)
    calls, lock = [], threading.Lock()
    def post(payload, params=None):
        with lock: calls.append(params["requestid"])
        time.sleep(.03)
        return {"Id": "q1", **payload}
    monkeypatch.setattr(main, "_post_timeactivity", post)
    def save(_):
        original = entry()
        try: return main._journaled_qbo_write(original.operation_id, "create", main._timeactivity_payload(original),
                                             prepare=lambda: main._guard_new_time(original))
        except HTTPException as exc: return exc
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(save, range(2)))
    assert len(calls) == 1
    assert sum(isinstance(r, HTTPException) for r in outcomes) == 1


def _child_write(path, start, output, number, same_id):
    """forked process gets its own interpreter locks, shares only the journal."""
    main.OP_FILE = path
    main._audit = lambda *a, **k: True
    main.qbo_query_all = lambda *a, **k: []
    def post(payload, params=None):
        output.put(("post", params["requestid"]))
        time.sleep(.03)
        return {"Id": params["requestid"], **payload}
    main._post_timeactivity = post
    original = entry(operation_id=same_id or str(uuid.uuid4()), employee_id="same" if same_id else str(number))
    start.wait(10)
    try: output.put(("result", main.create_time(original, None)["operationId"]))
    except HTTPException as exc: output.put(("error", exc.status_code))


@pytest.mark.parametrize("same_id", [False, True])
def test_local_process_claims_and_completions_are_serialized(tmp_path, same_id):
    context = multiprocessing.get_context("fork")
    start, output = context.Event(), context.Queue()
    operation_id = str(uuid.uuid4()) if same_id else None
    path = str(tmp_path / "process-operations.json")
    processes = [context.Process(target=_child_write, args=(path, start, output, i, operation_id)) for i in range(6)]
    for process in processes: process.start()
    start.set()
    for process in processes:
        process.join(15)
        assert process.exitcode == 0
    records = Journal(path).read()["operations"]
    assert len(records) == (1 if same_id else 6)
    assert {r["status"] for r in records} == {"completed"}
    messages = []
    while not output.empty(): messages.append(output.get(timeout=2))
    assert sum(kind == "post" for kind, _ in messages) == (1 if same_id else 6)


def test_expired_lease_only_same_id_can_reclaim_and_old_owner_cannot_finish(monkeypatch):
    wire(monkeypatch)
    original = entry()
    journal = main._journal()
    payload, scope = main._timeactivity_payload(original), main._write_scope()
    first = journal.claim(original.operation_id, "create", payload, scope)
    journal.transition(original.operation_id, first["owner"], "in_flight")
    journal.change(lambda data: data["operations"][0].update(leaseUntil=0))
    with pytest.raises(HTTPException) as other:
        journal.claim(str(uuid.uuid4()), "create", payload, scope)
    assert other.value.detail["code"] == "OPERATION_PENDING"
    second = journal.claim(original.operation_id, "create", payload, scope)
    assert second["owner"] != first["owner"] and second["retryDispatched"]
    with pytest.raises(HTTPException):
        journal.transition(original.operation_id, first["owner"], "completed", result={"Id": "wrong"})
    journal.transition(original.operation_id, second["owner"], "completed", result={"Id": "right"})
    assert journal.read()["operations"][0]["result"]["Id"] == "right"


def test_no_eviction_and_corrupt_existing_identity_fails_closed(monkeypatch):
    wire(monkeypatch)
    journal = main._journal()
    original = entry()
    first = journal.claim(original.operation_id, "create", main._timeactivity_payload(original), main._write_scope())
    def fill(data):
        for i in range(2002):
            record = copy.deepcopy(first)
            record.update(operationId=str(uuid.uuid4()), status="completed", result={"Id": str(i)})
            data["operations"].append(record)
    journal.change(fill)
    assert len(journal.read()["operations"]) == 2003
    assert journal.read()["operations"][0]["operationId"] == original.operation_id
    with open(main.OP_FILE) as handle: damaged = json.load(handle)
    damaged["operations"][0]["payload"]["Hours"] = 20
    with open(main.OP_FILE, "w") as handle: json.dump(damaged, handle)
    monkeypatch.setattr(main, "_post_timeactivity", lambda *a, **k: pytest.fail("corrupt store must not write"))
    with pytest.raises(HTTPException) as exc: main.create_time(original, None)
    assert exc.value.status_code == 503


def test_failed_claim_or_cas_never_posts_and_lost_completion_is_recoverable(monkeypatch, tmp_path):
    wire(monkeypatch)
    store = fake_remote(monkeypatch, tmp_path)
    calls = []
    def post(p, params=None):
        calls.append(params["requestid"])
        store.timeout_after_commit = True
        return {"Id": "q1", **p}
    monkeypatch.setattr(main, "_post_timeactivity", post)
    original = entry()
    store.fail_claim = True
    with pytest.raises(HTTPException): main.create_time(original, None)
    assert calls == []
    store.fail_claim = False
    with pytest.raises(HTTPException): main.create_time(original, None)
    assert calls == [original.operation_id]
    # The completion committed but its reply was lost. Replay reads the result.
    assert main.create_time(original, None)["Id"] == "q1"
    assert len(calls) == 1
    store.force_conflict = True
    with pytest.raises(HTTPException): main.create_time(entry(employee_id="different"), None)
    assert len(calls) == 1


def test_journal_deduplicates_visible_qbo_ids_in_day_total(monkeypatch):
    wire(monkeypatch)
    original = entry(hours=12)
    row = {"Id": "q1", **main._timeactivity_payload(original)}
    monkeypatch.setattr(main, "_post_timeactivity", lambda *a, **k: row)
    main.create_time(original, None)
    monkeypatch.setattr(main, "qbo_query_all", lambda *a, **k: [row])
    # 12 already logged + 1 new = 13, not 25 from double counting the receipt.
    main._guard_new_time(entry(hours=1, description="different"))


def test_realm_mismatch_and_failed_preflight_are_not_new_writes(monkeypatch):
    wire(monkeypatch)
    monkeypatch.setattr(main, "_post_timeactivity", lambda p, params=None: {"Id": "q1", **p})
    original = entry()
    main.create_time(original, None)
    monkeypatch.setattr(main, "_load_tokens", lambda: {"realm_id": "other-company"})
    with pytest.raises(HTTPException) as mismatch: main.create_time(original, None)
    assert mismatch.value.detail["code"] == "COMPANY_CHANGED"


def test_preflight_network_failure_can_retry_original_without_dead_end(monkeypatch):
    wire(monkeypatch)
    original, calls = entry(), []
    def query(*a, **k):
        calls.append(1)
        if len(calls) == 1: raise requests.Timeout("read failed")
        return []
    monkeypatch.setattr(main, "qbo_query_all", query)
    monkeypatch.setattr(main, "_post_timeactivity", lambda p, params=None: {"Id": "q1", **p})
    with pytest.raises(HTTPException): main.create_time(original, None)
    assert main._load_operations()["operations"][0]["status"] == "reserved"
    assert main.create_time(original, None)["Id"] == "q1"
