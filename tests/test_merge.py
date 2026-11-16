"""End-to-end tests for the patient merge API (POST /merge)."""
from __future__ import annotations

import json
import threading
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.store import Store

FHIR_HEADERS = {"content-type": "application/fhir+json"}
JSON_HEADERS = {"content-type": "application/json"}
BASE = "http://localhost:8000"


# --------------------------------------------------------------------------- helpers


def patient(full_url: str, mrn: str, **extra: Any) -> dict[str, Any]:
    return {
        "fullUrl": full_url,
        "resource": {
            "resourceType": "Patient",
            "identifier": [{"system": "urn:lis:mrn", "value": mrn}],
            **extra,
        },
        "request": {"method": "POST", "url": "Patient"},
    }


def specimen(full_url: str, subject: str) -> dict[str, Any]:
    return {
        "fullUrl": full_url,
        "resource": {
            "resourceType": "Specimen",
            "status": "available",
            "subject": {"reference": subject},
        },
        "request": {"method": "POST", "url": "Specimen"},
    }


def observation(full_url: str, subject: str, spec: str) -> dict[str, Any]:
    return {
        "fullUrl": full_url,
        "resource": {
            "resourceType": "Observation",
            "status": "final",
            "code": {"text": "glucose"},
            "subject": {"reference": subject},
            "specimen": {"reference": spec},
        },
        "request": {"method": "POST", "url": "Observation"},
    }


def bundle(bundle_id: str | None, *entries: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "resourceType": "Bundle",
        "type": "transaction",
        "entry": list(entries),
    }
    if bundle_id is not None:
        result["id"] = bundle_id
    return result


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "fhir.sqlite3"


@pytest.fixture()
def client(db_path):
    app = create_app(Store(db_path))
    with TestClient(app) as test_client:
        yield test_client


def post(client: TestClient, payload: dict[str, Any]):
    return client.post("/", content=json.dumps(payload), headers=FHIR_HEADERS)


def merge(client: TestClient, merge_id: str, source: str, target: str):
    return client.post(
        "/merge",
        content=json.dumps({"mergeId": merge_id, "source": source, "target": target}),
        headers=JSON_HEADERS,
    )


def entry_location(response, index: int) -> str:
    return response.json()["entry"][index]["response"]["location"]


def read(client: TestClient, location: str) -> dict[str, Any]:
    return client.get(location.replace(BASE, "")).json()


def patient_id(location: str) -> str:
    return location.rsplit("/", 1)[-1]


@pytest.fixture()
def two_patients(client):
    """P1 with specimen+observation, P2 alone. Returns (p1, p2, s1, o1) URLs."""
    first = post(
        client,
        bundle(
            "setup-1",
            patient("urn:uuid:p1", "MRN-1", name=[{"family": "Old"}]),
            specimen("urn:uuid:s1", "urn:uuid:p1"),
            observation("urn:uuid:o1", "urn:uuid:p1", "urn:uuid:s1"),
        ),
    )
    assert first.status_code == 200, first.text
    second = post(
        client,
        bundle("setup-2", patient("urn:uuid:p2", "MRN-2", name=[{"family": "Kept"}])),
    )
    assert second.status_code == 200, second.text
    return (
        entry_location(first, 0),
        entry_location(second, 0),
        entry_location(first, 1),
        entry_location(first, 2),
    )


# --------------------------------------------------------------------------- core effects


def test_merge_retargets_lab_data_and_old_address(client, two_patients):
    p1, p2, s1, o1 = two_patients
    response = merge(client, "m-1", p1, p2)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["mergeId"] == "m-1"
    assert result["source"] == p1
    assert result["target"] == p2
    assert result["remappedIdentifiers"] == 1
    assert result["updatedResources"] == 2  # specimen + observation

    # The old patient address reads the retained patient.
    retained = read(client, p1)
    assert retained["id"] == patient_id(p2)
    assert retained["identifier"] == [{"system": "urn:lis:mrn", "value": "MRN-2"}]

    # Existing Specimen/Observation subjects return the retained address.
    assert read(client, s1)["subject"]["reference"] == p2
    stored_obs = read(client, o1)
    assert stored_obs["subject"]["reference"] == p2
    assert stored_obs["specimen"]["reference"] == s1


