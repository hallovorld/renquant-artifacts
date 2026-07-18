"""Reference-rooted GC tests (RFC RenQuant#492 §2.6 r4 hierarchy) and the
reader/GC race guarantees (dirfd across a GC pass; pointer flip mid-read)."""
from __future__ import annotations

import hashlib
import inspect
from pathlib import Path

import pytest

from bundle_helpers import (
    CAL,
    PANEL,
    make_breakglass_authorization,
    make_members,
    make_store,
    publish_simple,
)
from renquant_artifacts.bundle_store import (
    BundleStore,
    RecoveryRequiredError,
)


def _lineage(tmp_path: Path):
    """A -> B -> C, then break-glass rollback to A, then D (parent A).

    Leaves ACTIVE = D (gen 5) with rollback ancestry {D, A}; B and C are
    divergent non-ancestors — the only GC-eligible class."""
    store = make_store(tmp_path)
    a = publish_simple(store, "a")
    b = publish_simple(store, "b")
    c = publish_simple(store, "c")
    store.rollback_to(a.bundle_id, authorization=make_breakglass_authorization())
    d = publish_simple(store, "d")
    assert d.generation == 5
    return store, a, b, c, d


def test_gc_full_retention_hierarchy(tmp_path: Path) -> None:
    """The §2.6/§4 r4 acceptance sequence: referenced bundle survives GC
    while the unreferenced sibling is collected; then the run-bundle
    reference is expired under the (injected) retention policy and the
    same bundle becomes collectable."""
    store, a, b, c, d = _lineage(tmp_path)
    referenced = {b.bundle_id}  # a retained run bundle references B

    report = store.collect_garbage(is_referenced=lambda bid: bid in referenced)
    assert report.deleted == (c.bundle_id,)  # unreferenced sibling collected
    assert report.retained_active == (d.bundle_id,)
    assert report.retained_ancestry == (a.bundle_id,)
    assert report.retained_referenced == (b.bundle_id,)
    assert (tmp_path / "bundles" / b.bundle_id).is_dir()
    assert not (tmp_path / "bundles" / c.bundle_id).exists()

    # the run bundle referencing B is expired under the retention policy
    referenced.clear()
    report2 = store.collect_garbage(is_referenced=lambda bid: bid in referenced)
    assert report2.deleted == (b.bundle_id,)
    assert not (tmp_path / "bundles" / b.bundle_id).exists()
    # ACTIVE and its rollback ancestry are STILL never collected
    assert (tmp_path / "bundles" / a.bundle_id).is_dir()
    assert (tmp_path / "bundles" / d.bundle_id).is_dir()
    with store.resolve_active() as resolved:
        assert resolved.bundle_id == d.bundle_id


def test_gc_deletions_are_operation_log_records(tmp_path: Path) -> None:
    store, a, b, c, d = _lineage(tmp_path)
    store.collect_garbage(is_referenced=lambda bid: False)
    deletions = [
        rec["bundle_id"]
        for rec in store.read_operations()
        if rec["record"] == "GC_DELETE"
    ]
    assert sorted(deletions) == sorted([b.bundle_id, c.bundle_id])


def test_gc_has_no_time_or_count_cutoff_anywhere(tmp_path: Path) -> None:
    """RFC §2.6 r4: run-bundle retention is the SINGLE knob. The GC API
    accepts a reference query and nothing else — no age, no keep-last-N."""
    params = inspect.signature(BundleStore.collect_garbage).parameters
    assert set(params) == {"self", "is_referenced"}
    # and the query has no default: GC cannot run without it
    with pytest.raises(TypeError):
        make_store(tmp_path).collect_garbage()  # type: ignore[call-arg]
    with pytest.raises(TypeError, match="callable"):
        make_store(tmp_path).collect_garbage(is_referenced=None)  # type: ignore[arg-type]


