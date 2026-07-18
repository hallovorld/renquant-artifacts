"""Idempotent stand-up of the bundle-store DIRECTORY skeleton (AC4 P0).

Census (RenQuant ``doc/design/2026-07-18-ac4-migration-census.md`` §6 P0:
"build, no live change"): the store directory stands up ALONGSIDE the flat
serving pair with ZERO serving change. This tool creates AT MOST:

* the store root itself (only if its parent already exists — a missing
  parent is a typo, not a store location);
* ``<root>/bundles/``;
* ``<root>/bundles/.lock``.

It NEVER creates the ``ACTIVE`` pointer or ``OPERATIONS.jsonl`` — both
belong to the first §2.3 publication (the P1 seal of the current pair) —
and it never opens, reads, or writes ANY other path, so the flat pair
sitting alongside is untouched by construction. Re-running is a no-op
(``created`` comes back empty). The RFC §2.1 host-model guard applies:
init refuses non-local mounts, exactly like the store itself.

CLI::

    python -m renquant_artifacts.bundle_store_init [--store-root ROOT]

Without ``--store-root`` the root comes from the declared real location
(:mod:`renquant_artifacts.bundle_store_location`); the resolution source
is echoed in the JSON report.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .bundle_store import (
    BUNDLES_DIRNAME,
    LOCK_FILENAME,
    BundleStoreError,
    NonLocalStoreError,
    default_local_mount_guard,
)
from .bundle_store_location import StoreLocationError, resolve_store_root


@dataclass(frozen=True)
class StoreInitReport:
    """What one :func:`init_store` run found and did."""

    root: str
    created: tuple[str, ...]
    already_present: tuple[str, ...]


def init_store(
    root: str | os.PathLike[str],
    *,
    local_mount_guard: Callable[[Path], tuple[bool, str]] | None = None,
) -> StoreInitReport:
    """Create the store skeleton under ``root``; idempotent, additive-only."""
    root_path = Path(root)
    guard = local_mount_guard or default_local_mount_guard
    ok, reason = guard(root_path)
    if not ok:
        raise NonLocalStoreError(
            f"refusing to initialize bundle store at {root_path}: {reason} "
            "(RFC §2.1 host model: single-host local POSIX filesystem only)"
        )
    if not root_path.parent.is_dir():
        raise BundleStoreError(
            f"refusing to initialize bundle store at {root_path}: parent "
            f"directory {root_path.parent} does not exist — the store stands "
            "up alongside an EXISTING serving directory (AC4 P0); a missing "
            "parent means the location is wrong"
        )

    created: list[str] = []
    already: list[str] = []

    def _ensure_dir(path: Path, label: str) -> None:
        if path.is_dir():
            already.append(label)
            return
        if path.exists():
            raise BundleStoreError(
                f"cannot initialize bundle store: {path} exists but is not a "
                "directory"
            )
        os.mkdir(path)
        created.append(label)

    _ensure_dir(root_path, str(root_path))
    bundles = root_path / BUNDLES_DIRNAME
    _ensure_dir(bundles, str(bundles))
    lock = bundles / LOCK_FILENAME
    if lock.exists():
        already.append(str(lock))
    else:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        os.close(fd)
        created.append(str(lock))

    return StoreInitReport(
        root=str(root_path),
        created=tuple(created),
        already_present=tuple(already),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bundle_store_init",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--store-root",
        help="store root; default = the declared real location "
        "(bundle_store_location resolution, echoed in the report)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        resolved = resolve_store_root(args.store_root)
        report = init_store(resolved.path)
    except (BundleStoreError, StoreLocationError, OSError) as exc:
        print(f"bundle_store_init: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "store_root": report.root,
                "store_root_source": resolved.source,
                "created": list(report.created),
                "already_present": list(report.already_present),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["StoreInitReport", "init_store", "main"]
