"""Read-only comparison of fresh scoped time rows and durable local receipts."""
from collections import defaultdict

from write_journal import PENDING, digest, receipt_order


FIELDS = ("TxnDate", "Hours", "Minutes", "Description", "BillableStatus", "HourlyRate",
          "EmployeeRef", "VendorRef", "CustomerRef", "ItemRef")


def value(row, field):
    if field.endswith("Ref"):
        return str((row.get(field) or {}).get("value", ""))
    if field in {"Hours", "Minutes"}:
        return int(row.get(field) or 0)
    if field == "HourlyRate":
        rate = row.get(field)
        return float(rate) if rate not in (None, "") else None
    return row.get(field, "")


def differences(expected, actual):
    return [field for field in FIELDS if field in expected and value(expected, field) != value(actual, field)]


def snapshot(rec):
    return {**(rec.get("before") or {}), **rec["payload"], **(rec.get("result") or {})}


def report(rows, operations, events, start, end, scope):
    def in_range(day):
        return isinstance(day, str) and start <= day <= end

    rows = [r for r in rows if in_range(r.get("TxnDate"))]
    legacy_events = [event for event in events if not event.get("scope")
                     and in_range((event.get("summary") or {}).get("date"))]
    # Pre-journal activity logs did not identify their company. Do not infer
    # that an old ID refers to the newly connected company's same numeric ID.
    events = [event for event in events if event.get("scope") == scope]
    all_ops = [r for r in operations if r["scope"] == scope]
    # Moving an entry affects both its old and new dates. Deletes inherit the
    # date from the pre-write snapshot. Undated claims are separately disclosed.
    def dates(rec):
        return [part.get("TxnDate") for part in (rec["payload"], rec.get("before") or {}, rec.get("result") or {})]
    ops = [r for r in all_ops if any(in_range(day) for day in dates(r))]
    unscoped = [r for r in all_ops if r["status"] in PENDING and not any(dates(r))]
    scoped_events = [e for e in events if in_range((e.get("summary") or {}).get("date"))
                     or in_range((e.get("before") or {}).get("date"))]
    actual = {str(r["Id"]): r for r in rows if r.get("Id") is not None}
    flags = []
    latest = {}
    for rec in sorted(all_ops, key=receipt_order):
        if rec["status"] == "completed":
            entry_id = str(rec["result"].get("Id") or rec["payload"].get("Id") or "")
            if entry_id:
                latest[entry_id] = rec
    for entry_id, rec in latest.items():
        expected = snapshot(rec)
        current = actual.get(entry_id)
        if rec["action"] == "delete":
            if current:
                flags.append({"code": "DELETED_ENTRY_PRESENT", "entryId": entry_id,
                              "message": f"Entry {entry_id} is present in QuickBooks despite a confirmed local delete."})
            continue
        if not in_range(expected.get("TxnDate")):
            continue
        if current is None:
            flags.append({"code": "CONFIRMED_ENTRY_MISSING", "entryId": entry_id,
                          "message": f"Confirmed entry {entry_id} is absent from this QuickBooks date range. It may have been moved or deleted; review it."})
        else:
            changed = differences(expected, current)
            if changed:
                flags.append({"code": "ENTRY_CHANGED", "entryId": entry_id, "fields": changed,
                              "message": f"Entry {entry_id} differs from its latest confirmed app write ({', '.join(changed)}). QuickBooks-side changes may be legitimate."})
    # Older writes can predate the durable journal. Compare the latest audit
    # event per entity only when a newer journal receipt does not supersede it.
    latest_audit = {}
    for event in sorted(events, key=lambda e: e.get("ts", "")):
        if event.get("entryId"):
            latest_audit[str(event["entryId"])] = event
    for entry_id, event in latest_audit.items():
        if entry_id in latest:
            continue
        summary = event.get("summary") or {}
        if not in_range(summary.get("date")):
            continue
        current = actual.get(entry_id)
        expected = {dest: summary[src] for src, dest in {"date": "TxnDate", "hours": "Hours", "minutes": "Minutes",
                    "description": "Description", "billableStatus": "BillableStatus"}.items() if src in summary}
        if event.get("action") == "delete":
            changed = bool(current)
        else:
            changed = current is None or bool(differences(expected, current))
        if changed:
            flags.append({"code": "AUDIT_DIFFERENCE", "entryId": entry_id,
                          "message": f"Entry {entry_id} differs from its latest local activity-log event. Review QuickBooks before taking action."})
    unresolved = []
    for rec in ops:
        if rec["status"] not in PENDING:
            continue
        item = {"operationId": rec["operationId"], "state": rec["status"], "action": rec["action"],
                "date": snapshot(rec).get("TxnDate"), "createdAt": rec["createdAt"]}
        if rec["action"] == "create":
            item["candidateIds"] = [str(row.get("Id")) for row in rows if not differences(rec["payload"], row)]
        else:
            item["entryId"] = rec["payload"].get("Id")
            item["entryPresent"] = str(item["entryId"]) in actual
        unresolved.append(item)
        flags.append({"code": "OUTCOME_UNCONFIRMED", **item,
                      "message": f"Save {rec['operationId']} is {rec['status']}. Matching rows alone do not prove its outcome. Retry only the original request and save ID."})
    groups = defaultdict(list)
    for row in rows:
        key = {field: value(row, field) for field in FIELDS}
        key["Description"] = key["Description"].strip()
        groups[digest(key)].append(str(row.get("Id")))
    for ids in groups.values():
        if len(ids) > 1:
            flags.append({"code": "POSSIBLE_DUPLICATE", "entryIds": ids,
                          "message": f"Entries {', '.join(ids)} have matching time fields. They may be intentional; review before deleting anything."})
    return {"start": start, "end": end,
            "freshQbo": {"entries": len(rows), "minutes": sum(value(r, "Hours") * 60 + value(r, "Minutes") for r in rows)},
            "journal": {"operations": len(ops), "completed": sum(r["status"] == "completed" for r in ops),
                        "failed": sum(r["status"] == "failed" for r in ops), "unresolved": unresolved,
                        "unresolvedIds": [r["operationId"] for r in unresolved],
                        "unscopedUnresolved": [{"operationId": r["operationId"], "state": r["status"]} for r in unscoped]},
            "auditEvents": len(scoped_events), "auditLegacyEvents": len(legacy_events), "flags": flags, "readOnly": True,
            "caveats": ["This compares a fresh QBO TimeActivity query with local receipts, not an independent accounting report.",
                        "External changes and deliberate duplicate entries can produce review flags; flags do not prove corruption.",
                        "No repair, deletion, or retry occurs during reconciliation.",
                        "Operation identities and pending outcomes are retained without automatic eviction; the separate activity log retains up to 2000 events.",
                        "Older activity-log events without a company identity are counted separately and not used to infer discrepancies.",
                        "Undated pending operations are shown separately because they cannot be assigned to this date range."]}
