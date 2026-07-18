"""``bundle_breakglass`` — the ONLY sanctioned manual mutation tool
(RFC RenQuant#492 §2.4).

It performs the same §2.3 protocol as any writer, with:

* a MANDATORY incident/task reference (``--incident-ref``) recorded in
  ``authorization.source.incident_ref``;
* ``authorization.tool = "bundle_breakglass"``;
* an ALWAYS-fired alarm on commit (a break-glass commit is by definition
  a containment event under the AC3 protocol; the drift-sentinel binding
  is a later phase — the CLI surfaces the alarm on stderr, library
  callers inject ``alarm_hook``);
* ``--rollback-to <bundle_id>`` restricted to ancestors reachable via
  ``parent_bundle`` (enforced by the store).

Usage::

    python -m renquant_artifacts.bundle_breakglass \
        --store-root <root> --incident-ref TASK-62 --operator renhao \
        --member panel-ltr.alpha158_fund.json=/path/to/panel.json \
        --member panel-rank-calibration.json=/path/to/cal.json \
        --bindings-json /path/to/bindings.json

    python -m renquant_artifacts.bundle_breakglass \
        --store-root <root> --incident-ref TASK-62 --operator renhao \
        --rollback-to 20260718T031500Z-0123456789abcdef
"""
from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path
from typing import Any, Mapping

from .bundle_schema import BREAKGLASS_TOOL, BUNDLE_MEMBER_NAMES, sha256_hex
from .bundle_store import BundleStore, BundleStoreError

BREAKGLASS_TOOL_VERSION = "1.0.0"


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
    parser.add_argument("--store-root", required=True, help="bundle store root (…/prod)")
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

    def _alarm(kind: str, payload: dict[str, Any]) -> None:
        print(
            f"ALARM[{kind}] {json.dumps(payload, sort_keys=True)}", file=sys.stderr
        )

    try:
        store = BundleStore(args.store_root, alarm_hook=_alarm)
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
    except (BundleStoreError, OSError, json.JSONDecodeError) as exc:
        print(f"bundle_breakglass: {exc}", file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "bundle_id": result.bundle_id,
                "generation": result.generation,
                "manifest_digest": result.manifest.manifest_digest,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
