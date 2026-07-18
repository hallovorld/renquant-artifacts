"""Bundle schema v1 for the 104 serving-pair transactional store.

Normative spec: RFC "transactional artifact bundles for the 104 serving
pair" (RenQuant#492, doc/design/2026-07-17-artifact-bundle-transactionality.md),
sections 2.2 (bundle identity) and 2.4 (writer authorization).

Schema v1 facts this module pins:

* the manifest field set is CLOSED — unknown fields are rejected;
* the member set is EXACT — ``panel-ltr.alpha158_fund.json`` and
  ``panel-rank-calibration.json``, nothing more, nothing less;
* the digest algorithm is sha256 (pinned per schema version; the field
  names ``sha256``/``manifest_digest`` carry the pin, values are bare hex);
* canonical serialization is UTF-8, sorted keys, no insignificant
  whitespace (compact separators), LF line ending (a single trailing LF);
* ``manifest_digest`` = sha256 over the canonical serialization of the
  manifest WITHOUT the ``manifest_digest`` field;
* ``bundle_id = <utc-ts>Z-<first 16 hex of manifest_digest>`` is DERIVED
  (directory name), not stored inside the manifest — storing it would be
  circular with ``manifest_digest``. The timestamp component is the
  compaction of ``created_at``, so the id is fully reproducible from the
  manifest alone.

Interpretations recorded for review (see the phase-1 progress doc):

* the closed-field rule is applied recursively to ``members`` entries,
  ``authorization``, and ``authorization.actor`` (fail-closed reading of
  "unknown fields REJECTED");
* ``bindings`` is required and must be a non-empty JSON object; its
  CONTENT is validated by the renquant-pipeline ``bundle_contract`` pair
  validator (RFC §2.5), which is a pluggable seam in phase 1;
* ``parent_bundle`` is a required KEY; ``null`` is legal only for a
  genesis bundle (first bundle in a store);
* the restamp writer class (mandatory incident/task reference, RFC §2.4)
  is recognized by tool name: any tool whose name contains ``restamp`` or
  the break-glass tool itself.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

#: Manifest schema version pinned by this module.
BUNDLE_SCHEMA_VERSION = 1

#: The EXACT member set for schema v1 (RFC §2.2). Any missing or extra
#: file in the bundle directory makes the bundle invalid.
BUNDLE_MEMBER_NAMES: tuple[str, ...] = (
    "panel-ltr.alpha158_fund.json",
    "panel-rank-calibration.json",
)

MANIFEST_FILENAME = "manifest.json"

#: The ONLY sanctioned manual-mutation tool name (RFC §2.4).
BREAKGLASS_TOOL = "bundle_breakglass"

#: Tool-name markers placing a writer in the restamp class (RFC §2.4):
#: these commits MUST carry ``authorization.source["incident_ref"]``
#: (mandatory incident/task reference, per the containment protocol).
RESTAMP_CLASS_TOOL_MARKERS: tuple[str, ...] = ("restamp", BREAKGLASS_TOOL)

_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "members",
        "bindings",
        "authorization",
        "parent_bundle",
        "created_at",
        "manifest_digest",
    }
)
_MEMBER_FIELDS = frozenset({"sha256", "bytes"})
_AUTHORIZATION_FIELDS = frozenset({"tool", "tool_version", "actor", "source", "inputs"})
_ACTOR_FIELDS = frozenset({"os_user", "operator"})

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_CREATED_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
BUNDLE_ID_RE = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{16}$")


class BundleSchemaError(ValueError):
    """A bundle manifest violates schema v1."""


def canonical_manifest_bytes(payload: Mapping[str, Any]) -> bytes:
    """Canonical serialization (RFC §2.2): UTF-8, sorted keys, no
    insignificant whitespace, LF (a single trailing LF)."""
    try:
        text = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise BundleSchemaError(f"manifest is not canonically serializable: {exc}") from exc
    return text.encode("utf-8") + b"\n"


def compute_manifest_digest(payload: Mapping[str, Any]) -> str:
    """sha256 hex over the canonical serialization of the manifest
    WITHOUT its ``manifest_digest`` field."""
    stripped = {k: v for k, v in payload.items() if k != "manifest_digest"}
    return hashlib.sha256(canonical_manifest_bytes(stripped)).hexdigest()


def compact_created_at(created_at: str) -> str:
    """``2026-07-18T03:15:00Z`` -> ``20260718T031500Z``."""
    return created_at.replace("-", "").replace(":", "")


def derive_bundle_id(created_at: str, manifest_digest: str) -> str:
    """``bundle_id = <utc-ts>Z-<first 16 hex of manifest_digest>``."""
    return f"{compact_created_at(created_at)}-{manifest_digest[:16]}"


def format_created_at(moment: datetime) -> str:
    """Render a datetime as the canonical ``created_at`` string (UTC,
    second precision)."""
    if moment.tzinfo is None:
        raise BundleSchemaError("created_at datetime must be timezone-aware")
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_restamp_class_tool(tool: str) -> bool:
    return any(marker in tool for marker in RESTAMP_CLASS_TOOL_MARKERS)


@dataclass(frozen=True)
class MemberDigest:
    """Per-member content identity: ``{sha256, bytes}`` (RFC §2.2)."""

    sha256: str
    bytes: int


@dataclass(frozen=True)
class BundleManifest:
    """Validated schema-v1 bundle manifest.

    ``bundle_id`` is a derived property, not a stored field — see the
    module docstring.
    """

    schema_version: int
    members: dict[str, MemberDigest]
    bindings: dict[str, Any]
    authorization: dict[str, Any]
    parent_bundle: str | None
    created_at: str
    manifest_digest: str

    @property
    def bundle_id(self) -> str:
        return derive_bundle_id(self.created_at, self.manifest_digest)

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "members": {
                name: {"sha256": digest.sha256, "bytes": digest.bytes}
                for name, digest in self.members.items()
            },
            "bindings": self.bindings,
            "authorization": self.authorization,
            "parent_bundle": self.parent_bundle,
            "created_at": self.created_at,
            "manifest_digest": self.manifest_digest,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_manifest_bytes(self.to_payload())

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "BundleManifest":
        validate_bundle_manifest_payload(payload)
        members = {
            name: MemberDigest(sha256=entry["sha256"], bytes=entry["bytes"])
            for name, entry in payload["members"].items()
        }
        return cls(
            schema_version=payload["schema_version"],
            members=members,
            bindings=dict(payload["bindings"]),
            authorization=dict(payload["authorization"]),
            parent_bundle=payload["parent_bundle"],
            created_at=payload["created_at"],
            manifest_digest=payload["manifest_digest"],
        )


def build_bundle_manifest(
    *,
    member_digests: Mapping[str, MemberDigest],
    bindings: Mapping[str, Any],
    authorization: Mapping[str, Any],
    parent_bundle: str | None,
    created_at: str,
) -> BundleManifest:
    """Assemble, digest-stamp, and validate a schema-v1 manifest."""
    payload: dict[str, Any] = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "members": {
            name: {"sha256": digest.sha256, "bytes": digest.bytes}
            for name, digest in member_digests.items()
        },
        "bindings": dict(bindings),
        "authorization": dict(authorization),
        "parent_bundle": parent_bundle,
        "created_at": created_at,
    }
    payload["manifest_digest"] = compute_manifest_digest(payload)
    return BundleManifest.from_payload(payload)


def validate_bundle_authorization(auth: Any) -> None:
    """Validate the ``authorization`` block (RFC §2.4)."""
    if not isinstance(auth, dict):
        raise BundleSchemaError("authorization must be an object")
    _require_exact_fields("authorization", set(auth), _AUTHORIZATION_FIELDS)

    tool = auth["tool"]
    if not isinstance(tool, str) or not tool:
        raise BundleSchemaError("authorization.tool must be a non-empty string")
    if tool == "hand-edit":
        raise BundleSchemaError(
            "authorization.tool='hand-edit' is not a sanctioned value; manual "
            f"response must use the {BREAKGLASS_TOOL!r} tool (RFC §2.4)"
        )
    if not isinstance(auth["tool_version"], str) or not auth["tool_version"]:
        raise BundleSchemaError("authorization.tool_version must be a non-empty string")

    actor = auth["actor"]
    if not isinstance(actor, dict):
        raise BundleSchemaError("authorization.actor must be an object")
    _require_exact_fields("authorization.actor", set(actor), _ACTOR_FIELDS)
    for key in sorted(_ACTOR_FIELDS):
        if not isinstance(actor[key], str) or not actor[key]:
            raise BundleSchemaError(f"authorization.actor.{key} must be a non-empty string")

    source = auth["source"]
    if not isinstance(source, dict) or not source:
        raise BundleSchemaError("authorization.source must be a non-empty object")
    if is_restamp_class_tool(tool):
        incident_ref = source.get("incident_ref")
        if not isinstance(incident_ref, str) or not incident_ref:
            raise BundleSchemaError(
                f"restamp-class tool {tool!r} requires a non-empty "
                "authorization.source.incident_ref (mandatory incident/task "
                "reference per RFC §2.4 / containment protocol)"
            )

    inputs = auth["inputs"]
    if not isinstance(inputs, dict):
        raise BundleSchemaError("authorization.inputs must be an object of content digests")
    for key, value in inputs.items():
        if not isinstance(key, str) or not isinstance(value, str) or not value:
            raise BundleSchemaError(
                "authorization.inputs entries must map string names to "
                "non-empty digest strings"
            )


def validate_bundle_manifest_payload(payload: Any) -> None:
    """Full schema-v1 validation of a manifest payload (RFC §2.2).

    Raises :class:`BundleSchemaError` on the first violation. Verifies the
    stamped ``manifest_digest`` against a recomputation over the canonical
    serialization.
    """
    if not isinstance(payload, dict):
        raise BundleSchemaError("manifest must be a JSON object")
    _require_exact_fields("manifest", set(payload), _TOP_LEVEL_FIELDS)

    schema_version = payload["schema_version"]
    if isinstance(schema_version, bool) or schema_version != BUNDLE_SCHEMA_VERSION:
        raise BundleSchemaError(
            f"schema_version must be {BUNDLE_SCHEMA_VERSION}, got {schema_version!r}"
        )

    members = payload["members"]
    if not isinstance(members, dict):
        raise BundleSchemaError("members must be an object")
    expected = set(BUNDLE_MEMBER_NAMES)
    if set(members) != expected:
        missing = sorted(expected - set(members))
        extra = sorted(set(members) - expected)
        raise BundleSchemaError(
            "members must be exactly the schema-v1 set "
            f"{sorted(expected)}; missing={missing} extra={extra}"
        )
    for name, entry in members.items():
        if not isinstance(entry, dict):
            raise BundleSchemaError(f"members[{name!r}] must be an object")
        _require_exact_fields(f"members[{name!r}]", set(entry), _MEMBER_FIELDS)
        sha = entry["sha256"]
        if not isinstance(sha, str) or not _HEX64_RE.match(sha):
            raise BundleSchemaError(
                f"members[{name!r}].sha256 must be 64 lowercase hex chars"
            )
        size = entry["bytes"]
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise BundleSchemaError(f"members[{name!r}].bytes must be a positive integer")

    bindings = payload["bindings"]
    if not isinstance(bindings, dict) or not bindings:
        raise BundleSchemaError("bindings must be a non-empty object")

    validate_bundle_authorization(payload["authorization"])

    parent = payload["parent_bundle"]
    if parent is not None and (
        not isinstance(parent, str) or not BUNDLE_ID_RE.match(parent)
    ):
        raise BundleSchemaError(
            "parent_bundle must be null (genesis only) or a valid bundle_id"
        )

    created_at = payload["created_at"]
    if not isinstance(created_at, str) or not _CREATED_AT_RE.match(created_at):
        raise BundleSchemaError(
            "created_at must be a UTC timestamp of the form YYYY-MM-DDTHH:MM:SSZ"
        )
    try:
        datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise BundleSchemaError(f"created_at is not a valid timestamp: {exc}") from exc

    digest = payload["manifest_digest"]
    if not isinstance(digest, str) or not _HEX64_RE.match(digest):
        raise BundleSchemaError("manifest_digest must be 64 lowercase hex chars")
    recomputed = compute_manifest_digest(payload)
    if digest != recomputed:
        raise BundleSchemaError(
            "manifest_digest mismatch: stamped "
            f"{digest[:16]}… != recomputed {recomputed[:16]}…"
        )


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _require_exact_fields(context: str, actual: set, expected: frozenset) -> None:
    unknown = sorted(actual - expected)
    missing = sorted(expected - actual)
    if unknown:
        raise BundleSchemaError(f"{context} has unknown field(s) {unknown}; schema v1 rejects them")
    if missing:
        raise BundleSchemaError(f"{context} is missing required field(s) {missing}")
