from __future__ import annotations

import json
from pathlib import Path

import pytest

from renquant_artifacts import load_artifact_manifest, resolve_artifact_manifest


def _write_manifest(path: Path, **overrides) -> dict:
    payload = {
        "artifact_id": "panel-ltr-prod",
        "model_family": "gbdt-panel-ltr",
        "strategy": "renquant_104",
        "fingerprint": "sha256:artifact",
        "uri": "object://renquant-artifacts/panel-ltr-prod.json",
        "promotion_status": "prod",
        "metrics": {"accepted": True, "oos_mean_ic": 0.03},
        "retention_class": "prod",
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return payload


def test_resolve_artifact_manifest_selects_prod_by_strategy_and_family(tmp_path: Path) -> None:
    expected = _write_manifest(tmp_path / "prod.json")
    _write_manifest(
        tmp_path / "shadow.json",
        artifact_id="patchtst-shadow",
        model_family="patchtst",
        promotion_status="shadow",
        metrics={"accepted": False, "oos_mean_ic": 0.01},
    )

    resolved = resolve_artifact_manifest(
        tmp_path,
        strategy="renquant_104",
        model_family="gbdt-panel-ltr",
        promotion_status="prod",
    )

    assert resolved == expected


def test_resolve_artifact_manifest_fails_closed_on_ambiguity(tmp_path: Path) -> None:
    _write_manifest(tmp_path / "a.json")
    _write_manifest(tmp_path / "b.json", artifact_id="panel-ltr-prod-b", fingerprint="sha256:b")

    with pytest.raises(ValueError, match="ambiguous artifact manifest selection"):
        resolve_artifact_manifest(
            tmp_path,
            strategy="renquant_104",
            model_family="gbdt-panel-ltr",
            promotion_status="prod",
        )


def test_load_artifact_manifest_validates_file(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    _write_manifest(path)

    assert load_artifact_manifest(path)["artifact_id"] == "panel-ltr-prod"