def test_merge_preserves_retained_demographics(client, two_patients):
    p1, p2, _s1, _o1 = two_patients
    before = read(client, p2)
    assert merge(client, "m-keep", p1, p2).status_code == 200
    after = read(client, p2)
    # The retained patient is byte-identical: no demographic overwrite and the
    # source's identifier is not appended to the stored resource...
    assert after == before
    assert after["identifier"] == [{"system": "urn:lis:mrn", "value": "MRN-2"}]
    # ...but the source's identifier now matches the retained patient.
    reused = post(client, bundle("reuse-src-mrn", patient("urn:uuid:x", "MRN-1")))
    assert reused.status_code == 200
    assert reused.json()["entry"][0]["response"]["status"] == "200 OK"
    assert entry_location(reused, 0) == p2
    assert client.get("/health").json()["resources"] == 4  # no new patient


def test_new_ingest_resolves_old_patient_address(client, two_patients):
    p1, p2, _s1, _o1 = two_patients
    assert merge(client, "m-resolve", p1, p2).status_code == 200

    # A new bundle may still reference the old permanent address; the stored
    # reference carries the retained patient's current address.
    response = post(
        client,
        bundle(
            "after-merge",
            specimen("urn:uuid:s2", p1),
            observation("urn:uuid:o2", p1, "urn:uuid:s2"),
        ),
    )
    assert response.status_code == 200, response.text
    assert read(client, entry_location(response, 0))["subject"]["reference"] == p2
    stored_obs = read(client, entry_location(response, 1))
    assert stored_obs["subject"]["reference"] == p2


def test_same_patient_validation_uses_merged_identity(client, two_patients):
    p1, p2, s1, _o1 = two_patients

    # Before the merge the specimen belongs to a *different* patient: rejected.
    before = post(
        client, bundle("pre-merge-obs", observation("urn:uuid:o", p2, s1))
    )
    assert before.status_code == 422
    assert "same patient" in before.json()["issue"][0]["diagnostics"]

    assert merge(client, "m-topo", p1, p2).status_code == 200

    # After the merge they are the same person: the check passes and the
    # stored subject is the retained patient's address.
    after = post(
        client, bundle("post-merge-obs", observation("urn:uuid:o", p2, s1))
    )
    assert after.status_code == 200, after.text
    assert read(client, entry_location(after, 0))["subject"]["reference"] == p2

    # The old address works too: subject and library specimen both resolve to
    # the retained patient, so the same-patient check compares merged identity.
    via_old = post(
        client, bundle("post-merge-obs-2", observation("urn:uuid:o", p1, s1))
    )
    assert via_old.status_code == 200, via_old.text
    assert read(client, entry_location(via_old, 0))["subject"]["reference"] == p2


# --------------------------------------------------------------------------- validation


def test_merge_unknown_patients_rejected(client, two_patients):
    p1, p2, _s1, _o1 = two_patients
    response = merge(client, "m-nope-src", "Patient/ghost", p2)
    assert response.status_code == 422
    assert response.json()["issue"][0]["code"] == "not-found"

    response = merge(client, "m-nope-tgt", p1, f"{BASE}/Patient/ghost")
    assert response.status_code == 422
    assert response.json()["issue"][0]["code"] == "not-found"


def test_merge_self_is_rejected(client, two_patients):
    p1, _p2, _s1, _o1 = two_patients
    response = merge(client, "m-self", p1, p1)
    assert response.status_code == 422
    assert "different" in response.json()["issue"][0]["diagnostics"]


def test_merged_patient_cannot_be_merged_again(client, two_patients):
    p1, p2, _s1, _o1 = two_patients
    third = post(client, bundle("setup-3", patient("urn:uuid:p3", "MRN-3")))
    p3 = entry_location(third, 0)

    assert merge(client, "m-a", p1, p2).status_code == 200

    # The merged-away patient can be neither source nor target ever again.
    again = merge(client, "m-b", p1, p3)
    assert again.status_code == 422
    assert again.json()["issue"][0]["code"] == "conflict"
    into_merged = merge(client, "m-c", p3, p1)
    assert into_merged.status_code == 422
    assert into_merged.json()["issue"][0]["code"] == "conflict"


