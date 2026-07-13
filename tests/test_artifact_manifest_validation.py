from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from renquant_artifacts import (
    ArtifactManifestContext,
    ArtifactManifestValidationPipeline,
    EvidenceBoundPromotionNotImplementedError,
    validate_crypto_promotion_contract,
    validate_triad_sidecar_contract,
)


def test_example_artifact_manifest_validates() -> None:
    manifest = json.loads((Path(__file__).parents[1] / "registry" / "example-artifact.json").read_text())
    ctx = ArtifactManifestContext(manifest)
    result = ArtifactManifestValidationPipeline().run(ctx)

    assert result.ok is True
    assert ctx.validation_report["ok"] is True


def test_prod_artifact_requires_acceptance_metrics() -> None:
    ctx = ArtifactManifestContext({
        "artifact_id": "bad-prod",
        "model_family": "gbdt-panel-ltr",
        "strategy": "renquant_104",
        "fingerprint": "sha256:bad",
        "uri": "object://renquant-artifacts/bad.json",
        "promotion_status": "prod",
        "metrics": {"accepted": False},
    })
    with pytest.raises(ValueError, match="accepted=true"):
        ArtifactManifestValidationPipeline().run(ctx)


def test_local_absolute_artifact_uri_is_rejected() -> None:
    ctx = ArtifactManifestContext({
        "artifact_id": "bad-local",
        "model_family": "gbdt-panel-ltr",
        "strategy": "renquant_104",
        "fingerprint": "sha256:bad",
        "uri": "/Users/renhao/git/github/RenQuant/artifacts/model.pt",
        "promotion_status": "diagnostic",
        "metrics": {"accepted": False},
    })
    with pytest.raises(ValueError, match="developer-local"):
        ArtifactManifestValidationPipeline().run(ctx)


def test_triad_sidecar_preflight_warns_when_not_required() -> None:
    report = validate_triad_sidecar_contract({"artifact_id": "candidate"}, required=False)

    assert report["ok"] is True
    assert report["present"] is False
    assert report["warnings"] == ["leakage triad sidecar is absent"]


def test_triad_sidecar_preflight_fails_closed_when_required() -> None:
    with pytest.raises(ValueError, match="missing leakage triad"):
        validate_triad_sidecar_contract({"artifact_id": "prod"}, required=True)


def test_triad_sidecar_preflight_requires_all_roles() -> None:
    manifest = {
        "triad_report": {
            "candidate": {"role": "candidate"},
            "baseline": {"role": "baseline"},
            "shadow": {"role": "shadow"},
            "leakage_safe": True,
        }
    }

    report = validate_triad_sidecar_contract(manifest, required=True)

    assert report["ok"] is True
    assert report["present"] is True
    assert report["roles"] == {
        "candidate": "candidate",
        "baseline": "baseline",
        "shadow": "shadow",
    }


def test_triad_sidecar_preflight_rejects_unsafe_report() -> None:
    with pytest.raises(ValueError, match="leakage_safe=true"):
        validate_triad_sidecar_contract({
            "triad_report": {
                "candidate": {"role": "candidate"},
                "baseline": {"role": "baseline"},
                "shadow": {"role": "shadow"},
                "leakage_safe": False,
            }
        })


# ── crypto promotion contract ──────────────────────────────────────


def _crypto_manifest(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "artifact_id": "crypto-xgb-test",
        "model_family": "xgb-crypto",
        "strategy": "renquant_crypto",
        "asset_class": "crypto",
        "fingerprint": "sha256:test",
        "uri": "store://crypto/test/model.json",
        "promotion_status": "diagnostic",
        "metrics": {"accepted": False, "paper_battery_pass": False},
    }
    base.update(overrides)
    return base


def test_crypto_diagnostic_manifest_is_marked_draft_and_rejected() -> None:
    # crypto-xgb-diagnostic.json is a literal placeholder: no model has been
    # trained, the fingerprint is a placeholder string, and the store URI is
    # unresolved. It must be structurally non-resolvable, not merely "valid".
    manifest = json.loads(
        (Path(__file__).parents[1] / "registry" / "crypto-xgb-diagnostic.json").read_text()
    )
    assert manifest["draft"] is True
    ctx = ArtifactManifestContext(manifest)
    with pytest.raises(ValueError, match="draft/placeholder"):
        ArtifactManifestValidationPipeline().run(ctx)


def test_crypto_promotion_rejects_non_crypto() -> None:
    with pytest.raises(ValueError, match="not a crypto artifact"):
        validate_crypto_promotion_contract({"asset_class": "equity"})


def test_crypto_promotion_rejects_draft_placeholder_manifest() -> None:
    # A draft/placeholder manifest must never be promotable, regardless of
    # target_status, even when the metrics block is fully populated.
    manifest = _crypto_manifest(
        draft=True,
        metrics={"paper_battery_pass": True, "accepted": True, "shadow_days": 7},
    )
    with pytest.raises(ValueError, match="draft/placeholder"):
        validate_crypto_promotion_contract(
            manifest, current_status="diagnostic", target_status="shadow"
        )


def test_crypto_promotion_requires_explicit_current_status() -> None:
    with pytest.raises(ValueError, match="requires an explicit current_status"):
        validate_crypto_promotion_contract(_crypto_manifest(), target_status="shadow")


