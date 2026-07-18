"""Schema v1 tests: canonical serialization, digest/bundle_id rules, and
closed-field validation (RFC RenQuant#492 §2.2, §2.4)."""
from __future__ import annotations

import json

import pytest

from bundle_helpers import make_authorization, make_bindings
from renquant_artifacts.bundle_schema import (
    BUNDLE_MEMBER_NAMES,
    BUNDLE_SCHEMA_VERSION,
    BundleManifest,
    BundleSchemaError,
    MemberDigest,
    build_bundle_manifest,
    canonical_manifest_bytes,
    compute_manifest_digest,
    derive_bundle_id,
    validate_bundle_manifest_payload,
)

PANEL, CAL = BUNDLE_MEMBER_NAMES


def _member_digests() -> dict[str, MemberDigest]:
    return {
        PANEL: MemberDigest(sha256="a" * 64, bytes=10),
        CAL: MemberDigest(sha256="b" * 64, bytes=20),
    }


def _valid_manifest() -> BundleManifest:
    return build_bundle_manifest(
        member_digests=_member_digests(),
        bindings=make_bindings(),
        authorization=make_authorization(),
        parent_bundle=None,
        created_at="2026-07-18T03:15:00Z",
    )


def _valid_payload() -> dict:
    return _valid_manifest().to_payload()


# -- canonical serialization ---------------------------------------------


def test_canonical_bytes_sorted_compact_lf() -> None:
    blob = canonical_manifest_bytes({"b": 1, "a": {"z": 2, "y": [1, 2]}})
    assert blob == b'{"a":{"y":[1,2],"z":2},"b":1}\n'


def test_canonical_bytes_utf8_not_ascii_escaped() -> None:
    assert canonical_manifest_bytes({"k": "é"}) == '{"k":"é"}\n'.encode("utf-8")


def test_canonical_bytes_rejects_nan() -> None:
    with pytest.raises(BundleSchemaError):
        canonical_manifest_bytes({"k": float("nan")})


def test_manifest_digest_excludes_digest_field_and_is_stable() -> None:
    payload = _valid_payload()
    without = {k: v for k, v in payload.items() if k != "manifest_digest"}
    assert compute_manifest_digest(payload) == compute_manifest_digest(without)
    assert payload["manifest_digest"] == compute_manifest_digest(payload)


# -- bundle identity -----------------------------------------------------


def test_bundle_id_rule() -> None:
    digest = "0123456789abcdef" + "0" * 48
    assert (
        derive_bundle_id("2026-07-18T03:15:00Z", digest)
        == "20260718T031500Z-0123456789abcdef"
    )


def test_bundle_id_derivable_from_manifest_alone() -> None:
    manifest = _valid_manifest()
    assert manifest.bundle_id == derive_bundle_id(
        manifest.created_at, manifest.manifest_digest
    )


def test_roundtrip_from_payload() -> None:
    manifest = _valid_manifest()
    again = BundleManifest.from_payload(json.loads(manifest.canonical_bytes()))
    assert again == manifest
    assert again.bundle_id == manifest.bundle_id


# -- closed field set ----------------------------------------------------


def test_unknown_top_level_field_rejected() -> None:
    payload = _valid_payload()
    payload["surprise"] = 1
    payload["manifest_digest"] = compute_manifest_digest(payload)
    with pytest.raises(BundleSchemaError, match="unknown field"):
        validate_bundle_manifest_payload(payload)


@pytest.mark.parametrize(
    "field",
    [
        "schema_version",
        "members",
        "bindings",
        "authorization",
        "parent_bundle",
        "created_at",
        "manifest_digest",
    ],
)
def test_missing_required_field_rejected(field: str) -> None:
    payload = _valid_payload()
    del payload[field]
    with pytest.raises(BundleSchemaError, match="missing required"):
        validate_bundle_manifest_payload(payload)


def test_unknown_member_entry_field_rejected() -> None:
    payload = _valid_payload()
    payload["members"][PANEL]["mtime"] = 12345
    payload["manifest_digest"] = compute_manifest_digest(payload)
    with pytest.raises(BundleSchemaError, match="unknown field"):
        validate_bundle_manifest_payload(payload)


def test_unknown_authorization_field_rejected() -> None:
    payload = _valid_payload()
    payload["authorization"]["note"] = "sneaky"
    payload["manifest_digest"] = compute_manifest_digest(payload)
    with pytest.raises(BundleSchemaError, match="unknown field"):
        validate_bundle_manifest_payload(payload)


# -- member set ----------------------------------------------------------


def test_member_set_is_exact_missing() -> None:
    payload = _valid_payload()
    del payload["members"][CAL]
    payload["manifest_digest"] = compute_manifest_digest(payload)
    with pytest.raises(BundleSchemaError, match="exactly the schema-v1 set"):
        validate_bundle_manifest_payload(payload)


def test_member_set_is_exact_extra() -> None:
    payload = _valid_payload()
    payload["members"]["extra.json"] = {"sha256": "c" * 64, "bytes": 5}
    payload["manifest_digest"] = compute_manifest_digest(payload)
    with pytest.raises(BundleSchemaError, match="exactly the schema-v1 set"):
        validate_bundle_manifest_payload(payload)


