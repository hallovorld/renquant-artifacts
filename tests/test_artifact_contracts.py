from __future__ import annotations

from types import SimpleNamespace

import pytest

from renquant_artifacts import (
    build_run_bundle,
    hash_jsonable,
    sha256_file,
    validate_feature_contract,
    validate_model_evidence_contract,
    validate_panel_artifact_contract,
)


def _valid_panel_payload() -> dict:
    return {
        "feature_cols": ["alpha_1", "alpha_2"],
        "trained_date": "2026-05-25",
        "config_fingerprint": "sha256:cfg",
        "panel_shape": {"rows": 1000, "cols": 2},
        "lookahead_days": 5,
        "train_run_id": "run-1",
        "oos_mean_ic": 0.04,
        "oos_std_ic": 0.02,
        "oos_per_fold_ic": [0.03, 0.05, 0.04],
        "cv_method": "purged-walk-forward",
        "cv_embargo_days": 5,
    }


def test_panel_artifact_contract_requires_strict_oos_evidence() -> None:
    result = validate_panel_artifact_contract(_valid_panel_payload(), strict=True)

    assert result.ok is True
    assert result.details["n_features"] == 2
    assert result.details["oos_mean_ic"] == pytest.approx(0.04)


def test_panel_artifact_contract_rejects_missing_embargo() -> None:
    payload = _valid_panel_payload()
    payload.pop("cv_embargo_days")

    result = validate_panel_artifact_contract(payload, strict=True)

    assert result.ok is False
    assert "missing cv_embargo_days" in result.errors


def test_model_evidence_contract_accepts_sequence_shape() -> None:
    payload = {
        "input_feature_cols": ["alpha_1", "alpha_2"],
        "trained_date": "2026-05-25",
        "config_fingerprint": "sha256:cfg",
        "sequence_shape": {"rows": 800, "timesteps": 64, "features": 2},
        "lookahead_days": 60,
        "train_run_id": "patchtst-run-1",
        "oos_mean_ic": 0.02,
        "oos_std_ic": 0.01,
        "oos_per_fold_ic": [0.01, 0.03],
        "cv_method": "purged-walk-forward",
        "cv_embargo_days": 60,
    }

    result = validate_model_evidence_contract(payload, strict=True)

    assert result.ok is True
    assert result.details["feature_field"] == "input_feature_cols"
    assert result.details["n_features"] == 2


def test_model_evidence_contract_rejects_embargo_shorter_than_horizon() -> None:
    payload = {
        "feature_cols": ["alpha_1"],
        "trained_date": "2026-05-25",
        "config_fingerprint": "sha256:cfg",
        "lookahead_days": 60,
        "train_run_id": "bad-run",
        "oos_mean_ic": 0.02,
        "oos_std_ic": 0.01,
        "oos_per_fold_ic": [0.01, 0.03],
        "cv_method": "purged-walk-forward",
        "cv_embargo_days": 5,
    }

    result = validate_model_evidence_contract(payload, strict=True)

    assert result.ok is False
    assert "cv_embargo_days=5 < lookahead_days=60" in result.errors


def test_panel_artifact_contract_blocks_sentiment_without_runtime_gate() -> None:
    payload = _valid_panel_payload()
    payload["feature_cols"] = ["alpha_1", "mean_sentiment"]
    runtime_config = {
        "ranking": {
            "panel_scoring": {
                "sentiment": {
                    "enabled": True,
                    "regime_policy": {"BULL_CALM": False},
                }
            }
        }
    }

    missing_gate = validate_panel_artifact_contract(
        payload,
        strict=True,
        runtime_config=runtime_config,
    )
    payload["metadata"] = {"sentiment_runtime_gate_contract": "runtime_zeroing"}
    with_gate = validate_panel_artifact_contract(
        payload,
        strict=True,
        runtime_config=runtime_config,
    )

    assert missing_gate.ok is False
    assert any("missing sentiment_runtime_gate_contract" in err for err in missing_gate.errors)
    assert with_gate.ok is True


def test_hash_jsonable_ignores_volatile_runtime_config() -> None:
    left = {"a": 1, "_strategy_dir": "/tmp/one", "nested": {"b": 2}}
    right = {"a": 1, "_strategy_dir": "/tmp/two", "nested": {"b": 2}}

    assert hash_jsonable(left) == hash_jsonable(right)


def test_feature_contract_supports_warn_policy() -> None:
    result = validate_feature_contract(["a", "b"], ["a"], policy="warn")

    assert result.ok is True
    assert result.warnings == ["missing 1 feature column(s)"]
    assert result.details["missing"] == ["b"]


def test_sha256_file_and_run_bundle_record_provenance(tmp_path) -> None:
    panel_path = tmp_path / "panel.json"
    panel_path.write_text(
        """
        {
          "feature_cols": ["alpha_1", "alpha_2"],
          "trained_date": "2026-05-25",
          "config_fingerprint": "sha256:cfg",
          "panel_shape": {"rows": 1000, "cols": 2},
          "lookahead_days": 5,
          "train_run_id": "run-1",
          "oos_mean_ic": 0.04,
          "oos_std_ic": 0.02,
          "oos_per_fold_ic": [0.03, 0.05],
          "cv_method": "purged-walk-forward",
          "cv_embargo_days": 5
        }
        """,
        encoding="utf-8",
    )
    ctx = SimpleNamespace(
        buy_blocked=True,
        skip_buys=False,
        bear_only=False,
        regime="BULL_CALM",
        confidence=0.8,
        ohlcv={},
        regime_state=None,
    )
    config = {
        "watchlist": ["MSFT", "AAPL"],
        "ranking": {"panel_scoring": {"artifact_path": str(panel_path)}},
    }

    bundle = build_run_bundle(
        config,
        tmp_path,
        run_id="daily-1",
        run_type="daily_full",
        ctx=ctx,
        broker_mode="alpaca",
    )

    assert sha256_file(panel_path).startswith("sha256:")
    assert bundle["watchlist_size"] == 2
    assert bundle["artifact_hashes"]["panel"].startswith("sha256:")
    assert bundle["panel_contract"]["ok"] is True
    assert bundle["pipeline_flags"]["buy_blocked"] is True