def test_crypto_promotion_rejects_unknown_current_status() -> None:
    with pytest.raises(ValueError, match="unknown crypto promotion current_status"):
        validate_crypto_promotion_contract(
            _crypto_manifest(), current_status="prod-scaled", target_status="shadow"
        )


def test_crypto_promotion_rejects_mismatched_current_status() -> None:
    # Manifest actually records promotion_status="diagnostic"; caller asserts
    # "shadow". The caller's belief must agree with the manifest's own record.
    with pytest.raises(ValueError, match="current_status mismatch"):
        validate_crypto_promotion_contract(
            _crypto_manifest(promotion_status="diagnostic"),
            current_status="shadow",
            target_status="prod",
        )


def test_crypto_promotion_rejects_skip_stage_diagnostic_to_prod() -> None:
    # Fully populated metrics must not matter here: diagnostic -> prod skips
    # the mandatory shadow stage and must be rejected before the metrics
    # gate is even inspected.
    manifest = _crypto_manifest(
        promotion_status="diagnostic",
        metrics={"paper_battery_pass": True, "accepted": True, "shadow_days": 7},
    )
    with pytest.raises(ValueError, match="illegal promotion transition"):
        validate_crypto_promotion_contract(
            manifest, current_status="diagnostic", target_status="prod"
        )


def test_crypto_promotion_rejects_same_status_transition() -> None:
    with pytest.raises(ValueError, match="illegal promotion transition"):
        validate_crypto_promotion_contract(
            _crypto_manifest(
                promotion_status="shadow", metrics={"paper_battery_pass": True}
            ),
            current_status="shadow",
            target_status="shadow",
        )


def test_crypto_promotion_rejects_backwards_transition() -> None:
    with pytest.raises(ValueError, match="illegal promotion transition"):
        validate_crypto_promotion_contract(
            _crypto_manifest(
                promotion_status="shadow", metrics={"paper_battery_pass": True}
            ),
            current_status="shadow",
            target_status="diagnostic",
        )


def test_crypto_promotion_rejects_unknown_target_status() -> None:
    with pytest.raises(ValueError, match="unknown crypto promotion target_status"):
        validate_crypto_promotion_contract(
            _crypto_manifest(), current_status="diagnostic", target_status="prod-scaled"
        )


# ── evidence-bound promotion is not implemented (Codex round-2, artifacts#23) ──
#
# validate_crypto_promotion_contract must refuse every diagnostic->shadow or
# shadow->prod request outright -- it must NEVER inspect paper_battery_pass/
# accepted/shadow_days, since those are self-attested, hand-editable manifest
# fields with no real evidence binding. A fully "correct-looking" metrics
# block must be refused exactly the same as an empty one: the refusal must
# not be data-dependent, or a caller could still be misled into thinking a
# populated manifest is authorization-ready.


def test_crypto_promotion_shadow_refuses_even_with_fully_populated_metrics() -> None:
    with pytest.raises(EvidenceBoundPromotionNotImplementedError):
        validate_crypto_promotion_contract(
            _crypto_manifest(
                metrics={"paper_battery_pass": True, "accepted": True, "shadow_days": 30},
            ),
            current_status="diagnostic",
            target_status="shadow",
        )


def test_crypto_promotion_shadow_refuses_with_empty_metrics() -> None:
    with pytest.raises(EvidenceBoundPromotionNotImplementedError):
        validate_crypto_promotion_contract(
            _crypto_manifest(), current_status="diagnostic", target_status="shadow"
        )


def test_crypto_promotion_prod_refuses_even_with_fully_populated_metrics() -> None:
    with pytest.raises(EvidenceBoundPromotionNotImplementedError):
        validate_crypto_promotion_contract(
            _crypto_manifest(
                promotion_status="shadow",
                metrics={"paper_battery_pass": True, "accepted": True, "shadow_days": 30},
            ),
            current_status="shadow",
            target_status="prod",
        )


def test_crypto_promotion_prod_refuses_with_empty_metrics() -> None:
    with pytest.raises(EvidenceBoundPromotionNotImplementedError):
        validate_crypto_promotion_contract(
            _crypto_manifest(promotion_status="shadow", metrics={}),
            current_status="shadow",
            target_status="prod",
        )


def test_crypto_promotion_not_implemented_error_is_not_a_plain_value_error() -> None:
    # Callers must be able to distinguish "evidence-bound promotion isn't
    # built yet" from an ordinary precondition failure (draft/mismatch/
    # illegal-transition), which all remain plain ValueError above.
    assert issubclass(EvidenceBoundPromotionNotImplementedError, NotImplementedError)
    assert not issubclass(EvidenceBoundPromotionNotImplementedError, ValueError)


def test_crypto_promotion_precondition_failures_still_take_priority() -> None:
    # An illegal transition must still be rejected as ValueError BEFORE the
    # not-implemented refusal is ever reached -- preconditions are real
    # checks and must not be shadowed by the blanket refusal.
    with pytest.raises(ValueError, match="illegal promotion transition"):
        validate_crypto_promotion_contract(
            _crypto_manifest(
                promotion_status="diagnostic",
                metrics={"paper_battery_pass": True, "accepted": True, "shadow_days": 7},
            ),
            current_status="diagnostic",
            target_status="prod",
        )
