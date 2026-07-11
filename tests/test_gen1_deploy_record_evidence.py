"""Self-consistency checks for the R-PIN gen-1 deploy-record evidence bundle
(store/deploy-records/gen1-20260711/).

These pin down the fixes made in response to Codex CHANGES_REQUESTED on PR #22:

Round 1:

1. attestation.json must not claim capture-verify-output.json (a later,
   separate gen-2 capture-dry-run observation) is a re-verification of gen-1's
   own manifest. It must be labeled as such, and any agreement claim must be
   backed by a real, machine-checkable field diff (field-agreement-check.json).
2. source-lock.snapshot.json's byte-verbatim captured `source_repo.role`
   string must remain untouched, and carry a caveat that it does not reflect
   the currently adopted multi-repo operating model.

Round 2 (Codex flagged that round 1's caveat, appended in place as
source_repo.role_provenance_note, changed source-lock.snapshot.json's bytes --
so attestation.json.source_lock_sha256 ended up binding an annotated
derivative rather than the raw captured file, defeating the point of an
immutable evidence snapshot):

3. source-lock.snapshot.json must be restored to its ORIGINAL byte-verbatim
   gen-1 capture content (sha256 ca31e11c...), with no annotation of any kind.
4. The role-authority caveat now lives in a separate sidecar file,
   source-lock-role-disclaimer.json, which references the raw file's hash and
   the specific field (source_repo.role) it caveats, without altering it.
5. attestation.json.source_lock_sha256 must equal the raw file's hash, and
   attestation.json must point to the sidecar via
   source_lock_role_disclaimer_ref rather than describing an in-place edit.

If any of these regress (e.g. someone reverts to the old "dry-run re-capture
agrees with the written gen-1 manifest" framing, silently rewrites the
captured role string, or re-annotates source-lock.snapshot.json in place),
these tests fail.
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
    """Codex point 2 (round 1): do not rewrite the captured lock-file bytes."""
    source_lock = _load("source-lock.snapshot.json")
    assert (
        source_lock["source_repo"]["role"]
        == "permanent umbrella/orchestrator and rollback source"
    )


def test_source_lock_snapshot_has_no_in_place_annotation() -> None:
    """Codex P0 (round 2): source-lock.snapshot.json must be the RAW gen-1
    capture with no added fields of any kind -- an in-place annotation (even
    a well-intentioned caveat) changes the file's bytes and breaks the
    byte-verbatim evidence guarantee. The caveat belongs in a sidecar
    instead (see test_source_lock_role_disclaimer_sidecar_* below)."""
    source_lock = _load("source-lock.snapshot.json")
    assert "role_provenance_note" not in source_lock["source_repo"]
    assert set(source_lock["source_repo"].keys()) == {
        "name",
        "role",
        "local_path",
        "remote",
        "never_delete",
    }


def test_source_lock_snapshot_hash_matches_attested_gen1_value() -> None:
    """Codex's exact round-2 ask: recompute source-lock.snapshot.json's actual
    sha256 and assert it equals the value attested as the gen-1 source lock.
    This is the regression guard proving the raw evidence file can never
    silently drift from what attestation.json claims it is again."""
    attestation = _load("attestation.json")
    actual = _sha256(RECORD_DIR / "source-lock.snapshot.json")
    assert actual == "ca31e11ce2846cce1b81968e57351286f0f3e97bcf964e85be37ca12c0b3fb36"
    assert attestation["source_lock_sha256"] == actual


def test_source_lock_role_disclaimer_sidecar_references_raw_file_and_field() -> None:
    """The role-authority caveat must live in a separate sidecar that
    provably refers to the specific raw file (by hash) and specific field
    (source_repo.role) it caveats, rather than being appended in place."""
    sidecar = _load("source-lock-role-disclaimer.json")
    raw_sha = _sha256(RECORD_DIR / "source-lock.snapshot.json")

    assert sidecar["subject_file"]["path"].endswith("source-lock.snapshot.json")
    assert sidecar["subject_file"]["sha256"] == raw_sha
    assert sidecar["subject_field"]["json_pointer"] == "/source_repo/role"


def test_source_lock_role_disclaimer_states_authority_caveat() -> None:
    """Codex point 2 substance, preserved: the sidecar must state the role
    string is historical lock-file provenance and that RenQuant is not a
    runtime/deployment/schedule/artifact/orchestration authority under the
    adopted multi-repo model."""
    sidecar = _load("source-lock-role-disclaimer.json")
    disclaimer = sidecar["disclaimer"]
    lowered = disclaimer.lower()
    for term in ("runtime", "deployment", "schedule", "artifact", "orchestration"):
        assert term in lowered, f"caveat missing '{term}' authority disclaimer"
    assert "subrepo-operating-model.md" in disclaimer
    assert "renquant-orchestrator" in lowered


def test_attestation_points_to_sidecar_not_an_in_place_note() -> None:
    """attestation.json must reference the sidecar (so a reader can find the
    caveat) and must NOT carry a stale source_lock_provenance_note describing
    a hash change that, after this fix, no longer exists."""
    attestation = _load("attestation.json")
    assert "source_lock_provenance_note" not in attestation

    ref = attestation["source_lock_role_disclaimer_ref"]
    sidecar_path = RECORD_DIR / "source-lock-role-disclaimer.json"
    assert ref["path"].endswith("source-lock-role-disclaimer.json")
    assert ref["sha256"] == _sha256(sidecar_path)