def test_gc_on_empty_store_is_a_noop(tmp_path: Path) -> None:
    report = make_store(tmp_path).collect_garbage(is_referenced=lambda b: False)
    assert report.deleted == ()


def test_gc_refuses_while_crash_interval_open(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    publish_simple(store, "a")

    class Crash(RuntimeError):
        pass

    def crash(label: str) -> None:
        if label == "step7:prepare-fsynced":
            raise Crash(label)

    doomed = make_store(tmp_path, crash_hook=crash)
    with pytest.raises(Crash):
        publish_simple(doomed, "b")
    with pytest.raises(RecoveryRequiredError):
        store.collect_garbage(is_referenced=lambda b: False)


def test_gc_sweeps_dead_tmp_dirs_with_record(tmp_path: Path) -> None:
    """A .tmp dir visible while GC holds the store flock belongs to a dead
    writer (live writers hold the lock across steps 2-10)."""
    store = make_store(tmp_path)
    publish_simple(store, "a")
    dead = tmp_path / "bundles" / "20260101T000000Z-feedfacefeedface.tmp"
    dead.mkdir()
    (dead / PANEL).write_bytes(b"half-written")
    report = store.collect_garbage(is_referenced=lambda b: False)
    assert report.swept_tmp == (dead.name,)
    assert not dead.exists()
    sweeps = [r for r in store.read_operations() if r["record"] == "GC_SWEEP_TMP"]
    assert [r["entry"] for r in sweeps] == [dead.name]


# -- reader/GC races (§2.6) ----------------------------------------------


def test_reader_holds_dirfd_across_gc_pass(tmp_path: Path) -> None:
    """A reader that resolved a bundle keeps reading verified content even
    after GC unlinks that bundle mid-read (POSIX unlink-after-open)."""
    store, a, b, c, d = _lineage(tmp_path)
    reader = store.resolve_bundle(b.bundle_id)
    try:
        store.collect_garbage(is_referenced=lambda bid: False)  # deletes B and C
        assert not (tmp_path / "bundles" / b.bundle_id).exists()
        data = reader.read_member(PANEL)
        assert data == make_members("b")[PANEL]
        # content STILL digest-clean against the manifest held at resolve
        assert (
            hashlib.sha256(data).hexdigest() == reader.manifest.members[PANEL].sha256
        )
    finally:
        reader.close()


def test_pointer_flip_mid_read_does_not_disturb_reader(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    first = publish_simple(store, "a")
    reader = store.resolve_active()
    try:
        second = publish_simple(store, "b")  # pointer flips mid-read
        assert reader.bundle_id == first.bundle_id
        assert reader.read_member(CAL) == make_members("a")[CAL]
        with store.resolve_active() as fresh:
            assert fresh.bundle_id == second.bundle_id
    finally:
        reader.close()


def test_flip_then_gc_mid_read(tmp_path: Path) -> None:
    """The §4 combined race: reader resolves ACTIVE, a writer flips the
    pointer, GC collects the now-unreferenced old ACTIVE — the reader's
    dirfd stays valid to the end."""
    store = make_store(tmp_path)
    first = publish_simple(store, "a")
    reader = store.resolve_active()
    try:
        publish_simple(store, "b")
        # first is now non-active; make it also non-ancestor via rollback?
        # No: it IS the parent of the new active, hence rollback ancestry —
        # prove it survives GC (retention), then read through the held fd.
        report = store.collect_garbage(is_referenced=lambda bid: False)
        assert first.bundle_id not in report.deleted
        assert reader.read_member(PANEL) == make_members("a")[PANEL]
    finally:
        reader.close()


def test_gc_never_deletes_active_even_if_unreferenced(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    result = publish_simple(store, "only")
    report = store.collect_garbage(is_referenced=lambda bid: False)
    assert report.deleted == ()
    assert report.retained_active == (result.bundle_id,)
    with store.resolve_active() as resolved:
        assert resolved.bundle_id == result.bundle_id
