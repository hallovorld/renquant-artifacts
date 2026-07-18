"""``bundle_breakglass`` — the ONLY sanctioned manual mutation tool
(RFC RenQuant#492 §2.4).

It performs the same §2.3 protocol as any writer, with:

* a MANDATORY incident/task reference (``--incident-ref``) recorded in
  ``authorization.source.incident_ref``;
* ``authorization.tool = "bundle_breakglass"``;
* an ALWAYS-fired alarm on commit, wired to the REAL drift-sentinel alarm
  channel (AC4 P0): ``renquant_common.notify.send`` against
  ``$RQ_ROOT/.env`` — the same canonical ntfy path the orchestrator
  sentinel (``ops/run_surface_drift_check.py`` via
  ``ops/liveness_common.py:alert``) delivers on — PLUS the stderr
  ``ALARM[...]`` record (see :mod:`renquant_artifacts.bundle_alarms`);
* ``--rollback-to <bundle_id>`` restricted to ancestors reachable via
  ``parent_bundle`` (enforced by the store).

``--store-root`` is optional as of AC4 P0: without it the tool runs
against the DECLARED real store location
(:mod:`renquant_artifacts.bundle_store_location` — explicit CLI path >
``RQ_BUNDLE_STORE_ROOT`` env > the umbrella's
``deploy/bundle_store_location.json``), and the resolution source is
echoed in the result JSON.

Usage::

    python -m renquant_artifacts.bundle_breakglass \
        --incident-ref TASK-62 --operator renhao \
        --member panel-ltr.alpha158_fund.json=/path/to/panel.json \
        --member panel-rank-calibration.json=/path/to/cal.json \
        --bindings-json /path/to/bindings.json

    python -m renquant_artifacts.bundle_breakglass \
        --incident-ref TASK-62 --operator renhao \
        --rollback-to 20260718T031500Z-0123456789abcdef
"""
from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path
from typing import Any, Mapping

from .bundle_alarms import create_sentinel_alarm_hook
from .bundle_schema import BREAKGLASS_TOOL, BUNDLE_MEMBER_NAMES, sha256_hex
from .bundle_store import BundleStore, BundleStoreError
from .bundle_store_location import StoreLocationError, resolve_store_root

BREAKGLASS_TOOL_VERSION = "1.1.0"


def build_breakglass_authorization(
    *,
    incident_ref: str,
    operator: str,
    inputs: Mapping[str, str],
    os_user: str | None = None,
) -> dict[str, Any]:
    """Assemble the §2.4 authorization block for a break-glass commit."""
    return {
        "tool": BREAKGLASS_TOOL,
        "tool_version": BREAKGLASS_TOOL_VERSION,
        "actor": {"os_user": os_user or getpass.getuser(), "operator": operator},
        "source": {"incident_ref": incident_ref},
        "inputs": dict(inputs),
    }


def _parse_member(arg: str) -> tuple[str, Path]:
    name, sep, raw_path = arg.partition("=")
    if not sep or not name or not raw_path:
        raise argparse.ArgumentTypeError(
            f"--member expects NAME=PATH, got {arg!r}"
        )
    return name, Path(raw_path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bundle_breakglass",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--store-root",
        help="bundle store root (…/prod); default = the declared real "
        "location (bundle_store_location resolution, echoed in the result)",
    )
    parser.add_argument(
        "--incident-ref",
        required=True,
        help="MANDATORY incident/task reference (containment protocol)",
    )
    parser.add_argument(
        "--operator", required=True, help="configured operator identity"
    )
    parser.add_argument(
        "--rollback-to",
        metavar="BUNDLE_ID",
        help="flip ACTIVE back to a parent_bundle ancestor (no new bundle)",
    )
    parser.add_argument(
        "--member",
        action="append",
        default=[],
        type=_parse_member,
        metavar="NAME=PATH",
        help="member file for a commit (exactly the schema-v1 member set)",
    )
    parser.add_argument(
        "--bindings-json",
        type=Path,
        help="JSON file with the manifest bindings block (commit mode)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if bool(args.rollback_to) == bool(args.member):
        print(
            "bundle_breakglass: exactly one of --rollback-to or --member… "
            "must be given",
            file=sys.stderr,
        )
        return 2

    try:
        resolved = resolve_store_root(args.store_root)
        store = BundleStore(resolved.path, alarm_hook=create_sentinel_alarm_hook())
        if args.rollback_to:
            authorization = build_breakglass_authorization(
                incident_ref=args.incident_ref,
                operator=args.operator,
                inputs={"rollback_target": args.rollback_to},
            )
            result = store.rollback_to(args.rollback_to, authorization=authorization)
        else:
            members = dict(args.member)
            if set(members) != set(BUNDLE_MEMBER_NAMES):
                print(
                    "bundle_breakglass: --member must supply exactly "
                    f"{sorted(BUNDLE_MEMBER_NAMES)}",
                    file=sys.stderr,
                )
                return 2
            if args.bindings_json is None:
                print(
                    "bundle_breakglass: --bindings-json is required for a commit",
                    file=sys.stderr,
                )
                return 2
            bindings = json.loads(args.bindings_json.read_text(encoding="utf-8"))
            inputs = {
                f"member:{name}": f"sha256:{sha256_hex(path.read_bytes())}"
                for name, path in members.items()
            }
            authorization = build_breakglass_authorization(
                incident_ref=args.incident_ref,
                operator=args.operator,
                inputs=inputs,
            )
            result = store.publish(
                members, bindings=bindings, authorization=authorization
            )
    except (
        BundleStoreError,
        StoreLocationError,
        OSError,
        json.JSONDecodeError,
    ) as exc:
        print(f"bundle_breakglass: {exc}", file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "bundle_id": result.bundle_id,
                "generation": result.generation,
                "manifest_digest": result.manifest.manifest_digest,
                "store_root": str(resolved.path),
                "store_root_source": resolved.source,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