@pytest.mark.parametrize(
    "sha,size",
    [
        ("A" * 64, 10),  # uppercase hex
        ("a" * 63, 10),  # short
        ("a" * 64, 0),  # empty member
        ("a" * 64, -1),
        ("a" * 64, True),  # bool masquerading as int
        ("a" * 64, "10"),
    ],
)
def test_member_digest_field_validation(sha: object, size: object) -> None:
    payload = _valid_payload()
    payload["members"][PANEL] = {"sha256": sha, "bytes": size}
    payload["manifest_digest"] = compute_manifest_digest(payload)
    with pytest.raises(BundleSchemaError):
        validate_bundle_manifest_payload(payload)


# -- scalar fields -------------------------------------------------------


@pytest.mark.parametrize("version", [2, 0, "1", True, None])
def test_schema_version_pinned(version: object) -> None:
    payload = _valid_payload()
    payload["schema_version"] = version
    payload["manifest_digest"] = compute_manifest_digest(payload)
    with pytest.raises(BundleSchemaError, match="schema_version"):
        validate_bundle_manifest_payload(payload)
    assert BUNDLE_SCHEMA_VERSION == 1


@pytest.mark.parametrize("bindings", [{}, None, "x", 3])
def test_bindings_must_be_nonempty_object(bindings: object) -> None:
    payload = _valid_payload()
    payload["bindings"] = bindings
    payload["manifest_digest"] = compute_manifest_digest(payload)
    with pytest.raises(BundleSchemaError, match="bindings"):
        validate_bundle_manifest_payload(payload)


@pytest.mark.parametrize(
    "parent", ["not-a-bundle-id", "", "20260718T031500Z-XYZ", 5]
)
def test_parent_bundle_format(parent: object) -> None:
    payload = _valid_payload()
    payload["parent_bundle"] = parent
    payload["manifest_digest"] = compute_manifest_digest(payload)
    with pytest.raises(BundleSchemaError, match="parent_bundle"):
        validate_bundle_manifest_payload(payload)


def test_parent_bundle_null_is_genesis_legal() -> None:
    validate_bundle_manifest_payload(_valid_payload())  # parent None


@pytest.mark.parametrize(
    "created", ["2026-07-18 03:15:00", "2026-07-18T03:15:00", "2026-13-40T99:00:00Z", ""]
)
def test_created_at_format(created: str) -> None:
    payload = _valid_payload()
    payload["created_at"] = created
    payload["manifest_digest"] = compute_manifest_digest(payload)
    with pytest.raises(BundleSchemaError, match="created_at"):
        validate_bundle_manifest_payload(payload)


def test_manifest_digest_tamper_detected() -> None:
    payload = _valid_payload()
    payload["bindings"] = {"scorer_fingerprint": "tampered"}
    # digest NOT recomputed -> mismatch must be caught
    with pytest.raises(BundleSchemaError, match="manifest_digest mismatch"):
        validate_bundle_manifest_payload(payload)


# -- authorization (§2.4) ------------------------------------------------


def test_hand_edit_tool_rejected() -> None:
    payload = _valid_payload()
    payload["authorization"]["tool"] = "hand-edit"
    payload["manifest_digest"] = compute_manifest_digest(payload)
    with pytest.raises(BundleSchemaError, match="hand-edit"):
        validate_bundle_manifest_payload(payload)


def test_actor_requires_os_user_and_operator() -> None:
    payload = _valid_payload()
    payload["authorization"]["actor"] = {"os_user": "u"}
    payload["manifest_digest"] = compute_manifest_digest(payload)
    with pytest.raises(BundleSchemaError, match="actor"):
        validate_bundle_manifest_payload(payload)


def test_source_must_be_nonempty() -> None:
    payload = _valid_payload()
    payload["authorization"]["source"] = {}
    payload["manifest_digest"] = compute_manifest_digest(payload)
    with pytest.raises(BundleSchemaError, match="source"):
        validate_bundle_manifest_payload(payload)


@pytest.mark.parametrize("tool", ["restamp", "stamp_restamp_fingerprint", "bundle_breakglass"])
def test_restamp_class_requires_incident_ref(tool: str) -> None:
    payload = _valid_payload()
    payload["authorization"]["tool"] = tool
    payload["authorization"]["source"] = {"reason": "manual"}  # no incident_ref
    payload["manifest_digest"] = compute_manifest_digest(payload)
    with pytest.raises(BundleSchemaError, match="incident_ref"):
        validate_bundle_manifest_payload(payload)


def test_restamp_class_with_incident_ref_accepted() -> None:
    payload = _valid_payload()
    payload["authorization"]["tool"] = "restamp"
    payload["authorization"]["source"] = {"incident_ref": "TASK-62"}
    payload["manifest_digest"] = compute_manifest_digest(payload)
    validate_bundle_manifest_payload(payload)


def test_wf_promote_does_not_need_incident_ref() -> None:
    validate_bundle_manifest_payload(_valid_payload())


def test_inputs_must_map_names_to_digest_strings() -> None:
    payload = _valid_payload()
    payload["authorization"]["inputs"] = {"panel": 123}
    payload["manifest_digest"] = compute_manifest_digest(payload)
    with pytest.raises(BundleSchemaError, match="inputs"):
        validate_bundle_manifest_payload(payload)
