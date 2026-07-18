"""Break-glass tool tests (RFC RenQuant#492 §2.4): mandatory incident
reference, same-protocol commits with authorization.tool=bundle_breakglass,
always-alarmed, and --rollback-to restricted to parent_bundle ancestors."""
from __future__ import annotations

import json
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
from renquant_artifacts.bundle_breakglass import main
from renquant_artifacts.bundle_store import (
    BundleStoreError,
    RollbackTargetError,
)


def _write_member_files(tmp_path: Path, tag: str) -> dict[str, Path]:
    out = {}
    members = make_members(tag)
    for name, data in members.items():
        path = tmp_path / f"src-{name}"
        path.write_bytes(data)
        out[name] = path
    return out


def _bindings_file(tmp_path: Path) -> Path:
    path = tmp_path / "bindings.json"
    path.write_text(json.dumps({"scorer_fingerprint": "f" * 16}))
    return path


def test_breakglass_commit_leaves_record_and_alarms(tmp_path: Path, capsys) -> None:
    store_root = tmp_path / "prod"
    setup = make_store(store_root)
    publish_simple(setup, "genesis")

    files = _write_member_files(tmp_path, "manual-fix")
    rc = main(
        [
            "--store-root",
            str(store_root),
            "--incident-ref",
            "TASK-62",
            "--operator",
            "renhao",
            "--member",
            f"{PANEL}={files[PANEL]}",
            "--member",
            f"{CAL}={files[CAL]}",
            "--bindings-json",
            str(_bindings_file(tmp_path)),
        ]
    )
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    result = json.loads(captured.out)
    assert result["generation"] == 2

    # §2.4: the commit is fully recorded — manifest AND operation log
    fresh = make_store(store_root)
    with fresh.resolve_active() as resolved:
        auth = resolved.manifest.authorization
        assert auth["tool"] == "bundle_breakglass"
        assert auth["source"]["incident_ref"] == "TASK-62"
        assert auth["actor"]["operator"] == "renhao"
        assert resolved.read_member(PANEL) == make_members("manual-fix")[PANEL]
    prepare = [r for r in fresh.read_operations() if r["record"] == "PREPARE"][-1]
    assert prepare["authorization"]["tool"] == "bundle_breakglass"
    assert prepare["authorization"]["source"]["incident_ref"] == "TASK-62"
    # ALWAYS alarms (drift-sentinel surface; CLI = stderr)
    assert "ALARM[breakglass_commit]" in captured.err
    assert "TASK-62" in captured.err


