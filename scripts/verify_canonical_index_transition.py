#!/usr/bin/env python3
"""Required-CI guard: the canonical-publication ``INDEX.json`` is append-only
across commits.

Verifies two things and exits non-zero on any violation:

* the CANDIDATE store (this PR's checkout) is internally content-addressed +
  producer-allow-listed (``verify_canonical_index_integrity``); and
* the candidate only ADDS to the BASE store (the PR merge-base / default-branch
  checkout) -- no historical entry deleted or mutated in place, and every
  referenced record file preserved byte-identically
  (``verify_index_transition``).

This is the machine enforcement behind "any commit is verifiable": a PR that
deletes a valid historical entry, or rewrites non-content-addressed metadata
(``artifact_uri`` / ``registered_at``) of an existing entry, fails here rather
than relying on a human reading the diff. Run in CI against a base worktree,
e.g.::

    python scripts/verify_canonical_index_transition.py \\
        --base ../artifacts-base/registry/canonical_publications \\
        --candidate registry/canonical_publications

An absent store on either side is treated as an empty store (nothing published
yet is not a violation), so the guard is armed and inert until the first
canonical publication lands.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from renquant_artifacts.canonical_registry import (
    verify_canonical_index_integrity,
    verify_index_transition,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base", required=True,
        help="path to the BASE canonical_publications dir (merge-base checkout)",
    )
    parser.add_argument(
        "--candidate", required=True,
        help="path to the CANDIDATE canonical_publications dir (this checkout)",
    )
    args = parser.parse_args(argv)

    candidate = Path(args.candidate)
    base = Path(args.base)

    errors: list[str] = []
    errors += verify_canonical_index_integrity(candidate)
    errors += verify_index_transition(base, candidate)

    if errors:
        print("Canonical INDEX append-only guard FAILED:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1
    print("Canonical INDEX append-only guard: OK (candidate is a clean superset of base).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
