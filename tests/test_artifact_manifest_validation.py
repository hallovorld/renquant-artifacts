from __future__ import annotations

import json
from pathlib import Path

import pytest

from renquant_artifacts import ArtifactManifestContext, ArtifactManifestValidationPipeline


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
