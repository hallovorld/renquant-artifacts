from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from renquant_artifacts import (
    CanonicalPublicationSnapshot,
    build_canonical_provenance_reference,
    load_artifact_manifest,
    resolve_artifact_manifest,
    write_canonical_run_intent,
)
from renquant_artifacts.canonical_registry import (
    CANONICAL_RUN_INTENT_FILENAME,
    CANONICAL_CODE_PIN_SUBREPOS,
    # White-box: the raw live-store writer is module-private (production
    # writes go through promote_candidate_publication). This fixture helper
    # seeds a store directly by design.
    _register_canonical_publication as register_canonical_publication,
    canonical_publication_binding,
    resolve_canonical_publication,
)


def _pubs_dir(root: Path) -> Path:
    return root / "registry_repo" / "registry" / "canonical_publications"


def _publication_snapshot(root: Path) -> CanonicalPublicationSnapshot:
    repo_root = root / "registry_repo"
    if not (repo_root / ".git").exists():
        subprocess.run(["git", "init", "-q"], cwd=repo_root, check=True)
        subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo_root, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=repo_root, check=True)
        subprocess.run(["git", "remote", "add", "origin", "https://github.com/hallovorld/renquant-artifacts"], cwd=repo_root, check=True)
        subprocess.run(["git", "add", "registry"], cwd=repo_root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "publish"], cwd=repo_root, check=True)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo_root, text=True).strip()
    return CanonicalPublicationSnapshot(repo_root, commit, "https://github.com/hallovorld/renquant-artifacts")


def _canonical_provenance_for(root: Path, fingerprint: str) -> dict:
    """Build (and memoize per-fingerprint within one tmp root) a REAL
    canonical publication for ``fingerprint``: a real ``run_intent.json``
    written via ``write_canonical_run_intent`` and registered in a real
    canonical publication store via ``register_canonical_publication``.

    Codex round-4 review on renquant-artifacts#24 explicitly ordered the
    previous fixture shape here removed: prod fixtures used to declare
    ``kind="canonical"`` with a run_intent_path that resolved nowhere and a
    fabricated self-consistent digest, which demonstrated the exact unsafe
    acceptance path the boundary must reject. Prod fixtures now only
    validate because a genuine publication record resolves from the store.
    """
    run_dir = root / "canonical_runs" / fingerprint.replace(":", "_")
    run_intent_path = run_dir / CANONICAL_RUN_INTENT_FILENAME
    if not run_intent_path.exists():
        write_canonical_run_intent(
            run_dir,
            run_id=f"run-{fingerprint}",
            run_type="daily_full",
            producer={
                "repo": "renquant-orchestrator",
                "entrypoint": "daily.TrainGbdtArtifactTask",
            },
            strategy_manifest_fingerprint="sha256:strategy",
            data_manifest_fingerprint="sha256:data",
            strategy_config_digest="sha256:strategyconfig",
            model_config_digest="sha256:modelconfig",
            calendar_universe_digest="sha256:universe",
            as_of="2026-07-18",
            code_pins={
                name: {"commit": "0" * 40, "remote": f"https://github.com/hallovorld/{name}"}
                for name in CANONICAL_CODE_PIN_SUBREPOS.values()
            },
        )
        register_canonical_publication(
            _pubs_dir(root),
            run_intent_path=run_intent_path,
            artifact_digest=fingerprint,
            artifact_uri=f"object://renquant-artifacts/{fingerprint.replace(':', '_')}.json",
        )
    snapshot = _publication_snapshot(root)
    entry, _record, errors = resolve_canonical_publication(fingerprint, _pubs_dir(root))
    assert errors == []
    provenance = build_canonical_provenance_reference(run_intent_path, fingerprint)
    provenance.update(
        registry_snapshot_commit=snapshot.commit,
        publication_record_digest=canonical_publication_binding(entry),
    )
    return provenance


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
        # F-7 (renquant-artifacts#24, Codex 2026-07-14 follow-up):
        # provenance is a REQUIRED, typed manifest field -- see
        # renquant_artifacts.experiment_registry.verify_artifact_provenance.
        # F-7 round-4 follow-up: prod manifests must resolve a REAL
        # publication record from the canonical publication store, so this
        # fixture registers one (see _canonical_provenance_for) and tests
        # thread a clean, exact registry snapshot into resolve/load calls.
    }
    payload.update(overrides)
    if "provenance" not in payload:
        payload["provenance"] = _canonical_provenance_for(
            path.parent, payload["fingerprint"],
        )
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
        canonical_publication_snapshot=_publication_snapshot(tmp_path),
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

    assert (
        load_artifact_manifest(
            path, canonical_publication_snapshot=_publication_snapshot(tmp_path)
        )["artifact_id"]
        == "panel-ltr-prod"
    )


def test_load_artifact_manifest_rejects_draft_placeholder(tmp_path: Path) -> None:
    path = tmp_path / "draft.json"
    _write_manifest(
        path,
        artifact_id="crypto-xgb-diagnostic",
        promotion_status="diagnostic",
        draft=True,
        metrics={"accepted": False},
    )

    with pytest.raises(ValueError, match="draft/placeholder"):
        load_artifact_manifest(path)


def test_resolve_artifact_manifest_rejects_draft_placeholder(tmp_path: Path) -> None:
    # A draft/placeholder manifest must not be selectable by the real
    # consumer-facing lookup function, even when it is the only candidate
    # that matches the requested criteria.
    _write_manifest(
        tmp_path / "crypto-diagnostic.json",
        artifact_id="crypto-xgb-diagnostic",
        model_family="xgb-crypto",
        strategy="renquant_crypto",
        promotion_status="diagnostic",
        draft=True,
        metrics={"accepted": False},
    )

    with pytest.raises(ValueError, match="draft/placeholder"):
        resolve_artifact_manifest(
            tmp_path,
            strategy="renquant_crypto",
            model_family="xgb-crypto",
            promotion_status="diagnostic",
        )
