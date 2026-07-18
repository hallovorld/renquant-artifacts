"""Reader-protocol tests (RFC RenQuant#492 §2.6): resolve -> PREPARE audit
-> dirfd-open -> digest-verify -> serve; fail-closed on invalid schema,
extra/missing members, digest drift, stale pointers, and log corruption."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from bundle_helpers import (
    CAL,
    PANEL,
    make_members,
    make_store,
    publish_simple,
)
from renquant_artifacts.bundle_schema import (
    MANIFEST_FILENAME,
    canonical_manifest_bytes,
    compute_manifest_digest,
)
from renquant_artifacts.bundle_store import BundleReadRefusedError


def test_resolve_active_happy_path(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    result = publish_simple(store, "x")
    with store.resolve_active() as resolved:
        assert resolved.bundle_id == result.bundle_id
        assert resolved.generation == 1
        assert resolved.crash_interval is False
        assert resolved.manifest.manifest_digest == result.manifest.manifest_digest
        members = make_members("x")
        assert resolved.read_member(PANEL) == members[PANEL]
        assert resolved.read_member(CAL) == members[CAL]


def test_no_active_pointer_refused(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    with pytest.raises(BundleReadRefusedError, match="no ACTIVE"):
        store.resolve_active()


def test_malformed_active_pointer_refused(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    publish_simple(store)
    (tmp_path / "ACTIVE").write_text("not a pointer at all\n")
    with pytest.raises(BundleReadRefusedError, match="malformed ACTIVE"):
        store.resolve_active()


def test_binary_corrupted_active_pointer_refused(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    publish_simple(store)
    (tmp_path / "ACTIVE").write_bytes(b"\xff\xfe\x00garbage")
    with pytest.raises(BundleReadRefusedError, match="malformed ACTIVE"):
        store.resolve_active()


def test_member_digest_drift_refused(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    result = publish_simple(store)
    member = tmp_path / "bundles" / result.bundle_id / CAL
    member.write_bytes(b'{"calibration": "tampered"}\n')
    with pytest.raises(BundleReadRefusedError, match="digest verification"):
        store.resolve_active()


def test_manifest_tamper_without_restamp_refused(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    result = publish_simple(store)
    manifest_path = tmp_path / "bundles" / result.bundle_id / MANIFEST_FILENAME
    payload = json.loads(manifest_path.read_text())
    payload["bindings"]["scorer_fingerprint"] = "tampered"
    manifest_path.write_bytes(canonical_manifest_bytes(payload))
    with pytest.raises(BundleReadRefusedError, match="manifest_digest mismatch"):
        store.resolve_active()


def test_unknown_schema_field_injection_refused(tmp_path: Path) -> None:
    """Even a digest-consistent restamp cannot smuggle an unknown field."""
    store = make_store(tmp_path)
    result = publish_simple(store)
    manifest_path = tmp_path / "bundles" / result.bundle_id / MANIFEST_FILENAME
    payload = json.loads(manifest_path.read_text())
    payload["smuggled"] = True
    payload["manifest_digest"] = compute_manifest_digest(payload)
    manifest_path.write_bytes(canonical_manifest_bytes(payload))
    with pytest.raises(BundleReadRefusedError, match="unknown field"):
        store.resolve_active()


def test_extra_member_file_refused(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    result = publish_simple(store)
    (tmp_path / "bundles" / result.bundle_id / "extra.json").write_text("{}")
    with pytest.raises(BundleReadRefusedError, match="member set mismatch"):
        store.resolve_active()


def test_missing_member_file_refused(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    result = publish_simple(store)
    (tmp_path / "bundles" / result.bundle_id / CAL).unlink()
    with pytest.raises(BundleReadRefusedError, match="member set mismatch"):
        store.resolve_active()


def test_stale_pointer_generation_regression_refused(tmp_path: Path) -> None:
    """A hand-restored old ACTIVE (generation regression outside
    --rollback-to) is detected against the ACTIVATE history and refused."""
    store = make_store(tmp_path)
    first = publish_simple(store, "a")
    publish_simple(store, "b")
    # regress the pointer to the (still archived, still PREPARE-audited)
    # first generation — exactly what a naive restore-from-backup does
    (tmp_path / "ACTIVE").write_text(f"1 {first.bundle_id}\n")
    with pytest.raises(BundleReadRefusedError, match="regression"):
        store.resolve_active()


def test_prepare_bundle_id_mismatch_refused(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    first = publish_simple(store, "a")
    publish_simple(store, "b")
    # pointer generation 2 but naming the WRONG bundle
    (tmp_path / "ACTIVE").write_text(f"2 {first.bundle_id}\n")
    with pytest.raises(BundleReadRefusedError, match="PREPARE record names"):
        store.resolve_active()


def test_corrupt_operations_log_line_refused(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    publish_simple(store, "a")
    publish_simple(store, "b")
    log_path = tmp_path / "bundles" / "OPERATIONS.jsonl"
    lines = log_path.read_bytes().splitlines(keepends=True)
    lines[1] = b"### corrupted ###\n"
    log_path.write_bytes(b"".join(lines))
    with pytest.raises(BundleReadRefusedError, match="corrupt"):
        store.resolve_active()


def test_torn_final_log_line_tolerated(tmp_path: Path) -> None:
    """A torn tail (append that never completed) is not a record; the
    records that DO exist keep serving."""
    store = make_store(tmp_path)
    result = publish_simple(store)
    log_path = tmp_path / "bundles" / "OPERATIONS.jsonl"
    with log_path.open("ab") as fh:
        fh.write(b'{"record":"PREP')  # no newline: torn append
    with store.resolve_active() as resolved:
        assert resolved.bundle_id == result.bundle_id


def test_resolve_bundle_archive_replay(tmp_path: Path) -> None:
    """Run-bundle replay shape (§2.2/§4): re-resolve a recorded
    {bundle_id, manifest_digest, member digests} against the archive and
    re-verify, long after the pointer moved on."""
    store = make_store(tmp_path)
    first = publish_simple(store, "a")
    run_bundle_record = {
        "bundle_id": first.bundle_id,
        "manifest_digest": first.manifest.manifest_digest,
        "members": {
            name: digest.sha256 for name, digest in first.manifest.members.items()
        },
        "pointer_generation": first.generation,
    }
    publish_simple(store, "b")
    publish_simple(store, "c")

    with store.resolve_bundle(run_bundle_record["bundle_id"]) as archived:
        assert archived.manifest.manifest_digest == run_bundle_record["manifest_digest"]
        for name, sha in run_bundle_record["members"].items():
            assert archived.manifest.members[name].sha256 == sha
        assert archived.read_member(PANEL) == make_members("a")[PANEL]


def test_resolve_bundle_unknown_id_refused(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    publish_simple(store)
    with pytest.raises(BundleReadRefusedError, match="not in the archive"):
        store.resolve_bundle("20260101T000000Z-" + "0" * 16)
    with pytest.raises(BundleReadRefusedError, match="malformed"):
        store.resolve_bundle("../../etc/passwd")


def test_resolve_bundle_detects_archive_corruption(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    first = publish_simple(store, "a")
    publish_simple(store, "b")
    (tmp_path / "bundles" / first.bundle_id / PANEL).write_bytes(b"rotted")
    with pytest.raises(BundleReadRefusedError, match="digest verification"):
        store.resolve_bundle(first.bundle_id)
