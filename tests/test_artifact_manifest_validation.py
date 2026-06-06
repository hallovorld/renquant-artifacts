from __future__ import annotations

import json
from pathlib import Path

import pytest

from renquant_artifacts import (
    ArtifactManifestContext,
    ArtifactManifestValidationPipeline,
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