def test_merge_chains_trace_to_final_and_never_cycle(client, two_patients):
    p1, p2, s1, _o1 = two_patients
    third = post(client, bundle("setup-c", patient("urn:uuid:p3", "MRN-3")))
    p3 = entry_location(third, 0)

    # The retained patient may itself be merged later: p1 -> p2 -> p3.
    assert merge(client, "m-chain-1", p1, p2).status_code == 200
    assert merge(client, "m-chain-2", p2, p3).status_code == 200

    # Both old addresses read the final retained patient.
    for old in (p1, p2):
        final = read(client, old)
        assert final["id"] == patient_id(p3)
        assert final["identifier"] == [{"system": "urn:lis:mrn", "value": "MRN-3"}]

    # Existing lab data traces to the final patient...
    assert read(client, s1)["subject"]["reference"] == p3
    # ...identifiers of every party match the final patient...
    for mrn, bid in (("MRN-1", "chain-id-1"), ("MRN-2", "chain-id-2")):
        reused = post(client, bundle(bid, patient("urn:uuid:x", mrn)))
        assert entry_location(reused, 0) == p3
    # ...and new references to any old address resolve to the final patient.
    new_spec = post(client, bundle("chain-spec", specimen("urn:uuid:s", p1)))
    assert read(client, entry_location(new_spec, 0))["subject"]["reference"] == p3

    # A cycle would require merging into an already-merged patient: rejected.
    assert merge(client, "m-cycle", p3, p1).status_code == 422
    assert merge(client, "m-cycle-2", p3, p2).status_code == 422


# --------------------------------------------------------------------------- idempotency


def test_merge_idempotent_replay_and_conflict(client, two_patients):
    p1, p2, _s1, _o1 = two_patients
    third = post(client, bundle("setup-i", patient("urn:uuid:p3", "MRN-3")))
    p3 = entry_location(third, 0)

    first = merge(client, "m-idem", p1, p2)
    assert first.status_code == 200

    # Same merge number, same parameters: the original result, byte-identical.
    replay = merge(client, "m-idem", p1, p2)
    assert replay.status_code == 200
    assert replay.content == first.content

    # Same merge number, different parameters: 409, and nothing changes.
    conflict = merge(client, "m-idem", p1, p3)
    assert conflict.status_code == 409
    assert conflict.json()["issue"][0]["code"] == "conflict"
    conflict2 = merge(client, "m-idem", p3, p2)
    assert conflict2.status_code == 409

    # The replay/conflict did not re-apply anything.
    assert client.get("/health").json()["resources"] == 5
    assert read(client, p1)["id"] == patient_id(p2)


def test_failed_merge_records_nothing(client, two_patients):
    p1, p2, _s1, _o1 = two_patients
    failed = merge(client, "m-retry", p1, "Patient/ghost")
    assert failed.status_code == 422

    # The merge number was not burned by the failed attempt.
    fixed = merge(client, "m-retry", p1, p2)
    assert fixed.status_code == 200, fixed.text
    assert read(client, p1)["id"] == patient_id(p2)


def test_merge_survives_restart(db_path):
    with TestClient(create_app(Store(db_path))) as client:
        first = post(
            client,
            bundle(
                "restart-setup",
                patient("urn:uuid:p1", "MRN-R1"),
                specimen("urn:uuid:s1", "urn:uuid:p1"),
            ),
        )
        second = post(client, bundle("restart-setup-2", patient("urn:uuid:p2", "MRN-R2")))
        p1, p2 = entry_location(first, 0), entry_location(second, 0)
        s1 = entry_location(first, 1)
        assert merge(client, "m-restart", p1, p2).status_code == 200
        original = merge(client, "m-restart", p1, p2)

    with TestClient(create_app(Store(db_path))) as restarted:
        # Old address still reads the retained patient...
        assert read(restarted, p1)["id"] == patient_id(p2)
        # ...stored references still point at the retained patient...
        assert read(restarted, s1)["subject"]["reference"] == p2
        # ...identifier matching still maps onto the retained patient...
        reused = post(restarted, bundle("restart-reuse", patient("urn:uuid:x", "MRN-R1")))
        assert entry_location(reused, 0) == p2
        # ...and the merge number still replays the original response.
        replay = merge(restarted, "m-restart", p1, p2)
        assert replay.status_code == 200
        assert replay.content == original.content
        conflict = merge(restarted, "m-restart", p2, p1)
        assert conflict.status_code == 409


