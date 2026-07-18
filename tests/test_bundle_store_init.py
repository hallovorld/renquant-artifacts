"""Store-init tests (AC4 P0): idempotent, additive-only stand-up of the
store skeleton ALONGSIDE the flat serving pair, with zero serving change.

The init tool may create at most ``<root>``, ``<root>/bundles/`` and
``<root>/bundles/.lock``; it never creates ``ACTIVE`` or
``OPERATIONS.jsonl`` (those belong to the first publication — the P1
seal) and never touches any sibling file.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from bundle_helpers import make_store, publish_simple
from renquant_artifacts.bundle_store import (
    BundleStoreError,
    NonLocalStoreError,
)
from renquant_artifacts.bundle_store_init import init_store, main
from renquant_artifacts.bundle_store_location import (
    DECLARATION_RELPATH,
    RQ_ROOT_ENV,
    STORE_ROOT_ENV,
)

_GUARD_OK = {"local_mount_guard": lambda p: (True, "test-injected")}


def _tree_digest(root: Path) -> dict[str, str]:
    """{relative_path: sha256} for every file under root."""
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            out[str(path.relative_to(root))] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return out


def test_fresh_init_creates_exactly_the_skeleton(tmp_path: Path) -> None:
    root = tmp_path / "prod"
    report = init_store(root, **_GUARD_OK)
    assert (root / "bundles").is_dir()
    assert (root / "bundles" / ".lock").is_file()
    assert len(report.created) == 3  # root, bundles/, bundles/.lock
    assert report.already_present == ()
    # NEVER creates the pointer or the operation log (P1's job).
    assert not (root / "ACTIVE").exists()
    assert not (root / "bundles" / "OPERATIONS.jsonl").exists()
    # Nothing beyond the skeleton exists.
    assert {p.name for p in root.rglob("*")} == {"bundles", ".lock"}


def test_init_is_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "prod"
    init_store(root, **_GUARD_OK)
    before = _tree_digest(root)
    report = init_store(root, **_GUARD_OK)
    assert report.created == ()
    assert len(report.already_present) == 3
    assert _tree_digest(root) == before


def test_init_alongside_flat_pair_is_zero_serving_change(tmp_path: Path) -> None:
    """The exact P0 shape: the store stands up NEXT TO the flat pair and
    its side files; every pre-existing byte is untouched."""
    root = tmp_path / "prod"
    root.mkdir()
    flat_files = {
        "panel-ltr.alpha158_fund.json": b'{"panel": "live"}\n',
        "panel-rank-calibration.json": b'{"cal": "live"}\n',
        "panel-ltr.alpha158_fund.previous.json": b'{"panel": "prev"}\n',
        "panel-rank-calibration.weekly_rollback_2026-07-12.json": b'{"cal": "rb"}\n',
    }
    for name, data in flat_files.items():
        (root / name).write_bytes(data)
    before = _tree_digest(root)

    report = init_store(root, **_GUARD_OK)

    after = _tree_digest(root)
    assert set(after) - set(before) == {"bundles/.lock"}  # only additions
    for name in before:  # every pre-existing byte identical
        assert after[name] == before[name]
    assert str(root) in report.already_present  # root itself pre-existed


def test_init_refuses_missing_parent(tmp_path: Path) -> None:
    with pytest.raises(BundleStoreError, match="parent"):
        init_store(tmp_path / "missing" / "prod", **_GUARD_OK)


def test_init_refuses_non_directory_collisions(tmp_path: Path) -> None:
    root_as_file = tmp_path / "prod"
    root_as_file.write_text("file")
    with pytest.raises(BundleStoreError, match="not a directory"):
        init_store(root_as_file, **_GUARD_OK)

    root = tmp_path / "prod2"
    root.mkdir()
    (root / "bundles").write_text("file")
    with pytest.raises(BundleStoreError, match="not a directory"):
        init_store(root, **_GUARD_OK)


def test_init_refuses_non_local_mount(tmp_path: Path) -> None:
    with pytest.raises(NonLocalStoreError):
        init_store(tmp_path / "prod", local_mount_guard=lambda p: (False, "nfs"))


def test_init_on_published_store_changes_nothing(tmp_path: Path) -> None:
    root = tmp_path / "prod"
    store = make_store(root)
    publish_simple(store, "genesis")
    before = _tree_digest(root)
    report = init_store(root, **_GUARD_OK)
    assert report.created == ()
    assert _tree_digest(root) == before
    with make_store(root).resolve_active() as resolved:
        assert resolved.generation == 1  # serving state untouched


def test_cli_env_resolution_and_idempotence(tmp_path: Path, monkeypatch, capsys) -> None:
    root = tmp_path / "prod"
    monkeypatch.setenv(STORE_ROOT_ENV, str(root))
    rc = main([])
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    report = json.loads(captured.out)
    assert report["store_root"] == str(root)
    assert report["store_root_source"] == f"env:{STORE_ROOT_ENV}"
    assert len(report["created"]) == 3

    rc = main([])
    report2 = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert report2["created"] == []
    assert len(report2["already_present"]) == 3


def test_cli_declaration_resolution(tmp_path: Path, monkeypatch, capsys) -> None:
    rq_root = tmp_path / "RenQuant"
    decl = rq_root / DECLARATION_RELPATH
    decl.parent.mkdir(parents=True)
    decl.write_text(
        json.dumps({"schema_version": 1, "store_root": "artifacts/prod"})
    )
    (rq_root / "artifacts").mkdir()
    monkeypatch.setenv(RQ_ROOT_ENV, str(rq_root))
    monkeypatch.delenv(STORE_ROOT_ENV, raising=False)
    rc = main([])
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    report = json.loads(captured.out)
    assert report["store_root"] == str(rq_root / "artifacts" / "prod")
    assert report["store_root_source"] == f"declaration:{decl}"
    assert (rq_root / "artifacts" / "prod" / "bundles" / ".lock").is_file()


def test_cli_fails_closed_without_any_location(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv(RQ_ROOT_ENV, str(tmp_path / "empty"))
    monkeypatch.delenv(STORE_ROOT_ENV, raising=False)
    rc = main([])
    captured = capsys.readouterr()
    assert rc == 1
    assert "declaration" in captured.err


def test_cli_explicit_store_root(tmp_path: Path, capsys) -> None:
    root = tmp_path / "prod"
    rc = main(["--store-root", str(root)])
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    report = json.loads(captured.out)
    assert report["store_root_source"] == "cli:--store-root"
    assert os.path.isfile(root / "bundles" / ".lock")
