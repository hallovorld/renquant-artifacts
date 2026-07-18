"""Writer-protocol tests (RFC RenQuant#492 §2.3, §2.1): the 10-step
durability-ordered commit, pointer format, generation monotonicity, the
pluggable pair-validator seam, host-model refusal, and collision abort."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from bundle_helpers import (
    CAL,
    PANEL,
    AlarmRecorder,
    make_authorization,
    make_bindings,
    make_members,
    make_store,
    publish_simple,
)
from renquant_artifacts.bundle_schema import (
    BundleSchemaError,
    MANIFEST_FILENAME,
    sha256_hex,
)
from renquant_artifacts.bundle_store import (
    BundleCollisionError,
    BundleStore,
    BundleValidationError,
    NonLocalStoreError,
    default_local_mount_guard,
)


def test_publish_genesis_layout_and_pointer(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    result = publish_simple(store, "a")

    assert result.generation == 1
    assert result.manifest.parent_bundle is None
    # pointer format: "<generation> <bundle_id>"
    assert (tmp_path / "ACTIVE").read_text() == f"1 {result.bundle_id}\n"
    bundle_dir = tmp_path / "bundles" / result.bundle_id
    assert sorted(p.name for p in bundle_dir.iterdir()) == sorted(
        [MANIFEST_FILENAME, PANEL, CAL]
    )
    members = make_members("a")
    assert (bundle_dir / PANEL).read_bytes() == members[PANEL]
    manifest_payload = json.loads((bundle_dir / MANIFEST_FILENAME).read_text())
    assert manifest_payload["members"][PANEL]["sha256"] == sha256_hex(members[PANEL])
    assert manifest_payload["members"][PANEL]["bytes"] == len(members[PANEL])


def test_publish_operation_log_prepare_then_activate(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    result = publish_simple(store)

    records = store.read_operations()
    assert [r["record"] for r in records] == ["PREPARE", "ACTIVATE"]
    prepare, activate = records
    assert prepare["generation"] == activate["generation"] == 1
    assert prepare["bundle_id"] == activate["bundle_id"] == result.bundle_id
    assert prepare["authorization"]["tool"] == "wf_promote"
    # ACTIVATE is cryptographically bound to its PREPARE line
    raw_lines = (tmp_path / "bundles" / "OPERATIONS.jsonl").read_bytes().splitlines()
    prepare_line = raw_lines[0] + b"\n"
    assert activate["prepare_sha256"] == sha256_hex(prepare_line)


def test_second_publish_links_parent_and_increments_generation(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    first = publish_simple(store, "a")
    second = publish_simple(store, "b")

    assert second.generation == 2
    assert second.manifest.parent_bundle == first.bundle_id
    assert (tmp_path / "ACTIVE").read_text() == f"2 {second.bundle_id}\n"
    # the first bundle is retained immutably
    assert (tmp_path / "bundles" / first.bundle_id / PANEL).exists()


def test_publish_requires_exact_member_set(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    with pytest.raises(BundleValidationError, match="member set"):
        store.publish(
            {PANEL: b"data"},
            bindings=make_bindings(),
            authorization=make_authorization(),
        )
    with pytest.raises(BundleValidationError, match="member set"):
        store.publish(
            {**make_members(), "extra.json": b"x"},
            bindings=make_bindings(),
            authorization=make_authorization(),
        )
    assert not (tmp_path / "ACTIVE").exists()


def test_publish_validates_authorization_up_front(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    bad = make_authorization()
    del bad["tool_version"]
    with pytest.raises(BundleSchemaError):
        store.publish(make_members(), bindings=make_bindings(), authorization=bad)
    assert not (tmp_path / "bundles").exists() or not any(
        (tmp_path / "bundles").glob("2*")
    )


# -- pair-validator seam (§2.5, pluggable in phase 1) --------------------


def test_pair_validator_receives_manifest_and_member_paths(tmp_path: Path) -> None:
    seen: list[tuple[dict, dict]] = []

    def validator(manifest_payload: dict, member_paths: dict) -> None:
        seen.append((manifest_payload, member_paths))

    store = make_store(tmp_path, pair_validator=validator)
    result = publish_simple(store)

    assert len(seen) == 1
    payload, paths = seen[0]
    assert payload["manifest_digest"] == result.manifest.manifest_digest
    assert set(paths) == {PANEL, CAL}
    assert all(isinstance(p, Path) and p.is_file() for p in paths.values())


class _Verdict:
    def __init__(self, ok: bool) -> None:
        self.ok = ok


@pytest.mark.parametrize(
    "validator",
    [
        lambda m, p: False,
        lambda m, p: _Verdict(ok=False),
        lambda m, p: (_ for _ in ()).throw(ValueError("pair mismatch")),
    ],
    ids=["returns-false", "verdict-not-ok", "raises"],
)
def test_pair_validator_rejection_aborts_before_prepare(tmp_path: Path, validator) -> None:
    store = make_store(tmp_path, pair_validator=validator)
    with pytest.raises(BundleValidationError):
        publish_simple(store)
    # step 6 failure: bundle dir deleted, no PREPARE, no pointer
    assert not any((tmp_path / "bundles").glob("2*"))
    assert not (tmp_path / "ACTIVE").exists()
    assert store.read_operations() == []


@pytest.mark.parametrize(
    "validator", [lambda m, p: None, lambda m, p: True, lambda m, p: _Verdict(ok=True)],
    ids=["none", "true", "verdict-ok"],
)
def test_pair_validator_pass_shapes(tmp_path: Path, validator) -> None:
    store = make_store(tmp_path, pair_validator=validator)
    assert publish_simple(store).generation == 1


# -- host model (§2.1) ---------------------------------------------------


def test_non_local_store_path_refused_at_open(tmp_path: Path) -> None:
    with pytest.raises(NonLocalStoreError, match="host model"):
        BundleStore(
            tmp_path, local_mount_guard=lambda p: (False, "nfs mount detected")
        )


def test_default_local_mount_guard_accepts_local_tmp(tmp_path: Path) -> None:
    ok, reason = default_local_mount_guard(tmp_path)
    assert ok, reason


def test_default_guard_is_the_default(tmp_path: Path) -> None:
    # constructing without injection must run the real guard and pass on
    # a local tmp dir (macOS APFS / CI ext4-tmpfs)
    BundleStore(tmp_path)


# -- identity collision (§2.2) -------------------------------------------


def test_bundle_id_collision_aborts(tmp_path: Path) -> None:
    # a genuinely FIXED clock: same created_at every call
    from datetime import datetime, timezone

    moment = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)
    store = make_store(tmp_path, clock=lambda: moment)
    first = publish_simple(store, "same")
    # Remove ACTIVE so the second publish also builds a genesis manifest
    # (same parent=None, same created_at, same content => same bundle_id).
    (tmp_path / "ACTIVE").unlink()
    with pytest.raises(BundleCollisionError):
        publish_simple(store, "same")
    # the original archived bundle is untouched
    assert (tmp_path / "bundles" / first.bundle_id / MANIFEST_FILENAME).exists()


def test_stale_active_tmp_is_overwritten(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    (tmp_path / "ACTIVE.tmp").write_text("junk from a dead writer")
    result = publish_simple(store)
    assert (tmp_path / "ACTIVE").read_text() == f"1 {result.bundle_id}\n"
    assert not (tmp_path / "ACTIVE.tmp").exists()


def test_breakglass_publish_fires_alarm(tmp_path: Path) -> None:
    alarms = AlarmRecorder()
    store = make_store(tmp_path, alarm_hook=alarms)
    publish_simple(store)  # normal tool: no alarm
    from bundle_helpers import make_breakglass_authorization

    publish_simple(store, "fix", authorization=make_breakglass_authorization("INC-7"))
    assert alarms.kinds() == ["breakglass_commit"]
    assert alarms.events[0][1]["incident_ref"] == "INC-7"
