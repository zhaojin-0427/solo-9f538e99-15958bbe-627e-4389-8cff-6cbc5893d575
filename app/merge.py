"""Patient merge: fold two Patients confirmed to be the same person into one.

``POST /merge`` takes a merge number (``mergeId``), a ``source`` patient and a
``target`` (retained) patient. Both must exist and must not already be merged
away. The merge is applied in a single SQLite transaction together with its
idempotency record, so a crash or restart never leaves half a merge behind:

* the retained patient's resource is never rewritten (demographics and its own
  identifier list stay exactly as stored);
* every complete ``identifier(system, value)`` of either party now matches the
  retained patient during ingestion;
* stored references to the source patient's permanent address (Specimen and
  Observation subjects, and any other reference) are rewritten to the retained
  patient's address, so existing lab data reads with the current owner;
* the source patient's old address keeps resolving: reads follow the merge
  chain and return the retained patient, and new bundles may still reference
  the old address — it resolves to the retained patient at ingest time.

The retained patient may itself be merged later; old addresses then trace the
chain to the final patient. Requiring the target to be unmerged makes cycles
impossible by construction.

Idempotency: the same ``mergeId`` with the same body replays the original
response; the same ``mergeId`` with a different body is a ``409`` conflict.
The record lives in SQLite and survives restarts.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from typing import Any

from . import settings
from .store import Store
from .transaction import BundleError, fingerprint

# Status codes used by this module:
#   400 - the HTTP body is not a JSON object
#   409 - mergeId replayed with different parameters
#   422 - merge rejected (unknown patient, already merged, self-merge, ...)
MALFORMED = 400
CONFLICT = 409
REJECTED = 422

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _issue(severity: str, code: str, diagnostics: str) -> dict[str, Any]:
    return {"severity": severity, "code": code, "diagnostics": diagnostics}


def _fail(status: int, message: str, code: str = "invalid") -> BundleError:
    return BundleError(status, message, [_issue("error", code, message)])


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _permanent(resource_id: str) -> str:
    return f"{settings.BASE_URL}/Patient/{resource_id}"


def _patient_ref(value: Any, field: str) -> str:
    """Normalise a patient reference to a bare resource id.

    Accepts a bare id (``"pat-1"``), a relative reference (``"Patient/pat-1"``)
    or the permanent address (``"<FHIR_BASE_URL>/Patient/pat-1"``).
    """
    if not isinstance(value, str) or not value.strip():
        raise _fail(
            REJECTED,
            f"'{field}' is required: a Patient id, 'Patient/<id>' or the permanent "
            f"address '{settings.BASE_URL}/Patient/<id>'",
            code="required",
        )
    ref = value.strip()
    prefix = settings.BASE_URL + "/"
    if ref.startswith(prefix):
        ref = ref[len(prefix):]
    if ref.startswith("Patient/"):
        candidate = ref[len("Patient/"):]
    elif "/" not in ref:
        candidate = ref
    else:
        raise _fail(
            REJECTED,
            f"'{field}' must reference a Patient, got {value!r}",
        )
    if not _ID_RE.match(candidate):
        raise _fail(
            REJECTED,
            f"'{field}' is not a valid Patient id (letters, digits, '.', '_', '-'; "
            f"at most 64 characters): {value!r}",
        )
    return candidate


def merge_patients(store: Store, payload: Any) -> dict[str, Any]:
    """Validate and atomically apply a patient merge. Raises BundleError."""
    if not isinstance(payload, dict):
        raise BundleError(
            MALFORMED,
            "Merge request body must be a JSON object",
            [_issue("error", "invalid", "Body is not a JSON object")],
        )
    merge_id = payload.get("mergeId")
    if not isinstance(merge_id, str) or not _ID_RE.match(merge_id):
        raise _fail(
            REJECTED,
            "'mergeId' is required and must be a valid id (letters, digits, '.', "
            "'_', '-'; at most 64 characters)",
            code="required",
        )
    source_id = _patient_ref(payload.get("source"), "source")
    target_id = _patient_ref(payload.get("target"), "target")
    if source_id == target_id:
        raise _fail(REJECTED, "'source' and 'target' must be two different patients")
    body_fingerprint = fingerprint(payload)

    # Cheap, lock-free path for an identical retry.
    replay = _replay(store, merge_id, body_fingerprint)
    if replay is not None:
        return replay

    with store.transaction() as conn:
        # Re-check under the write lock: a concurrent duplicate may have won.
        row = conn.execute(
            "SELECT fingerprint, response FROM merge_records WHERE merge_id = ?",
            (merge_id,),
        ).fetchone()
        if row is not None:
            if row["fingerprint"] != body_fingerprint:
                raise _id_conflict(merge_id)
            return json.loads(row["response"])

        _require_live_patient(conn, source_id, "source")
        _require_live_patient(conn, target_id, "target")

        result = _apply(conn, merge_id, source_id, target_id)
        conn.execute(
            "INSERT INTO merge_records(merge_id, fingerprint, response, created_at) "
            "VALUES (?,?,?,?)",
            (
                merge_id,
                body_fingerprint,
                # Preserve the response exactly as first returned so an
                # idempotent replay is indistinguishable to the client.
                json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                time.time(),
            ),
        )
        return result


def _replay(store: Store, merge_id: str, body_fingerprint: str) -> dict[str, Any] | None:
    conn = store.raw_connection()
    try:
        row = conn.execute(
            "SELECT fingerprint, response FROM merge_records WHERE merge_id = ?",
            (merge_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    if row["fingerprint"] != body_fingerprint:
        raise _id_conflict(merge_id)
    return json.loads(row["response"])


def _id_conflict(merge_id: str) -> BundleError:
    return BundleError(
        CONFLICT,
        f"mergeId {merge_id!r} was already used with different parameters",
        [_issue("error", "conflict",
                "A merge number is idempotent: retry with the exact original body")],
    )


def _require_live_patient(conn: sqlite3.Connection, patient_id: str, field: str) -> None:
    row = conn.execute(
        "SELECT 1 FROM resources WHERE resource_type = 'Patient' AND id = ?",
        (patient_id,),
    ).fetchone()
    if row is None:
        raise _fail(
            REJECTED,
            f"{field} patient '{_permanent(patient_id)}' does not exist",
            code="not-found",
        )
    merged = conn.execute(
        "SELECT target_id FROM patient_merges WHERE source_id = ?", (patient_id,)
    ).fetchone()
    if merged is not None:
        raise _fail(
            REJECTED,
            f"{field} patient '{_permanent(patient_id)}' was already merged into "
            f"'{_permanent(merged['target_id'])}'; merge into the retained patient "
            "instead",
            code="conflict",
        )


def _apply(conn: sqlite3.Connection, merge_id: str, source_id: str,
           target_id: str) -> dict[str, Any]:
    source_perm = _permanent(source_id)
    target_perm = _permanent(target_id)

    # 1. Both parties' identifiers now match the retained patient. The retained
    #    patient's stored resource (demographics, its own identifier list) is
    #    never touched.
    remapped = conn.execute(
        "UPDATE patient_identities SET patient_id = ? WHERE patient_id = ?",
        (target_id, source_id),
    ).rowcount

    # 2. Stored references to the source patient's address now name the
    #    retained patient, so existing Specimen/Observation subjects read with
    #    the current permanent address of their owner.
    updated = _rewrite_patient_references(conn, source_perm, target_perm)

    # 3. The merge itself: the old address follows the chain to the survivor.
    conn.execute(
        "INSERT INTO patient_merges(source_id, target_id, merge_id, created_at) "
        "VALUES (?,?,?,?)",
        (source_id, target_id, merge_id, time.time()),
    )

    return {
        "mergeId": merge_id,
        "source": source_perm,
        "target": target_perm,
        "final": target_perm,
        "remappedIdentifiers": remapped,
        "updatedResources": updated,
    }


def _rewrite_patient_references(conn: sqlite3.Connection, old_ref: str,
                                new_ref: str) -> int:
    """Point every stored ``reference`` equal to ``old_ref`` at ``new_ref``."""
    rows = conn.execute("SELECT resource_type, id, content FROM resources").fetchall()
    updated = 0
    for row in rows:
        if old_ref not in row["content"]:
            continue
        resource = json.loads(row["content"])
        if _swap_refs(resource, old_ref, new_ref):
            conn.execute(
                "UPDATE resources SET content = ? WHERE resource_type = ? AND id = ?",
                (_canonical(resource), row["resource_type"], row["id"]),
            )
            updated += 1
    return updated


def _swap_refs(node: Any, old_ref: str, new_ref: str) -> bool:
    """Rewrite ``reference`` strings in place; return True if anything changed."""
    changed = False
    if isinstance(node, list):
        for item in node:
            changed |= _swap_refs(item, old_ref, new_ref)
        return changed
    if not isinstance(node, dict):
        return False
    if node.get("reference") == old_ref:
        node["reference"] = new_ref
        changed = True
    for name, item in node.items():
        if name != "reference":
            changed |= _swap_refs(item, old_ref, new_ref)
    return changed
