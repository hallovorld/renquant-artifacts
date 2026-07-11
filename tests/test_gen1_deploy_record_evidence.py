"""Self-consistency checks for the R-PIN gen-1 deploy-record evidence bundle
(store/deploy-records/gen1-20260711/).

These pin down the fixes made in response to Codex CHANGES_REQUESTED on PR #22:

1. attestation.json must not claim capture-verify-output.json (a later,
   separate gen-2 capture-dry-run observation) is a re-verification of gen-1's
   own manifest. It must be labeled as such, and any agreement claim must be
   backed by a real, machine-checkable field diff (field-agreement-check.json).
2. source-lock.snapshot.json's byte-verbatim captured `source_repo.role`
   string must remain untouched, but must carry an added caveat that it does
   not reflect the currently adopted multi-repo operating model.

If either regresses (e.g. someone reverts to the old "dry-run re-capture
agrees with the written gen-1 manifest" framing, or silently rewrites the
captured role string), these tests fail.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).parents[1]
RECORD_DIR = ROOT / "store" / "deploy-records" / "gen1-20260711"

REPO_FIELDS = ("remote", "branch", "commit", "role", "status")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load(name: str) -> dict:
    return json.loads((RECORD_DIR / name).read_text())


def test_attestation_hash_bindings_match_committed_files() -> None:
    attestation = _load("attestation.json")
    assert attestation["source_lock_sha256"] == _sha256(RECORD_DIR / "source-lock.snapshot.json")
    assert attestation["runtime_inventory_sha256"] == _sha256(
        RECORD_DIR / "runtime-inventory.snapshot.json"
    )
    assert attestation["verification"]["output_sha256"] == _sha256(
        RECORD_DIR / "capture-verify-output.json"
    )
    assert attestation["verification"]["field_agreement_check_sha256"] == _sha256(
        RECORD_DIR / "field-agreement-check.json"
    )


def test_capture_verify_output_is_labeled_as_a_later_gen2_observation() -> None:
    """Regression guard for Codex point 1: capture-verify-output.json is a
    gen-2 observation, not a gen-1 re-verification, and attestation.json must
    say so rather than claiming agreement-with-gen-1 as proof."""
    attestation = _load("attestation.json")
    gen2 = _load("capture-verify-output.json")

    assert gen2["manifest"]["generation"] == 2
    assert gen2["manifest"]["deployment"]["state"] == "captured"
    assert (
        gen2["manifest"]["deployment"]["supersedes_sha256"]
        == attestation["manifest_gen1_sha256"]
    )

    verification = attestation["verification"]
    note = verification["note"].lower()
    # Must not repeat the retracted false-equivalence framing.
    assert "dry-run re-capture agrees with the written gen-1 manifest" not in note
    # Must accurately characterize what capture-verify-output.json is.
    assert "later" in note and "gen-2" in note
    assert "not" in note and (
        "re-verification" in note or "reproduction" in note
    )


def test_field_agreement_check_reports_no_drift_and_is_independently_reproducible() -> None:
    """Recompute the gen1-input vs gen2-observation diff directly from the
    committed source files (not just trust the checked-in summary) and assert
    it matches what field-agreement-check.json claims."""
    source_lock = _load("source-lock.snapshot.json")
    gen2 = _load("capture-verify-output.json")
    check = _load("field-agreement-check.json")

    left_repos = {
        r["name"]: {k: r[k] for k in REPO_FIELDS} for r in source_lock["subrepos"]
    }
    right_repos = {
        name: {k: v[k] for k in REPO_FIELDS}
        for name, v in gen2["manifest"]["repos"].items()
    }
    assert left_repos == right_repos, "recomputed repos diff disagrees with source data"

    assert check["diff_count"] == 0
    assert check["diffs"] == []
    assert check["verdict"] == "no_drift_detected"
    assert check["host_match"] is True
    for name, entry in check["per_repo"].items():
        assert entry["match"] is True, f"{name} field mismatch recorded in field-agreement-check.json"

    # The artifact must be honest that this is NOT proof gen-1's original
    # capture was itself correct at gen-1 time.
    meaning = check["evidentiary_meaning"].lower()
    assert "not proof" in meaning or "is not proof" in meaning
    assert "drift" in meaning


def test_field_agreement_check_declares_artifact_store_out_of_scope() -> None:
    """manifest.artifact_store has no gen-1-attested source committed in this
    bundle; the check must say so rather than silently ignoring it."""
    check = _load("field-agreement-check.json")
    assert any("artifact_store" in item for item in check["fields_not_compared"])


def test_source_repo_role_string_is_untouched_verbatim() -> None:
    """Codex point 2: do not rewrite the captured lock-file bytes."""
    source_lock = _load("source-lock.snapshot.json")
    assert (
        source_lock["source_repo"]["role"]
        == "permanent umbrella/orchestrator and rollback source"
    )


def test_source_repo_role_has_authority_caveat() -> None:
    """Codex point 2: an added annotation must state the role string is
    historical and that RenQuant is not a runtime/deployment/schedule/
    artifact/orchestration authority under the adopted multi-repo model."""
    source_lock = _load("source-lock.snapshot.json")
    note = source_lock["source_repo"].get("role_provenance_note", "")
    assert note, "source_repo.role_provenance_note must be present"
    lowered = note.lower()
    for term in ("runtime", "deployment", "schedule", "artifact", "orchestration"):
        assert term in lowered, f"caveat missing '{term}' authority disclaimer"
    assert "subrepo-operating-model.md" in note
    assert "renquant-orchestrator" in lowered