def test_incident_ref_is_mandatory(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--store-root", str(tmp_path), "--operator", "renhao"])
    assert excinfo.value.code == 2


def test_exactly_one_mode_required(tmp_path: Path, capsys) -> None:
    rc = main(
        [
            "--store-root",
            str(tmp_path),
            "--incident-ref",
            "INC-1",
            "--operator",
            "renhao",
        ]
    )
    assert rc == 2
    assert "exactly one of" in capsys.readouterr().err


def test_commit_requires_full_member_set_and_bindings(tmp_path: Path, capsys) -> None:
    files = _write_member_files(tmp_path, "x")
    base = [
        "--store-root",
        str(tmp_path / "prod"),
        "--incident-ref",
        "INC-1",
        "--operator",
        "renhao",
    ]
    rc = main(base + ["--member", f"{PANEL}={files[PANEL]}"])
    assert rc == 2
    assert "exactly" in capsys.readouterr().err
    rc = main(
        base
        + ["--member", f"{PANEL}={files[PANEL]}", "--member", f"{CAL}={files[CAL]}"]
    )
    assert rc == 2
    assert "--bindings-json" in capsys.readouterr().err


def test_rollback_to_ancestor_flips_with_new_generation(tmp_path: Path, capsys) -> None:
    store_root = tmp_path / "prod"
    store = make_store(store_root)
    genesis = publish_simple(store, "genesis")
    publish_simple(store, "second")

    rc = main(
        [
            "--store-root",
            str(store_root),
            "--incident-ref",
            "INC-9",
            "--operator",
            "renhao",
            "--rollback-to",
            genesis.bundle_id,
        ]
    )
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    out = json.loads(captured.out)
    assert out["bundle_id"] == genesis.bundle_id
    assert out["generation"] == 3  # rollback NEVER regresses the generation
    assert "ALARM[breakglass_commit]" in captured.err
    with make_store(store_root).resolve_active() as resolved:
        assert resolved.bundle_id == genesis.bundle_id
        assert resolved.generation == 3
        assert resolved.crash_interval is False


def test_rollback_to_non_ancestor_refused(tmp_path: Path, capsys) -> None:
    store_root = tmp_path / "prod"
    store = make_store(store_root)
    genesis = publish_simple(store, "genesis")
    second = publish_simple(store, "second")
    # roll back to genesis legitimately…
    store.rollback_to(genesis.bundle_id, authorization=make_breakglass_authorization())
    # …then `second` is NOT an ancestor of the (genesis) ACTIVE bundle
    rc = main(
        [
            "--store-root",
            str(store_root),
            "--incident-ref",
            "INC-9",
            "--operator",
            "renhao",
            "--rollback-to",
            second.bundle_id,
        ]
    )
    assert rc == 1
    assert "ancestor" in capsys.readouterr().err


def test_library_rollback_requires_breakglass_tool(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    genesis = publish_simple(store, "a")
    publish_simple(store, "b")
    from bundle_helpers import make_authorization

    with pytest.raises(BundleStoreError, match="break-glass"):
        store.rollback_to(genesis.bundle_id, authorization=make_authorization())


def test_library_rollback_to_self_is_not_an_ancestor(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    result = publish_simple(store, "only")
    with pytest.raises(RollbackTargetError):
        store.rollback_to(
            result.bundle_id, authorization=make_breakglass_authorization()
        )


# -- AC4 P0: operational against the DECLARED real store location ---------


def test_cli_resolves_store_root_from_env(tmp_path: Path, monkeypatch, capsys) -> None:
    """No --store-root: the tool runs against the resolved declared
    location (here via RQ_BUNDLE_STORE_ROOT) and echoes the provenance."""
    store_root = tmp_path / "prod"
    setup = make_store(store_root)
    publish_simple(setup, "genesis")
    monkeypatch.setenv("RQ_BUNDLE_STORE_ROOT", str(store_root))

    files = _write_member_files(tmp_path, "env-fix")
    rc = main(
        [
            "--incident-ref",
            "TASK-77",
            "--operator",
            "renhao",
            "--member",
            f"{PANEL}={files[PANEL]}",
            "--member",
            f"{CAL}={files[CAL]}",
            "--bindings-json",
            str(_bindings_file(tmp_path)),
        ]
    )
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    result = json.loads(captured.out)
    assert result["store_root"] == str(store_root)
    assert result["store_root_source"] == "env:RQ_BUNDLE_STORE_ROOT"
    with make_store(store_root).resolve_active() as resolved:
        assert resolved.manifest.authorization["source"]["incident_ref"] == "TASK-77"


def test_cli_resolves_store_root_from_umbrella_declaration(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    rq_root = tmp_path / "RenQuant"
    decl = rq_root / "deploy" / "bundle_store_location.json"
    decl.parent.mkdir(parents=True)
    decl.write_text(
        json.dumps({"schema_version": 1, "store_root": "artifacts/prod"})
    )
    store_root = rq_root / "artifacts" / "prod"
    setup = make_store(store_root)
    publish_simple(setup, "genesis")
    monkeypatch.setenv("RQ_ROOT", str(rq_root))
    monkeypatch.delenv("RQ_BUNDLE_STORE_ROOT", raising=False)

    files = _write_member_files(tmp_path, "decl-fix")
    rc = main(
        [
            "--incident-ref",
            "TASK-88",
            "--operator",
            "renhao",
            "--member",
            f"{PANEL}={files[PANEL]}",
            "--member",
            f"{CAL}={files[CAL]}",
            "--bindings-json",
            str(_bindings_file(tmp_path)),
        ]
    )
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    result = json.loads(captured.out)
    assert result["store_root"] == str(store_root)
    assert result["store_root_source"] == f"declaration:{decl}"


def test_cli_fails_closed_when_no_location_resolvable(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("RQ_ROOT", str(tmp_path / "empty"))
    monkeypatch.delenv("RQ_BUNDLE_STORE_ROOT", raising=False)
    rc = main(
        [
            "--incident-ref",
            "INC-1",
            "--operator",
            "renhao",
            "--rollback-to",
            "20260718T031500Z-0123456789abcdef",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 1
    assert "declaration" in captured.err
