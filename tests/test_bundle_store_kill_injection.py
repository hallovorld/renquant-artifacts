"""Kill-injection suite (RFC RenQuant#492 §2.3 activation-audit invariant,
§4 acceptance).

A crash is injected at EVERY numbered writer step and after EVERY fsync
(the full :data:`WRITER_CHECKPOINTS` matrix) via a ``crash_hook`` that
raises at exactly one checkpoint; the store's lock fd is closed on the way
out, exactly as flock is released on process death. After each simulated
crash a FRESH store instance (the "next process") must uphold:

* THE invariant: a reader NEVER serves a generation without its PREPARE
  record — asserted after every single crash point;
* crash at/before step 9's rename: the previous ACTIVE is intact;
* crash in the rename->ACTIVATE interval (SPECIFICALLY step 9 rename and
  its dirfd fsync): the new generation serves — it was fully prepared and
  validated, and its PREPARE is durable BECAUSE step 7 precedes the flip —
  flagged as a crash interval, alarmed, and every mutation is refused
  until a RECOVERY record is committed;
* dangling PREPARE (crash between steps 7 and 9): mutations refuse until
  RECOVERY; the recovered generation number is never reused.

The harness is the monkeypatched-crash style sanctioned by §4: it proves
the crash-at-point semantics of the on-disk state sequence (true
power-loss buffer reordering is out of unit-test reach; the fsync ordering
is enforced by construction and inspected by the checkpoint labels).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from bundle_helpers import (
    AlarmRecorder,
    make_breakglass_authorization,
    make_members,
    make_store,
    publish_simple,
)
from renquant_artifacts.bundle_store import (
    WRITER_CHECKPOINTS,
    BundleStore,
    RecoveryRequiredError,
)


class InjectedCrash(RuntimeError):
    pass


def crash_at(label: str):
    def hook(checkpoint: str) -> None:
        if checkpoint == label:
            raise InjectedCrash(label)

    return hook


IDX = {label: i for i, label in enumerate(WRITER_CHECKPOINTS)}
PREPARE_WRITTEN = IDX["step7:prepare-written"]
ACTIVE_RENAMED = IDX["step9:active-renamed"]
ACTIVATE_WRITTEN = IDX["step10:activate-written"]

#: the §4 "SPECIFICALLY" interval: pointer flipped, ACTIVATE not yet logged
RENAME_TO_ACTIVATE_INTERVAL = ("step9:active-renamed", "step9:store-root-fsynced")


def _assert_serving_is_audited(store: BundleStore) -> int:
    """THE invariant: whatever resolve_active serves has a PREPARE record."""
    with store.resolve_active() as resolved:
        generation = resolved.generation
    prepares = {
        rec["generation"]
        for rec in store.read_operations()
        if rec["record"] == "PREPARE"
    }
    assert generation in prepares, (
        f"reader served generation {generation} without a PREPARE record"
    )
    return generation


@pytest.mark.parametrize("label", WRITER_CHECKPOINTS)
def test_kill_injection_full_writer_matrix(tmp_path: Path, label: str) -> None:
    # generation 1: a healthy genesis bundle
    setup_store = make_store(tmp_path)
    genesis = publish_simple(setup_store, "genesis")

    # the doomed writer, crashing at exactly `label`
    doomed = make_store(tmp_path, crash_hook=crash_at(label))
    with pytest.raises(InjectedCrash):
        publish_simple(doomed, "candidate")

    # "next process": fresh store, fresh alarm recorder
    alarms = AlarmRecorder()
    store = make_store(tmp_path, alarm_hook=alarms)
    idx = IDX[label]

    served = _assert_serving_is_audited(store)

    if idx < PREPARE_WRITTEN:
        # nothing audit-relevant happened: previous ACTIVE intact,
        # no dangling PREPARE, next publish proceeds directly.
        assert served == 1
        with store.resolve_active() as resolved:
            assert resolved.bundle_id == genesis.bundle_id
            assert resolved.crash_interval is False
            assert resolved.read_member("panel-ltr.alpha158_fund.json") == make_members(
                "genesis"
            )["panel-ltr.alpha158_fund.json"]
        assert alarms.events == []
        assert store.recovery_required() is False
        after = publish_simple(store, "after-crash")
        assert after.generation == 2

    elif idx < ACTIVE_RENAMED:
        # PREPARE durable, pointer NOT flipped: previous ACTIVE serves
        # cleanly; the dangling PREPARE blocks all mutation until RECOVERY;
        # the prepared-but-never-activated generation is never reused.
        assert served == 1
        with store.resolve_active() as resolved:
            assert resolved.bundle_id == genesis.bundle_id
            assert resolved.crash_interval is False
        with pytest.raises(RecoveryRequiredError):
            publish_simple(store, "blocked")
        with pytest.raises(RecoveryRequiredError):
            store.collect_garbage(is_referenced=lambda b: False)
        with pytest.raises(RecoveryRequiredError):
            store.rollback_to(
                genesis.bundle_id,
                authorization=make_breakglass_authorization("INC-KILL"),
            )
        assert store.recovery_required() is True

        recovered = store.commit_recovery(
            actor={"os_user": "test", "operator": "renhao"},
            note=f"kill-injection at {label}",
        )
        assert recovered == [2]
        assert alarms.kinds() == ["recovery_committed"]
        after = publish_simple(store, "after-recovery")
        assert after.generation == 3  # 2 is burned, never reused
        assert _assert_serving_is_audited(store) == 3

    elif idx < ACTIVATE_WRITTEN:
        # THE rename->ACTIVATE interval: pointer flipped, ACTIVATE missing.
        # The new generation serves ONLY because its PREPARE preceded the
        # flip; it is flagged + alarmed, and mutation requires RECOVERY.
        assert label in RENAME_TO_ACTIVATE_INTERVAL
        assert served == 2
        with store.resolve_active() as resolved:
            assert resolved.generation == 2
            assert resolved.bundle_id != genesis.bundle_id
            assert resolved.crash_interval is True
            assert resolved.recovery_required is True
            # the fully-prepared state is intact and digest-verified
            assert resolved.read_member("panel-rank-calibration.json") == make_members(
                "candidate"
            )["panel-rank-calibration.json"]
        assert "crash_interval_serve" in alarms.kinds()
        with pytest.raises(RecoveryRequiredError):
            publish_simple(store, "blocked")
        recovered = store.commit_recovery(
            actor={"os_user": "test", "operator": "renhao"},
            note=f"kill-injection at {label}",
        )
        assert recovered == [2]
        # after RECOVERY the same generation serves CLEAN (interval audited)
        quiet = AlarmRecorder()
        clean_store = make_store(tmp_path, alarm_hook=quiet)
        with clean_store.resolve_active() as resolved:
            assert resolved.generation == 2
            assert resolved.crash_interval is False
        assert quiet.events == []
        after = publish_simple(clean_store, "after-recovery")
        assert after.generation == 3

    else:
        # ACTIVATE record written (its fsync/unlock alone was lost): the
        # commit is complete and the store is fully healthy.
        assert served == 2
        with store.resolve_active() as resolved:
            assert resolved.crash_interval is False
        assert store.recovery_required() is False
        assert alarms.events == []
        after = publish_simple(store, "after-crash")
        assert after.generation == 3


def test_kill_matrix_covers_every_step_and_every_fsync() -> None:
    """The matrix parametrization is the full §2.3 protocol: all 10
    numbered steps appear, and every fsync site has an 'after' point."""
    steps = {label.split(":")[0] for label in WRITER_CHECKPOINTS}
    assert steps == {f"step{i}" for i in range(1, 11)}
    fsync_points = [label for label in WRITER_CHECKPOINTS if "fsynced" in label]
    # member x2, manifest, tmp dirfd, bundles dirfd, PREPARE, ACTIVE.tmp,
    # store-root dirfd, ACTIVATE = 9 fsync sites
    assert len(fsync_points) == 9


def test_rollback_kill_in_rename_to_activate_interval(tmp_path: Path) -> None:
    """The same §2.3 invariant holds for the rollback flip path."""
    store = make_store(tmp_path)
    genesis = publish_simple(store, "genesis")
    publish_simple(store, "second")

    doomed = make_store(tmp_path, crash_hook=crash_at("step9:active-renamed"))
    with pytest.raises(InjectedCrash):
        doomed.rollback_to(
            genesis.bundle_id,
            authorization=make_breakglass_authorization("INC-RB"),
        )

    alarms = AlarmRecorder()
    fresh = make_store(tmp_path, alarm_hook=alarms)
    with fresh.resolve_active() as resolved:
        # pointer flipped back to genesis under a NEW generation with a
        # durable PREPARE; crash interval detected
        assert resolved.bundle_id == genesis.bundle_id
        assert resolved.generation == 3
        assert resolved.crash_interval is True
    assert "crash_interval_serve" in alarms.kinds()
    with pytest.raises(RecoveryRequiredError):
        publish_simple(fresh, "blocked")
    assert fresh.commit_recovery(
        actor={"os_user": "test", "operator": "renhao"}, note="rollback kill"
    ) == [3]
    assert publish_simple(fresh, "onwards").generation == 4


def test_hand_flipped_pointer_without_prepare_is_refused(tmp_path: Path) -> None:
    """Adversarial writer that flips BEFORE preparing (the ordering the
    protocol forbids): the reader must fail closed (§2.3: NO PREPARE =>
    REFUSED, never serve unaudited state)."""
    from renquant_artifacts.bundle_store import BundleReadRefusedError

    store = make_store(tmp_path)
    result = publish_simple(store, "genesis")
    # simulate the forbidden state: pointer names a generation for which
    # no PREPARE record was ever written
    (tmp_path / "ACTIVE").write_text(f"7 {result.bundle_id}\n")
    with pytest.raises(BundleReadRefusedError, match="NO PREPARE"):
        store.resolve_active()
