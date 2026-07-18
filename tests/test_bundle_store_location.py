"""Declared-store-location resolution tests (AC4 P0).

Precedence: explicit CLI path > ``RQ_BUNDLE_STORE_ROOT`` env > the
umbrella's ``deploy/bundle_store_location.json`` under ``RQ_ROOT``.
Fail-closed on a missing/malformed declaration; every resolution carries
its provenance ``source``. All tests pass an explicit ``environ`` — the
resolver must never be exercised against the real machine environment
from a test.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from renquant_artifacts.bundle_store_location import (
    DECLARATION_RELPATH,
    DEFAULT_RQ_ROOT,
    RQ_ROOT_ENV,
    STORE_ROOT_ENV,
    StoreLocationError,
    declaration_path,
    resolve_store_root,
)


def _write_declaration(rq_root: Path, payload: dict) -> Path:
    path = rq_root / DECLARATION_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _declaration(store_root: str) -> dict:
    return {"schema_version": 1, "store_root": store_root}


def test_explicit_path_wins_over_env_and_declaration(tmp_path: Path) -> None:
    rq_root = tmp_path / "RenQuant"
    _write_declaration(rq_root, _declaration("declared/prod"))
    environ = {RQ_ROOT_ENV: str(rq_root), STORE_ROOT_ENV: str(tmp_path / "envroot")}
    resolved = resolve_store_root(tmp_path / "explicit", environ=environ)
    assert resolved.path == tmp_path / "explicit"
    assert resolved.source == "cli:--store-root"


def test_env_override_wins_over_declaration(tmp_path: Path) -> None:
    rq_root = tmp_path / "RenQuant"
    _write_declaration(rq_root, _declaration("declared/prod"))
    environ = {RQ_ROOT_ENV: str(rq_root), STORE_ROOT_ENV: str(tmp_path / "envroot")}
    resolved = resolve_store_root(environ=environ)
    assert resolved.path == tmp_path / "envroot"
    assert resolved.source == f"env:{STORE_ROOT_ENV}"


def test_declaration_relative_store_root_joins_rq_root(tmp_path: Path) -> None:
    rq_root = tmp_path / "RenQuant"
    decl = _write_declaration(
        rq_root, _declaration("backtesting/renquant_104/artifacts/prod")
    )
    resolved = resolve_store_root(environ={RQ_ROOT_ENV: str(rq_root)})
    assert resolved.path == rq_root / "backtesting/renquant_104/artifacts/prod"
    assert resolved.source == f"declaration:{decl}"


def test_declaration_absolute_store_root_used_verbatim(tmp_path: Path) -> None:
    rq_root = tmp_path / "RenQuant"
    absolute = tmp_path / "elsewhere" / "prod"
    _write_declaration(rq_root, _declaration(str(absolute)))
    resolved = resolve_store_root(environ={RQ_ROOT_ENV: str(rq_root)})
    assert resolved.path == absolute


def test_missing_declaration_fails_closed_naming_overrides(tmp_path: Path) -> None:
    environ = {RQ_ROOT_ENV: str(tmp_path / "empty")}
    with pytest.raises(StoreLocationError) as excinfo:
        resolve_store_root(environ=environ)
    message = str(excinfo.value)
    assert DECLARATION_RELPATH in message
    assert STORE_ROOT_ENV in message
    assert RQ_ROOT_ENV in message


@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": 2, "store_root": "x"},  # wrong schema version
        {"schema_version": 1},  # missing store_root
        {"schema_version": 1, "store_root": ""},  # empty store_root
        {"schema_version": 1, "store_root": 7},  # non-string store_root
        {"schema_version": 1, "store_root": "x", "extra": 1},  # unknown field
    ],
)
def test_malformed_declarations_rejected(tmp_path: Path, payload: dict) -> None:
    rq_root = tmp_path / "RenQuant"
    _write_declaration(rq_root, payload)
    with pytest.raises(StoreLocationError):
        resolve_store_root(environ={RQ_ROOT_ENV: str(rq_root)})


def test_declaration_not_json_rejected(tmp_path: Path) -> None:
    rq_root = tmp_path / "RenQuant"
    path = rq_root / DECLARATION_RELPATH
    path.parent.mkdir(parents=True)
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(StoreLocationError, match="not valid JSON"):
        resolve_store_root(environ={RQ_ROOT_ENV: str(rq_root)})


def test_declaration_non_object_rejected(tmp_path: Path) -> None:
    rq_root = tmp_path / "RenQuant"
    path = rq_root / DECLARATION_RELPATH
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(["store"]), encoding="utf-8")
    with pytest.raises(StoreLocationError, match="JSON object"):
        resolve_store_root(environ={RQ_ROOT_ENV: str(rq_root)})


def test_underscore_comment_keys_ignored(tmp_path: Path) -> None:
    rq_root = tmp_path / "RenQuant"
    _write_declaration(
        rq_root,
        {"_comment": "reviewed location", **_declaration("prod")},
    )
    resolved = resolve_store_root(environ={RQ_ROOT_ENV: str(rq_root)})
    assert resolved.path == rq_root / "prod"


def test_declaration_path_uses_rq_root_env(tmp_path: Path) -> None:
    assert declaration_path({RQ_ROOT_ENV: str(tmp_path)}) == tmp_path / DECLARATION_RELPATH
    # Empty environ pins the documented default umbrella checkout.
    assert declaration_path({}) == Path(DEFAULT_RQ_ROOT) / DECLARATION_RELPATH


def test_default_rq_root_is_the_live_umbrella_checkout() -> None:
    # Pinned: the fleet convention (ops/liveness_common.py RQ_DEFAULT).
    assert DEFAULT_RQ_ROOT == "/Users/renhao/git/github/RenQuant"
    assert DECLARATION_RELPATH == "deploy/bundle_store_location.json"
