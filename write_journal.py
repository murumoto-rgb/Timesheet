"""Durable single-company write identities, reservations and fenced leases.

Every transition is a local flock/replace transaction or a PostgREST revision
compare-and-swap. No network call to QBO takes place inside a store transaction.
An expired lease can only be reclaimed with the SAME operation ID and payload;
pending reservations never expire into permission for a different create.
"""
import copy
import fcntl
import hashlib
import json
import math
import os
import tempfile
import time
import uuid
from datetime import datetime, timezone

import requests
from fastapi import HTTPException


LEASE_SECONDS = 120
STATES = {"reserved", "in_flight", "uncertain", "completed", "failed"}
PENDING = {"reserved", "in_flight", "uncertain"}


def digest(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def unavailable():
    return HTTPException(503, {"code": "JOURNAL_UNAVAILABLE", "message":
        "The write journal could not be verified. No new QuickBooks request was sent. "
        "If a save was already underway, keep its original save ID and reconcile before retrying."})


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def receipt_order(record):
    # Store revisions, unlike application-host clocks, have one global order.
    return (record.get("completedRevision", 0), record.get("updatedAt", record["createdAt"]))


def validate(data):
    """Reject corruption, duplicate identities and incomplete legacy records."""
    if not isinstance(data, dict) or not isinstance(data.get("operations"), list):
        raise unavailable()
    if "revision" in data and (type(data["revision"]) is not int or data["revision"] < 0):
        raise unavailable()
    seen = set()
    for rec in data["operations"]:
        try:
            key = rec["operationId"]
            if str(uuid.UUID(key)) != key or key in seen:
                raise ValueError()
            seen.add(key)
            if rec["action"] not in {"create", "update", "delete"} or rec["status"] not in STATES:
                raise ValueError()
            if not isinstance(rec["payload"], dict) or rec["digest"] != digest(rec["payload"]):
                raise ValueError()
            if not isinstance(rec["scope"], str) or not rec["scope"] or not isinstance(rec["createdAt"], str):
                raise ValueError()
            if rec["status"] == "completed":
                if not isinstance(rec.get("result"), dict) or not rec["result"].get("Id"):
                    raise ValueError()
                if "completedRevision" in rec and (type(rec["completedRevision"]) is not int or rec["completedRevision"] < 1):
                    raise ValueError()
            if rec["status"] in {"reserved", "in_flight"}:
                if not rec.get("owner") or not math.isfinite(float(rec["leaseUntil"])):
                    raise ValueError()
            if rec["status"] == "failed" and (type(rec.get("errorStatus")) is not int or "error" not in rec):
                raise ValueError()
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
            raise unavailable()
    return data


class Journal:
    def __init__(self, path, *, url="", headers=None, http=requests):
        self.path = path
        self.url = url.rstrip("/") + "/rest/v1/qbo_tokens" if url else ""
        self.headers = headers or {}
        self.http = http

    def _read(self):
        if self.url:
            response = self.http.get(self.url, params={"id": "eq.4", "select": "data"},
                                     headers=self.headers, timeout=15)
            response.raise_for_status()
            rows = response.json()
            if not isinstance(rows, list) or len(rows) > 1:
                raise unavailable()
            if not rows:
                return None
            return validate(rows[0]["data"])
        try:
            with open(self.path) as handle:
                return validate(json.load(handle))
        except FileNotFoundError:
            return None

    def read(self):
        try:
            return self._read() or {"revision": 0, "operations": []}
        except HTTPException:
            raise
        except Exception as exc:
            raise unavailable() from exc

    def _replace(self, data):
        directory = os.path.dirname(self.path) or "."
        fd, temporary = tempfile.mkstemp(prefix=".operations-", dir=directory)
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(data, handle, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _cas(self, old, new):
        headers = {**self.headers, "Prefer": "return=representation"}
        if old is None:
            # Plain INSERT, never upsert: a simultaneous creator must win once.
            response = self.http.post(self.url, headers=headers,
                                      json={"id": 4, "data": new}, timeout=15)
            if response.status_code == 409:
                return False
        else:
            revision = old.get("revision")
            response = self.http.patch(self.url,
                params={"id": "eq.4", "data->>revision": f"eq.{revision}" if revision is not None else "is.null"},
                headers=headers, json={"data": new}, timeout=15)
        response.raise_for_status()
        rows = response.json()
        if rows == []:
            return False
        if not isinstance(rows, list) or len(rows) != 1 or rows[0].get("data") != new:
            raise unavailable()
        return True

    def change(self, mutate):
        """Callbacks may be rerun after CAS contention; no external side effects."""
        try:
            if self.url:
                for _ in range(16):
                    old = self._read()
                    new = copy.deepcopy(old or {"revision": 0, "operations": []})
                    result = mutate(new)
                    new["revision"] = (old or {}).get("revision", 0) + 1
                    validate(new)
                    if self._cas(old, new):
                        return copy.deepcopy(result)
                raise unavailable()
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            descriptor = os.open(self.path + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
            with os.fdopen(descriptor, "a+") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                old = self._read()
                new = copy.deepcopy(old or {"revision": 0, "operations": []})
                result = mutate(new)
                new["revision"] = (old or {}).get("revision", 0) + 1
                validate(new)
                self._replace(new)
                return copy.deepcopy(result)
        except HTTPException:
            raise
        except Exception as exc:
            raise unavailable() from exc

    @staticmethod
    def match(rec, action, payload, scope):
        if rec["action"] != action or rec["digest"] != digest(payload) or rec["scope"] != scope:
            raise HTTPException(409, {"code": "OPERATION_MISMATCH", "message":
                "This save ID belongs to a different request or QuickBooks company. Retry its original request."})

    def find(self, operation_id, action, payload, scope):
        rec = next((r for r in self.read()["operations"] if r["operationId"] == operation_id), None)
        if rec:
            self.match(rec, action, payload, scope)
        return rec

    def claim(self, operation_id, action, payload, scope):
        owner = str(uuid.uuid4())
        def mutate(data):
            now = time.time()
            rec = next((r for r in data["operations"] if r["operationId"] == operation_id), None)
            if rec:
                self.match(rec, action, payload, scope)
                if rec["status"] in {"completed", "failed"}:
                    return rec
                if rec["status"] in {"reserved", "in_flight"} and rec["leaseUntil"] > now:
                    raise HTTPException(409, {"code": "OPERATION_PENDING", "operationId": operation_id,
                        "message": "This save is still being processed. Retry the unchanged request with its original save ID shortly."})
                # A crashed preflight can run again; a dispatched/uncertain write
                # skips preflight and resends only the original requestid/body.
                rec["retryDispatched"] = rec["status"] != "reserved"
            else:
                if action == "create":
                    for pending in data["operations"]:
                        if (pending["scope"] == scope and pending["action"] == "create"
                            and pending["status"] in PENDING and person_day(pending["payload"]) == person_day(payload)):
                            raise HTTPException(409, {"code": "OPERATION_PENDING", "operationId": pending["operationId"],
                                "message": "Another save for this person and date is unresolved. Review and retry that original save first."})
                rec = {"operationId": operation_id, "action": action, "payload": copy.deepcopy(payload),
                       "digest": digest(payload), "scope": scope, "createdAt": stamp(), "retryDispatched": False}
                data["operations"].append(rec)
            rec.update(status="in_flight" if rec["retryDispatched"] else "reserved", owner=owner,
                       leaseUntil=now + LEASE_SECONDS, attempts=rec.get("attempts", 0) + 1)
            return rec
        return self.change(mutate)

    def transition(self, operation_id, owner, status, **fields):
        def mutate(data):
            rec = next((r for r in data["operations"] if r["operationId"] == operation_id), None)
            if rec is None or rec.get("owner") != owner or rec["status"] not in PENDING:
                raise unavailable()
            if status == "in_flight" and rec.get("leaseUntil", 0) <= time.time():
                raise HTTPException(409, {"code": "OPERATION_PENDING", "operationId": operation_id,
                    "message": "This save's reservation expired before sending. Retry the original save."})
            rec.update(fields)
            rec["status"] = status
            rec["updatedAt"] = stamp()
            if status == "completed":
                rec["completedRevision"] = data.get("revision", 0) + 1
            if status in {"completed", "failed", "uncertain"}:
                rec["leaseUntil"] = 0
            return rec
        return self.change(mutate)


def person_day(payload):
    who_type = "VendorRef" if payload.get("VendorRef") else "EmployeeRef"
    return (payload.get("TxnDate"), who_type, str((payload.get(who_type) or {}).get("value", "")))