def test_bundle_replay_after_merge_returns_original_mapping(client, two_patients):
    p1, p2, _s1, _o1 = two_patients
    payload = bundle(
        "replay-bundle",
        patient("urn:uuid:q", "MRN-Q"),
        specimen("urn:uuid:sq", "urn:uuid:q"),
    )
    first = post(client, payload)
    assert first.status_code == 200
    q_location = entry_location(first, 0)

    # Merge the patient created by that bundle into the retained one.
    assert merge(client, "m-replay", q_location, p2).status_code == 200

    # Replaying the original Bundle.id returns the original mapping (old
    # addresses) and rebuilds nothing, even though the merge moved the data.
    replay = post(client, payload)
    assert replay.status_code == 200
    assert replay.content == first.content
    assert client.get("/health").json()["resources"] == 6
    # The old address from the original mapping still resolves post-merge.
    assert read(client, q_location)["id"] == patient_id(p2)


# --------------------------------------------------------------------------- atomicity & concurrency


def test_merge_and_ingest_interleave_atomically(client, two_patients):
    """Any interleaving of merge and ingest leaves one consistent state."""
    p1, p2, _s1, _o1 = two_patients
    errors: list[str] = []
    barrier = threading.Barrier(6)

    def merger() -> None:
        barrier.wait()
        response = merge(client, "m-race", p1, p2)
        if response.status_code != 200:
            errors.append(f"merge: {response.text}")

    def ingester(index: int) -> None:
        barrier.wait()
        response = post(
            client,
            bundle(
                f"race-{index}",
                specimen("urn:uuid:s", p1),
                observation("urn:uuid:o", p1, "urn:uuid:s"),
            ),
        )
        if response.status_code != 200:
            errors.append(f"ingest {index}: {response.text}")

    threads = [threading.Thread(target=merger)] + [
        threading.Thread(target=ingester, args=(i,)) for i in range(5)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not errors, errors
    # Whether an ingest landed before or after the merge, every stored
    # reference now names the retained patient.
    store = Store(client.app.state.store.db_path)
    conn = store.raw_connection()
    try:
        rows = conn.execute(
            "SELECT content FROM resources WHERE resource_type IN ('Specimen', 'Observation')"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 2 + 2 * 5
    for row in rows:
        content = json.loads(row["content"])
        assert content["subject"]["reference"] == p2
    assert read(client, p1)["id"] == patient_id(p2)


# --------------------------------------------------------------------------- request shape


def test_merge_requires_json_content_type(client):
    response = client.post("/merge", content=b"{}", headers={"content-type": "text/plain"})
    assert response.status_code == 415


@pytest.mark.parametrize("body", [b"{", b"", b"[]", b"null", b'"x"'])
def test_merge_malformed_bodies(client, body):
    response = client.post("/merge", content=body, headers=JSON_HEADERS)
    assert response.status_code == 400
    assert response.json()["resourceType"] == "OperationOutcome"


@pytest.mark.parametrize(
    "payload, expected",
    [
        ({}, "mergeid"),
        ({"mergeId": "m", "source": "Patient/a"}, "target"),
        ({"mergeId": "bad id!", "source": "a", "target": "b"}, "mergeid"),
        ({"mergeId": "m", "source": "", "target": "b"}, "source"),
        ({"mergeId": "m", "source": "Specimen/a", "target": "b"}, "patient"),
        (
            {"mergeId": "m", "source": "https://other.example.org/Patient/a", "target": "b"},
            "patient",
        ),
    ],
)
def test_merge_invalid_shapes_are_rejected(client, payload, expected):
    response = client.post("/merge", content=json.dumps(payload), headers=JSON_HEADERS)
    assert response.status_code == 422, response.text
    assert expected in json.dumps(response.json()).lower()


def test_merge_accepts_bare_ids_and_relative_references(client, two_patients):
    p1, p2, _s1, _o1 = two_patients
    # Bare id for the source, relative reference for the target.
    response = merge(client, "m-forms", patient_id(p1), f"Patient/{patient_id(p2)}")
    assert response.status_code == 200, response.text
    assert response.json()["source"] == p1
    assert response.json()["target"] == p2
