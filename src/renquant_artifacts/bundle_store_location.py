"""Declared REAL store location for the 104 serving-pair bundle store.

AC4 migration phase P0 (census: RenQuant
``doc/design/2026-07-18-ac4-migration-census.md`` §6; RFC RenQuant#492
``doc/design/2026-07-17-artifact-bundle-transactionality.md`` §2.1): the
bundle store stands up ALONGSIDE the flat serving pair, under the
umbrella's ``backtesting/renquant_104/artifacts/prod``. That location is
DECLARED exactly once — in the umbrella repo's reviewed
``deploy/bundle_store_location.json`` — and resolved here; tools never
hardcode it at call sites.

Resolution precedence (first hit wins). Every resolution carries a
``source`` string so each tool run can ECHO where its store root came
from:

1. an explicit path (a CLI ``--store-root`` argument) —
   ``source="cli:--store-root"``;
2. the ``RQ_BUNDLE_STORE_ROOT`` environment variable —
   ``source="env:RQ_BUNDLE_STORE_ROOT"``;
3. the umbrella declaration file
   ``$RQ_ROOT/deploy/bundle_store_location.json`` (``RQ_ROOT`` defaulting
   to the live umbrella checkout), whose ``store_root`` is joined onto
   ``RQ_ROOT`` when relative — ``source="declaration:<file>"``.

Fail-closed: a missing or malformed declaration raises
:class:`StoreLocationError` naming the file and both overrides. There is
deliberately NO silent built-in fallback path — the umbrella declaration
is the single source of truth for the real location, and a tool that
cannot resolve it must say so rather than invent a store.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

#: Environment variable naming the umbrella checkout (existing fleet
#: convention — see e.g. renquant-orchestrator ``ops/liveness_common.py``).
RQ_ROOT_ENV = "RQ_ROOT"

#: The live umbrella checkout, used only when :data:`RQ_ROOT_ENV` is unset
#: (same default the ops fleet pins).
DEFAULT_RQ_ROOT = "/Users/renhao/git/github/RenQuant"

#: Environment override naming the store root directly (precedence 2).
STORE_ROOT_ENV = "RQ_BUNDLE_STORE_ROOT"

#: The umbrella's declared-location file, relative to ``RQ_ROOT``.
DECLARATION_RELPATH = "deploy/bundle_store_location.json"

#: Schema version of the declaration file this resolver understands.
DECLARATION_SCHEMA_VERSION = 1


class StoreLocationError(RuntimeError):
    """The declared store location cannot be resolved (fail-closed)."""


@dataclass(frozen=True)
class ResolvedStoreRoot:
    """A resolved store root plus the provenance of the resolution."""

    path: Path
    source: str


def _rq_root(environ: Mapping[str, str]) -> Path:
    return Path(environ.get(RQ_ROOT_ENV) or DEFAULT_RQ_ROOT)


def declaration_path(environ: Mapping[str, str] | None = None) -> Path:
    """Absolute path of the umbrella declaration file for this environment."""
    env = os.environ if environ is None else environ
    return _rq_root(env) / DECLARATION_RELPATH


def _load_declaration(path: Path) -> dict[str, object]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise StoreLocationError(
            f"bundle-store declaration not found: {path} — the umbrella "
            f"declares the real store location there (AC4 P0). Set "
            f"{RQ_ROOT_ENV} to the umbrella checkout, or override with "
            f"{STORE_ROOT_ENV} / an explicit --store-root."
        )
    except OSError as exc:
        raise StoreLocationError(f"bundle-store declaration unreadable: {path}: {exc}")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StoreLocationError(f"bundle-store declaration is not valid JSON: {path}: {exc}")
    if not isinstance(payload, dict):
        raise StoreLocationError(
            f"bundle-store declaration must be a JSON object: {path}"
        )
    return payload


def _parse_declaration(payload: Mapping[str, object], path: Path) -> str:
    known = {"schema_version", "store_root"}
    unknown = [k for k in payload if k not in known and not str(k).startswith("_")]
    if unknown:
        raise StoreLocationError(
            f"bundle-store declaration {path} has unknown field(s) "
            f"{sorted(unknown)}; schema v{DECLARATION_SCHEMA_VERSION} allows "
            f"{sorted(known)} (underscore-prefixed comment keys ignored)"
        )
    if payload.get("schema_version") != DECLARATION_SCHEMA_VERSION:
        raise StoreLocationError(
            f"bundle-store declaration {path} has schema_version="
            f"{payload.get('schema_version')!r}; this resolver understands "
            f"{DECLARATION_SCHEMA_VERSION}"
        )
    store_root = payload.get("store_root")
    if not isinstance(store_root, str) or not store_root.strip():
        raise StoreLocationError(
            f"bundle-store declaration {path} is missing a non-empty "
            f"'store_root' string"
        )
    return store_root.strip()


def resolve_store_root(
    explicit: str | os.PathLike[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> ResolvedStoreRoot:
    """Resolve the store root per the module-docstring precedence."""
    env = os.environ if environ is None else environ
    if explicit is not None:
        return ResolvedStoreRoot(path=Path(explicit), source="cli:--store-root")
    from_env = env.get(STORE_ROOT_ENV)
    if from_env:
        return ResolvedStoreRoot(path=Path(from_env), source=f"env:{STORE_ROOT_ENV}")
    decl = declaration_path(env)
    store_root = _parse_declaration(_load_declaration(decl), decl)
    root = Path(store_root)
    if not root.is_absolute():
        root = _rq_root(env) / root
    return ResolvedStoreRoot(path=root, source=f"declaration:{decl}")


__all__ = [
    "DECLARATION_RELPATH",
    "DECLARATION_SCHEMA_VERSION",
    "DEFAULT_RQ_ROOT",
    "RQ_ROOT_ENV",
    "STORE_ROOT_ENV",
    "ResolvedStoreRoot",
    "StoreLocationError",
    "declaration_path",
    "resolve_store_root",
]
