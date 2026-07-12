from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from renquant_artifacts import (
    ArtifactManifestContext,
    ArtifactManifestValidationPipeline,
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


def test_crypto_manifest_validates() -> None:
    manifest = json.loads(
        (Path(__file__).parents[1] / "registry" / "crypto-xgb-diagnostic.json").read_text()
    )
    ctx = ArtifactManifestContext(manifest)
    result = ArtifactManifestValidationPipeline().run(ctx)
    assert result.ok is True


def test_crypto_promotion_rejects_non_crypto() -> None:
    with pytest.raises(ValueError, match="not a crypto artifact"):
        validate_crypto_promotion_contract({"asset_class": "equity"})


def test_crypto_promotion_diagnostic_passes_without_battery() -> None:
    report = validate_crypto_promotion_contract(
        _crypto_manifest(), target_status="diagnostic"
    )
    assert report["ok"] is True


def test_crypto_promotion_shadow_requires_battery() -> None:
    with pytest.raises(ValueError, match="paper battery"):
        validate_crypto_promotion_contract(
            _crypto_manifest(), target_status="shadow"
        )


def test_crypto_promotion_shadow_passes_with_battery() -> None:
    report = validate_crypto_promotion_contract(
        _crypto_manifest(metrics={"paper_battery_pass": True}),
        target_status="shadow",
    )
    assert report["ok"] is True


def test_crypto_promotion_prod_requires_accepted_and_shadow_days() -> None:
    with pytest.raises(ValueError, match="accepted=true"):
        validate_crypto_promotion_contract(
            _crypto_manifest(metrics={"paper_battery_pass": True}),
            target_status="prod",
        )


def test_crypto_promotion_prod_passes_full() -> None:
    report = validate_crypto_promotion_contract(
        _crypto_manifest(
            metrics={"paper_battery_pass": True, "accepted": True, "shadow_days": 7}
        ),
        target_status="prod",
    )
    assert report["ok"] is True
    assert report["target_status"] == "prod"


def test_crypto_promotion_rejects_unknown_target_status() -> None:
    with pytest.raises(ValueError, match="unknown crypto promotion target_status"):
        validate_crypto_promotion_contract(
            _crypto_manifest(), target_status="prod-scaled"
        )


def test_crypto_promotion_shadow_rejects_string_false_battery_flag() -> None:
    # A hand-edited manifest could carry the string "false" instead of a JSON
    # boolean; a truthy check would treat it as passing. Must fail closed.
    with pytest.raises(ValueError, match="paper battery"):
        validate_crypto_promotion_contract(
            _crypto_manifest(metrics={"paper_battery_pass": "false"}),
            target_status="shadow",
        )


def test_crypto_promotion_prod_rejects_non_numeric_shadow_days() -> None:
    with pytest.raises(ValueError, match="shadow_days"):
        validate_crypto_promotion_contract(
            _crypto_manifest(
                metrics={
                    "paper_battery_pass": True,
                    "accepted": True,
                    "shadow_days": "7",
                }
            ),
            target_status="prod",
        )


def test_crypto_promotion_prod_rejects_zero_shadow_days() -> None:
    with pytest.raises(ValueError, match="shadow_days"):
        validate_crypto_promotion_contract(
            _crypto_manifest(
                metrics={
                    "paper_battery_pass": True,
                    "accepted": True,
                    "shadow_days": 0,
                }
            ),
            target_status="prod",
        )


def test_crypto_promotion_prod_rejects_bool_shadow_days() -> None:
    with pytest.raises(ValueError, match="shadow_days"):
        validate_crypto_promotion_contract(
            _crypto_manifest(
                metrics={
                    "paper_battery_pass": True,
                    "accepted": True,
                    "shadow_days": True,
                }
            ),
            target_status="prod",
        )
