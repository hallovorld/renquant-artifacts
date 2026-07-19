"""F-7 (RenQuant#471) corrected registry contract.

Covers the three gaps from Codex's round-5 review of umbrella PR #471:

1. ``reject_exploratory_promotion`` is wired into the REAL promotion
   entrypoint (``ValidateArtifactManifestTask`` / ``validate_artifact_manifest``
   / ``load_artifact_manifest`` / ``resolve_artifact_manifest``), not left as
   an unreferenced helper.
2. The 5 required pin categories are verified against the actual
   environment (git checkouts, data manifest, model artifact file,
   resolved universe), not merely required-present.
3. A manifest registry (index digest binding) makes "registered" mean
   something more than "lives under the right directory."
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from renquant_artifacts import (
    build_canonical_provenance_reference,
    build_experiment_provenance_reference,
    load_artifact_manifest,
    register_canonical_publication,
    reject_exploratory_promotion,
    resolve_artifact_manifest,
    resolve_canonical_publication,
    validate_artifact_manifest,
    verify_artifact_provenance,
    verify_calendar_universe_pin,
    verify_canonical_run_intent,
    verify_code_pin,
    verify_data_snapshot_pin,
    verify_experiment_pins,
    verify_manifest_registered,
    verify_model_artifact_pin,
    write_canonical_run_intent,
    write_experiment_classification,
)
from renquant_artifacts.canonical_registry import (
    CANONICAL_PUBLICATIONS_INDEX_FILENAME,
    _record_filename,
)
from renquant_common.model_fingerprint import artifact_sha256


def _init_repo(path: Path, *, remote: str) -> str:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "f.txt").write_text("x")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)
    subprocess.run(["git", "remote", "add", "origin", remote], cwd=path, check=True)
    return subprocess.check_output(
        ["git", "-C", str(path), "log", "-1", "--format=%H"], text=True,
    ).strip()


# ── code-pin verification (real git repos, not mocks) ──────────────────────


class TestVerifyCodePin:
    def test_clean_matching_pin_passes(self, tmp_path):
        remote = "https://github.com/hallovorld/renquant-strategy-104"
        commit = _init_repo(tmp_path / "repo", remote=remote)
        errors = verify_code_pin(tmp_path / "repo", commit, remote)
        assert errors == []

    def test_wrong_head_fails(self, tmp_path):
        remote = "https://github.com/hallovorld/renquant-strategy-104"
        _init_repo(tmp_path / "repo", remote=remote)
        errors = verify_code_pin(tmp_path / "repo", "0" * 40, remote)
        assert any("does not match pinned commit" in e for e in errors)

    def test_dirty_checkout_fails(self, tmp_path):
        remote = "https://github.com/hallovorld/renquant-strategy-104"
        commit = _init_repo(tmp_path / "repo", remote=remote)
        (tmp_path / "repo" / "f.txt").write_text("dirty")
        errors = verify_code_pin(tmp_path / "repo", commit, remote)
        assert any("dirty" in e for e in errors)

    def test_wrong_remote_fails(self, tmp_path):
        commit = _init_repo(
            tmp_path / "repo", remote="https://github.com/WRONG/wrong-repo",
        )
        errors = verify_code_pin(
            tmp_path / "repo", commit,
            "https://github.com/hallovorld/renquant-strategy-104",
        )
        assert any("remote URL mismatch" in e for e in errors)

    def test_missing_commit_or_remote_fails(self, tmp_path):
        assert any("no commit hash" in e for e in verify_code_pin(tmp_path, "", "x"))
        assert any("no remote URL" in e for e in verify_code_pin(tmp_path, "x", ""))


# ── content-fingerprint pin verification ────────────────────────────────────


class TestVerifyDataSnapshotPin:
    def test_matching_fingerprint_passes(self):
        manifest = {"dataset_id": "d1", "fingerprint": "sha256:abc"}
        assert verify_data_snapshot_pin("sha256:abc", manifest) == []

    def test_mismatched_fingerprint_fails(self):
        manifest = {"dataset_id": "d1", "fingerprint": "sha256:abc"}
        errors = verify_data_snapshot_pin("sha256:different", manifest)
        assert any("data_snapshot mismatch" in e for e in errors)

    def test_manifest_without_fingerprint_fails(self):
        errors = verify_data_snapshot_pin("sha256:abc", {"dataset_id": "d1"})
        assert any("no fingerprint" in e for e in errors)


class TestVerifyModelArtifactPin:
    def test_matching_file_hash_passes(self, tmp_path):
        artifact = tmp_path / "model.pt"
        artifact.write_bytes(b"weights")
        assert verify_model_artifact_pin(artifact_sha256(artifact), artifact) == []

    def test_mismatched_file_hash_fails(self, tmp_path):
        artifact = tmp_path / "model.pt"
        artifact.write_bytes(b"weights")
        errors = verify_model_artifact_pin("sha256:" + "0" * 64, artifact)
        assert any("model_artifact mismatch" in e for e in errors)

    def test_missing_file_fails(self, tmp_path):
        errors = verify_model_artifact_pin("sha256:abc", tmp_path / "nope.pt")
        assert any("not found" in e for e in errors)


class TestVerifyCalendarUniversePin:
    def test_matching_universe_passes(self):
        from renquant_artifacts import hash_jsonable
        universe = ["AAPL", "MSFT", "SPY"]
        digest = hash_jsonable(sorted(set(universe)))
        assert verify_calendar_universe_pin(digest, universe) == []

    def test_order_independent(self):
        from renquant_artifacts import hash_jsonable
        digest = hash_jsonable(sorted({"AAPL", "MSFT"}))
        assert verify_calendar_universe_pin(digest, ["MSFT", "AAPL"]) == []

    def test_mismatched_universe_fails(self):
        from renquant_artifacts import hash_jsonable
        digest = hash_jsonable(sorted({"AAPL", "MSFT"}))
        errors = verify_calendar_universe_pin(digest, ["AAPL", "MSFT", "GOOG"])
        assert any("calendar_universe mismatch" in e for e in errors)


# ── verify_experiment_pins: full 5-category dispatch, fail-closed ──────────


class _Fixture:
    """Builds a subrepos.lock.json + two real git checkouts for pin tests."""

    def __init__(self, tmp_path: Path):
        self.tmp_path = tmp_path
        self.strategy_remote = "https://github.com/hallovorld/renquant-strategy-104"
        self.pipeline_remote = "https://github.com/hallovorld/renquant-pipeline"
        self.strategy_commit = _init_repo(
            tmp_path / "renquant-strategy-104", remote=self.strategy_remote,
        )
        self.pipeline_commit = _init_repo(
            tmp_path / "renquant-pipeline", remote=self.pipeline_remote,
        )
        lock = {
            "subrepos": [
                {
                    "name": "renquant-strategy-104",
                    "local_path": str(tmp_path / "renquant-strategy-104"),
                    "commit": self.strategy_commit,
                    "remote": self.strategy_remote,
                },
                {
                    "name": "renquant-pipeline",
                    "local_path": str(tmp_path / "renquant-pipeline"),
                    "commit": self.pipeline_commit,
                    "remote": self.pipeline_remote,
                },
            ]
        }
        (tmp_path / "subrepos.lock.json").write_text(json.dumps(lock))

        self.data_manifest = {"dataset_id": "d1", "fingerprint": "sha256:datapin"}
        self.model_artifact = tmp_path / "model.pt"
        self.model_artifact.write_bytes(b"weights")
        self.universe = ["AAPL", "MSFT"]

        from renquant_artifacts import hash_jsonable
        self.pins = {
            "strategy_config": self.strategy_commit,
            "pipeline_version": self.pipeline_commit,
            "data_snapshot": "sha256:datapin",
            "model_artifact": artifact_sha256(self.model_artifact),
            "calendar_universe": hash_jsonable(sorted(set(self.universe))),
        }

    def verify(self, **overrides):
        pins = overrides.pop("pins", self.pins)
        kwargs = dict(
            repo_root=self.tmp_path,
            data_manifest=self.data_manifest,
            model_artifact_path=self.model_artifact,
            universe=self.universe,
        )
        kwargs.update(overrides)
        return verify_experiment_pins(pins, **kwargs)


class TestVerifyExperimentPins:
    def test_all_five_categories_verify_clean(self, tmp_path):
        fx = _Fixture(tmp_path)
        assert fx.verify() == []

    def test_missing_pin_key_fails(self, tmp_path):
        fx = _Fixture(tmp_path)
        incomplete = dict(fx.pins)
        del incomplete["model_artifact"]
        errors = fx.verify(pins=incomplete)
        assert any("missing required keys" in e for e in errors)

    def test_stale_strategy_commit_vs_lock_fails(self, tmp_path):
        fx = _Fixture(tmp_path)
        stale_pins = dict(fx.pins)
        stale_pins["strategy_config"] = "0" * 40
        errors = fx.verify(pins=stale_pins)
        assert any(
            "does not match subrepos.lock.json pin" in e
            or "does not match pinned commit" in e
            for e in errors
        )

    def test_dirty_pipeline_checkout_fails(self, tmp_path):
        fx = _Fixture(tmp_path)
        (tmp_path / "renquant-pipeline" / "f.txt").write_text("dirty")
        errors = fx.verify()
        assert any("pins.pipeline_version" in e and "dirty" in e for e in errors)

    def test_missing_data_manifest_fails_closed(self, tmp_path):
        fx = _Fixture(tmp_path)
        errors = fx.verify(data_manifest=None)
        assert any("no data_manifest supplied" in e for e in errors)

    def test_missing_model_artifact_path_fails_closed(self, tmp_path):
        fx = _Fixture(tmp_path)
        errors = fx.verify(model_artifact_path=None)
        assert any("no model_artifact_path supplied" in e for e in errors)

    def test_missing_universe_fails_closed(self, tmp_path):
        fx = _Fixture(tmp_path)
        errors = fx.verify(universe=None)
        assert any("no universe supplied" in e for e in errors)

    def test_mismatched_data_snapshot_fails(self, tmp_path):
        fx = _Fixture(tmp_path)
        bad_pins = dict(fx.pins)
        bad_pins["data_snapshot"] = "sha256:wrong"
        errors = fx.verify(pins=bad_pins)
        assert any("pins.data_snapshot" in e and "mismatch" in e for e in errors)

    def test_missing_lock_file_fails_closed(self, tmp_path):
        errors = verify_experiment_pins(
            {
                "strategy_config": "x", "pipeline_version": "x",
                "data_snapshot": "x", "model_artifact": "x",
                "calendar_universe": "x",
            },
            repo_root=tmp_path,
        )
        assert any("subrepos.lock.json not found" in e for e in errors)


# ── manifest registry index (immutable registration record) ────────────────


class TestVerifyManifestRegistered:
    def test_registered_matching_digest_passes(self, tmp_path):
        index = {"exp-001": {"digest": "sha256:abc", "path": "experiments/manifests/exp-001.json"}}
        index_path = tmp_path / "INDEX.json"
        index_path.write_text(json.dumps(index))
        assert verify_manifest_registered("sha256:abc", "exp-001", index_path) == []

    def test_unregistered_experiment_id_fails(self, tmp_path):
        index_path = tmp_path / "INDEX.json"
        index_path.write_text(json.dumps({}))
        errors = verify_manifest_registered("sha256:abc", "exp-999", index_path)
        assert any("is not registered" in e for e in errors)

    def test_digest_mismatch_fails(self, tmp_path):
        index = {"exp-001": {"digest": "sha256:abc"}}
        index_path = tmp_path / "INDEX.json"
        index_path.write_text(json.dumps(index))
        errors = verify_manifest_registered("sha256:tampered", "exp-001", index_path)
        assert any("digest mismatch" in e for e in errors)

    def test_missing_index_fails_closed(self, tmp_path):
        errors = verify_manifest_registered("sha256:abc", "exp-001", tmp_path / "nope.json")
        assert any("not found" in e for e in errors)


# ── EXPLORATORY_ONLY marker + promotion-boundary enforcement ───────────────


class TestClassificationAndRejection:
    def test_write_then_reject(self, tmp_path):
        out = tmp_path / "run_output"
        write_experiment_classification(
            out, experiment_id="exp-1", manifest_path="experiments/manifests/exp-1.json",
            manifest_digest="sha256:m", config_digest="sha256:c",
        )
        with pytest.raises(ValueError, match="EXPLORATORY_ONLY"):
            reject_exploratory_promotion(out)

    def test_missing_marker_now_fails_closed(self, tmp_path):
        """Behavior CHANGED (Codex review 2026-07-14): a missing marker used
        to be silently treated as "not exploratory, proceed" -- that was "a
        second layer of the same bypass" (a caller could point provenance at
        an empty/fake directory and sail through). It now raises: a missing
        marker is unverifiable provenance, not proof of a clean run."""
        with pytest.raises(ValueError, match="no experiment classification"):
            reject_exploratory_promotion(tmp_path / "clean_output")

    def test_write_is_atomic_tmp_rename(self, tmp_path):
        out = tmp_path / "run_output"
        marker = write_experiment_classification(
            out, experiment_id="exp-1", manifest_path="x",
            manifest_digest="sha256:m", config_digest="sha256:c",
        )
        assert marker.exists()
        assert not marker.with_suffix(".json.tmp").exists()


class TestPromotionBoundaryIntegration:
    """Exercises the REAL promotion entrypoints, not a mock of them.

    This is exactly what Codex flagged as missing on RenQuant#471: a marker
    that nothing enforces. Here the SAME ``validate_artifact_manifest`` /
    ``load_artifact_manifest`` / ``resolve_artifact_manifest`` that every
    real caller across the multirepo (renquant_pipeline.inference,
    renquant-artifacts registry resolution) funnels through is called
    directly against an EXPLORATORY_ONLY-tagged candidate.

    Codex's 2026-07-14 follow-up review found the round-1 wiring still
    bypassable two ways: (1) ``provenance_dir`` was optional, so simply
    omitting it skipped the check entirely; (2) even when present, it was a
    bare caller-supplied path with no binding to a real, registered run --
    pointing it at an empty directory (or one with a tampered marker)
    silently passed. ``provenance`` is now a REQUIRED, typed field (see
    ``verify_artifact_provenance``/``PROVENANCE_KINDS``), and
    ``kind="experiment"`` is bound to the immutable, content-addressed
    manifest-registry index this PR already built for the experiment side
    (``verify_manifest_registered``), not a bare path.
    """

    def _register(self, index_path: Path, experiment_id: str, digest: str, path: str = "x") -> None:
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index = json.loads(index_path.read_text()) if index_path.exists() else {}
        index[experiment_id] = {"digest": digest, "path": path}
        index_path.write_text(json.dumps(index))

    def _exploratory_manifest(self, tmp_path: Path, *, registered: bool = True, **overrides) -> dict:
        run_dir = tmp_path / "sim_output" / "exp-forbidden"
        write_experiment_classification(
            run_dir, experiment_id="exp-forbidden",
            manifest_path="experiments/manifests/exp-forbidden.json",
            manifest_digest="sha256:m", config_digest="sha256:c",
        )
        index_path = tmp_path / "experiments" / "manifests" / "INDEX.json"
        if registered:
            self._register(index_path, "exp-forbidden", "sha256:m")
        manifest = {
            "artifact_id": "candidate-from-exploratory-run",
            "model_family": "gbdt-panel-ltr",
            "strategy": "renquant_104",
            "fingerprint": "sha256:candidate",
            "uri": "object://renquant-artifacts/candidate.json",
            "promotion_status": "prod",
            "metrics": {"accepted": True},
            "provenance": build_experiment_provenance_reference(run_dir, index_path),
        }
        manifest.update(overrides)
        return manifest

    def test_validate_artifact_manifest_rejects_exploratory_provenance(self, tmp_path):
        manifest = self._exploratory_manifest(tmp_path)
        with pytest.raises(ValueError, match="registered experiment"):
            validate_artifact_manifest(manifest)

    def test_load_artifact_manifest_rejects_exploratory_provenance(self, tmp_path):
        manifest = self._exploratory_manifest(tmp_path)
        path = tmp_path / "candidate.json"
        path.write_text(json.dumps(manifest))
        with pytest.raises(ValueError, match="registered experiment"):
            load_artifact_manifest(path)

    def test_resolve_artifact_manifest_rejects_exploratory_provenance(self, tmp_path):
        manifest = self._exploratory_manifest(tmp_path)
        registry_dir = tmp_path / "registry"
        registry_dir.mkdir()
        (registry_dir / "candidate.json").write_text(json.dumps(manifest))
        with pytest.raises(ValueError, match="registered experiment"):
            resolve_artifact_manifest(
                registry_dir,
                strategy="renquant_104",
                model_family="gbdt-panel-ltr",
                promotion_status="prod",
            )

    def test_kind_none_ordinary_artifact_is_accepted(self, tmp_path):
        """Regression control (renamed/re-scoped -- see
        test_manifest_without_provenance_dir_now_rejected for why): an
        ordinary, non-experiment artifact must still validate when it makes
        the now-required explicit 'kind=none' declaration. This is the
        narrow allowlist the fix permits; it must not be confused with the
        OLD "no provenance_dir at all" bypass -- the field is present and
        explicit, just declaring "not experiment-derived".

        promotion_status is deliberately NOT "prod" here (F-7 canonical
        follow-up): kind="none" can no longer be combined with
        promotion_status="prod" at all (see
        TestProvenanceKindNoneProdRejection below) -- a real prod artifact
        must carry a verified kind="canonical" run-intent binding instead.
        This test's job is only to prove the narrow "none" allowlist itself
        still works for genuinely non-prod, ordinary artifacts."""
        manifest = {
            "artifact_id": "ordinary-artifact",
            "model_family": "gbdt-panel-ltr",
            "strategy": "renquant_104",
            "fingerprint": "sha256:ordinary",
            "uri": "object://renquant-artifacts/ordinary.json",
            "promotion_status": "diagnostic",
            "metrics": {"accepted": True},
            "provenance": {"kind": "none"},
        }
        report = validate_artifact_manifest(manifest)
        assert report["ok"] is True

    def test_manifest_without_provenance_key_now_rejected(self, tmp_path):
        """Behavior CHANGED (Codex review 2026-07-14): this manifest shape
        (no 'provenance' key at all) used to validate successfully -- that
        WAS the bypass: "An experiment-derived result can therefore be
        promoted simply by omitting provenance_dir." provenance is now
        required, so silent omission is itself a rejection. See
        test_kind_none_ordinary_artifact_is_accepted for how an ordinary
        artifact expresses the same intent explicitly instead.

        Enforcement-window follow-up (#24 sequencing fix): the rejection is
        GOVERNED until the sequenced consumer migrations (renquant-model#55,
        renquant-orchestrator#518) land -- asserted here through the
        per-call strict switch, which is the exact post-migration behavior;
        the in-window warn-not-raise legacy shape is pinned in
        tests/test_provenance_enforcement_window.py."""
        manifest = {
            "artifact_id": "ordinary-artifact",
            "model_family": "gbdt-panel-ltr",
            "strategy": "renquant_104",
            "fingerprint": "sha256:ordinary",
            "uri": "object://renquant-artifacts/ordinary.json",
            "promotion_status": "prod",
            "metrics": {"accepted": True},
        }
        with pytest.raises(ValueError, match="missing a required 'provenance' record"):
            validate_artifact_manifest(manifest, require_provenance=True)


class TestProvenanceBypassClosed:
    """The exact end-to-end negative tests Codex's follow-up review asked
    for: an artifact produced from a marked experiment but with MISSING or
    FALSIFIED provenance must be rejected by the REAL validation entrypoint,
    not accepted -- this is the dishonest/absent path, in addition to (not a
    replacement for) TestPromotionBoundaryIntegration's honest-provenance
    happy/rejection paths above.
    """

    def _candidate(self, **overrides) -> dict:
        manifest = {
            "artifact_id": "candidate-from-exploratory-run",
            "model_family": "gbdt-panel-ltr",
            "strategy": "renquant_104",
            "fingerprint": "sha256:candidate",
            "uri": "object://renquant-artifacts/candidate.json",
            "promotion_status": "prod",
            "metrics": {"accepted": True},
        }
        manifest.update(overrides)
        return manifest

    def test_omitted_provenance_bypass_closed(self, tmp_path):
        """A real EXPLORATORY_ONLY run happened (marker + registration both
        genuinely exist) but the candidate manifest simply omits
        'provenance' entirely -- the exact bypass Codex reported. Must be
        rejected, not silently accepted."""
        run_dir = tmp_path / "sim_output" / "exp-1"
        write_experiment_classification(
            run_dir, experiment_id="exp-1",
            manifest_path="experiments/manifests/exp-1.json",
            manifest_digest="sha256:m", config_digest="sha256:c",
        )
        index_path = tmp_path / "experiments" / "manifests" / "INDEX.json"
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(json.dumps({"exp-1": {"digest": "sha256:m", "path": "x"}}))

        manifest = self._candidate()  # no provenance key at all
        # Post-migration (window-closed) behavior: rejected outright. The
        # enforcement window (#24 sequencing fix) governs WHEN this becomes
        # unconditional -- never WHETHER: even while the window is open the
        # omission is surfaced as a FutureWarning, not silently accepted
        # (see tests/test_provenance_enforcement_window.py).
        with pytest.raises(ValueError, match="missing a required 'provenance' record"):
            validate_artifact_manifest(manifest, require_provenance=True)

    def test_provenance_pointing_at_nonexistent_directory_rejected(self, tmp_path):
        """provenance.kind='experiment' resolves to a directory that does
        not exist at all -- "a lineage reference that doesn't actually
        resolve to anything." Must be rejected."""
        index_path = tmp_path / "experiments" / "manifests" / "INDEX.json"
        manifest = self._candidate(
            provenance={
                "kind": "experiment",
                "dir": str(tmp_path / "sim_output" / "never-ran"),
                "registry_index_path": str(index_path),
            },
        )
        with pytest.raises(ValueError, match="no experiment classification"):
            validate_artifact_manifest(manifest)

    def test_provenance_pointing_at_empty_directory_rejected(self, tmp_path):
        """provenance.kind='experiment' resolves to a real, existing
        directory that simply has no classification record in it (e.g. an
        attacker points at an empty decoy directory instead of the real
        exploratory run's output). Must be rejected, not treated as "not
        exploratory, proceed"."""
        empty_dir = tmp_path / "sim_output" / "decoy"
        empty_dir.mkdir(parents=True)
        index_path = tmp_path / "experiments" / "manifests" / "INDEX.json"
        manifest = self._candidate(
            provenance={
                "kind": "experiment",
                "dir": str(empty_dir),
                "registry_index_path": str(index_path),
            },
        )
        with pytest.raises(ValueError, match="no experiment classification"):
            validate_artifact_manifest(manifest)

    def test_falsified_classification_field_still_rejected(self, tmp_path):
        """The classification file is tampered with to hide the
        EXPLORATORY_ONLY marker (classification field overwritten to look
        benign) while the manifest_digest/experiment_id are left intact and
        genuinely registered. Because registration -- not the mutable
        self-reported field -- is the ground truth for a registered
        experiment (every registered experiment is EXPLORATORY_ONLY by
        construction), this must STILL be rejected."""
        run_dir = tmp_path / "sim_output" / "exp-2"
        marker = write_experiment_classification(
            run_dir, experiment_id="exp-2",
            manifest_path="experiments/manifests/exp-2.json",
            manifest_digest="sha256:m2", config_digest="sha256:c",
        )
        index_path = tmp_path / "experiments" / "manifests" / "INDEX.json"
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(json.dumps({"exp-2": {"digest": "sha256:m2", "path": "x"}}))

        # Tamper: hide the EXPLORATORY_ONLY marker, digest/experiment_id
        # left untouched (still a genuinely registered pair).
        raw = json.loads(marker.read_text())
        raw["classification"] = "PRODUCTION"
        marker.write_text(json.dumps(raw))

        manifest = self._candidate(
            provenance={
                "kind": "experiment",
                "dir": str(run_dir),
                "registry_index_path": str(index_path),
            },
        )
        with pytest.raises(ValueError, match="registered experiment"):
            validate_artifact_manifest(manifest)

    def test_unregistered_digest_with_falsified_classification_rejected(self, tmp_path):
        """The classification file claims a non-exploratory status AND its
        manifest_digest/experiment_id do not correspond to any registered
        entry (a wholly fabricated record). Neither signal (registration,
        self-reported classification) can vouch for this record, so it must
        be rejected as ambiguous/unverifiable -- not accepted by default."""
        run_dir = tmp_path / "sim_output" / "forged"
        write_experiment_classification(
            run_dir, experiment_id="forged-experiment",
            manifest_path="experiments/manifests/forged.json",
            manifest_digest="sha256:notreallyregistered", config_digest="sha256:c",
        )
        marker = run_dir / "_experiment_classification.json"
        raw = json.loads(marker.read_text())
        raw["classification"] = "PRODUCTION"
        marker.write_text(json.dumps(raw))

        index_path = tmp_path / "experiments" / "manifests" / "INDEX.json"
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(json.dumps({}))  # nothing registered

        manifest = self._candidate(
            provenance={
                "kind": "experiment",
                "dir": str(run_dir),
                "registry_index_path": str(index_path),
            },
        )
        with pytest.raises(ValueError, match="ambiguous/unverifiable"):
            validate_artifact_manifest(manifest)

    def test_provenance_kind_none_with_no_local_path_has_nothing_to_check(self, tmp_path):
        """RENAMED/re-scoped (Codex round-3 follow-up on THIS SAME PR
        closed the bypass this test used to (mis-)document as safe --
        see test_provenance_kind_none_over_registered_experiment_output_rejected
        below for the fix). A kind='none' manifest with no
        local_artifact_path/artifact_path/file-uri at all gives
        _verify_none_provenance nothing on disk to inspect, so it still
        validates -- this is the honestly-disclosed RESIDUAL limit (see
        _verify_none_provenance's docstring), not the closed bypass. It
        must be told apart from an artifact that DOES reference a real,
        locally-visible experiment output directory (which is now
        rejected, not accepted).

        promotion_status is overridden to non-prod here (F-7 canonical
        follow-up): kind='none' + promotion_status='prod' is now rejected
        unconditionally regardless of local resolvability -- see
        test_provenance_kind_none_with_no_local_path_at_prod_rejected below,
        which is this SAME fixture at prod."""
        manifest = self._candidate(provenance={"kind": "none"}, promotion_status="diagnostic")
        report = validate_artifact_manifest(manifest)
        assert report["ok"] is True

    def test_provenance_kind_none_with_no_local_path_at_prod_rejected(self, tmp_path):
        """F-7 canonical follow-up (companion to Scenario C below): the SAME
        fixture as test_provenance_kind_none_with_no_local_path_has_nothing_to_check
        immediately above, but at promotion_status='prod'. Before this
        fixed, kind='none' passed unconditionally here BECAUSE there was
        nothing local to check -- exactly the residual gap the F-7 canonical
        design exists to close for real production artifacts: the new
        prod+none rule rejects this regardless of local resolvability, since
        it does not depend on any filesystem walk at all."""
        manifest = self._candidate(provenance={"kind": "none"})  # promotion_status='prod' by default
        with pytest.raises(ValueError, match="cannot be combined with"):
            validate_artifact_manifest(manifest)

    def test_provenance_kind_none_over_registered_experiment_output_rejected(self, tmp_path):
        """THE exact end-to-end negative test Codex's round-3 follow-up
        review demanded, quoted in full in
        verify_artifact_provenance's docstring: 'create a real registered
        experiment output, construct its candidate artifact with
        provenance={"kind": "none"}, and prove validation rejects it. It
        currently accepts.'

        Before this fix: kind='none' returned from
        verify_artifact_provenance() immediately, with NO inspection of
        the manifest at all -- a candidate manifest built from a REAL,
        genuinely registered EXPLORATORY_ONLY run's own output directory
        (referenced here via local_artifact_path, exactly as a real
        producer sets it so the artifact is locatable -- see
        renquant_model_gbdt.pipelines.BuildArtifactManifestTask and
        experiments/gbdt_scratch_from_archived_20260528/promote_candidate.py
        in renquant-model) validated successfully despite the dishonest
        'none' claim. That is the live, reproducible gap this test proves
        closed: the fixture below is BYTE-FOR-BYTE what a dishonest caller
        would have gotten away with previously.
        """
        run_dir = tmp_path / "sim_output" / "exp-none-bypass"
        write_experiment_classification(
            run_dir, experiment_id="exp-none-bypass",
            manifest_path="experiments/manifests/exp-none-bypass.json",
            manifest_digest="sha256:none-bypass", config_digest="sha256:c",
        )
        index_path = tmp_path / "experiments" / "manifests" / "INDEX.json"
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(
            json.dumps({"exp-none-bypass": {"digest": "sha256:none-bypass", "path": "x"}})
        )

        # The dishonest candidate: its own local_artifact_path points
        # straight at the real registered experiment's output directory
        # (a subdirectory of it, to also prove the bounded upward walk),
        # but provenance dishonestly declares kind="none" instead of the
        # honest "experiment" + dir + registry_index_path.
        candidate_file = run_dir / "candidate_run" / "output.json"
        candidate_file.parent.mkdir(parents=True)
        candidate_file.write_text("{}")
        # promotion_status overridden to non-prod (F-7 canonical follow-up):
        # this test is specifically about the marker-scan bypass closure,
        # not the separate, unconditional kind='none'+prod rejection (see
        # test_provenance_kind_none_over_opaque_store_uri_at_prod_rejected
        # for that one) -- prod would raise on the new rule before ever
        # reaching the marker-scan check this test exists to exercise.
        manifest = self._candidate(
            local_artifact_path=str(candidate_file),
            provenance={"kind": "none"},
            promotion_status="diagnostic",
        )

        with pytest.raises(ValueError, match="EXPLORATORY_ONLY classification record"):
            validate_artifact_manifest(manifest)

    def test_provenance_kind_none_via_artifact_path_or_file_uri_also_rejected(self, tmp_path):
        """Same bypass, exercised through the OTHER two identity fields
        _verify_none_provenance inspects (artifact_path, and a file://
        uri) -- not just local_artifact_path -- so all three real
        producer-set identity fields get equal treatment."""
        run_dir = tmp_path / "sim_output" / "exp-none-bypass-2"
        write_experiment_classification(
            run_dir, experiment_id="exp-none-bypass-2",
            manifest_path="experiments/manifests/exp-none-bypass-2.json",
            manifest_digest="sha256:none-bypass-2", config_digest="sha256:c",
        )
        index_path = tmp_path / "experiments" / "manifests" / "INDEX.json"
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(
            json.dumps({"exp-none-bypass-2": {"digest": "sha256:none-bypass-2", "path": "x"}})
        )
        artifact_file = run_dir / "output.json"
        artifact_file.write_text("{}")

        for kwargs in (
            {"artifact_path": str(artifact_file)},
            {"uri": f"file://{artifact_file}"},
        ):
            # promotion_status overridden to non-prod for the same reason as
            # test_provenance_kind_none_over_registered_experiment_output_rejected
            # above -- this test targets the marker-scan bypass closure.
            manifest = self._candidate(
                provenance={"kind": "none"}, promotion_status="diagnostic", **kwargs,
            )
            with pytest.raises(ValueError, match="EXPLORATORY_ONLY classification record"):
                validate_artifact_manifest(manifest)

    def test_provenance_kind_none_over_opaque_store_uri_has_nothing_to_check(self, tmp_path):
        """Honestly-disclosed residual limit (see _verify_none_provenance
        docstring): an artifact whose ONLY identity is an opaque
        store://.../object://... reference -- even one that happens to be
        produced from a registered experiment -- gives this check no local
        filesystem path to inspect at all, so it still passes. This is
        materially narrower than the prior gap (which accepted this for
        EVERY kind='none' manifest, including ones with a real local path)
        and is not silently claimed to be closed.

        Regression control kept at non-prod (F-7 canonical follow-up): see
        test_provenance_kind_none_over_opaque_store_uri_at_prod_rejected
        immediately below for the SAME fixture at promotion_status='prod',
        which is Scenario C -- low-stakes 'none' usage away from prod is
        unaffected by that new rule."""
        run_dir = tmp_path / "sim_output" / "exp-none-bypass-3"
        write_experiment_classification(
            run_dir, experiment_id="exp-none-bypass-3",
            manifest_path="experiments/manifests/exp-none-bypass-3.json",
            manifest_digest="sha256:none-bypass-3", config_digest="sha256:c",
        )
        manifest = self._candidate(
            uri="object://renquant-artifacts/opaque-only.json",
            provenance={"kind": "none"},
            promotion_status="diagnostic",
        )
        report = validate_artifact_manifest(manifest)
        assert report["ok"] is True

    def test_provenance_kind_none_over_opaque_store_uri_at_prod_rejected(self, tmp_path):
        """Scenario C (F-7 canonical follow-up design doc, section 8): the
        EXACT SAME fixture as
        test_provenance_kind_none_over_opaque_store_uri_has_nothing_to_check
        above (a real registered experiment, artifact identity is an opaque
        object://... URI only, no local path) but at
        promotion_status='prod'. Before this fix, kind='none' passed
        unconditionally here -- Codex's core complaint this whole design
        exists to fix ("kind='none' passes unconditionally for artifacts
        whose only identity is an opaque store://object:// URI"). Now
        rejected: a real prod artifact can never use kind='none' at all,
        regardless of whether there is anything local to inspect."""
        run_dir = tmp_path / "sim_output" / "exp-none-bypass-3-prod"
        write_experiment_classification(
            run_dir, experiment_id="exp-none-bypass-3-prod",
            manifest_path="experiments/manifests/exp-none-bypass-3-prod.json",
            manifest_digest="sha256:none-bypass-3-prod", config_digest="sha256:c",
        )
        manifest = self._candidate(
            uri="object://renquant-artifacts/opaque-only-prod.json",
            provenance={"kind": "none"},
            # promotion_status='prod' by default via self._candidate()
        )
        with pytest.raises(ValueError, match="cannot be combined with"):
            validate_artifact_manifest(manifest)

    def test_unknown_provenance_kind_rejected(self, tmp_path):
        manifest = self._candidate(provenance={"kind": "totally-made-up"})
        with pytest.raises(ValueError, match="must be one of"):
            validate_artifact_manifest(manifest)

    def test_experiment_kind_missing_required_subkeys_rejected(self, tmp_path):
        manifest = self._candidate(provenance={"kind": "experiment"})
        with pytest.raises(ValueError, match="missing required keys"):
            validate_artifact_manifest(manifest)


# ── kind="canonical": run-intent record + digest-field binding ─────────────


class _CanonicalFixture:
    """Builds 3 real git checkouts (strategy-104/pipeline/model) + a
    matching subrepos.lock.json + a real run_intent.json written via
    write_canonical_run_intent, with genuinely matching code pins -- the
    SAME real-git-repo technique _Fixture/_init_repo already use for the
    5-category experiment-pin tests above, generalized to the 3 canonical
    code-pin categories (CANONICAL_CODE_PIN_SUBREPOS).
    """

    REPOS = {
        "renquant-strategy-104": "https://github.com/hallovorld/renquant-strategy-104",
        "renquant-pipeline": "https://github.com/hallovorld/renquant-pipeline",
        "renquant-model": "https://github.com/hallovorld/renquant-model",
    }

    def __init__(self, tmp_path: Path):
        self.tmp_path = tmp_path
        self.commits = {
            name: _init_repo(tmp_path / name, remote=remote)
            for name, remote in self.REPOS.items()
        }
        lock = {
            "subrepos": [
                {
                    "name": name,
                    "local_path": str(tmp_path / name),
                    "commit": self.commits[name],
                    "remote": remote,
                }
                for name, remote in self.REPOS.items()
            ]
        }
        (tmp_path / "subrepos.lock.json").write_text(json.dumps(lock))

        code_pins = {
            name: {"commit": self.commits[name], "remote": remote}
            for name, remote in self.REPOS.items()
        }
        self.output_dir = tmp_path / "training_output" / "run-20260716"
        self.run_intent_path = write_canonical_run_intent(
            self.output_dir,
            run_id="run-20260716-001",
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
            as_of="2026-07-16",
            code_pins=code_pins,
        )


class TestVerifyCanonicalRunIntent:
    """Unit-level tests for canonical_registry.verify_canonical_run_intent
    -- the code-pin/producer-allowlist check that backs the kind='canonical'
    provenance branch, mirroring TestVerifyExperimentPins's coverage style
    for the analogous 5-category experiment check."""

    def test_clean_matching_run_intent_passes(self, tmp_path):
        fx = _CanonicalFixture(tmp_path)
        errors = verify_canonical_run_intent(fx.run_intent_path, repo_root=fx.tmp_path)
        assert errors == []

    def test_missing_required_keys_fails_closed(self, tmp_path):
        fx = _CanonicalFixture(tmp_path)
        raw = json.loads(fx.run_intent_path.read_text())
        del raw["as_of"]
        fx.run_intent_path.write_text(json.dumps(raw))

        errors = verify_canonical_run_intent(fx.run_intent_path, repo_root=fx.tmp_path)
        assert any("missing required keys" in e for e in errors)

    def test_unknown_producer_fails_closed(self, tmp_path):
        """Scenario B(ii) (F-7 canonical follow-up design doc, section 8):
        a run_intent.json whose producer names an entrypoint not in
        CANONICAL_PRODUCERS must fail verification."""
        fx = _CanonicalFixture(tmp_path)
        raw = json.loads(fx.run_intent_path.read_text())
        raw["producer"] = {"repo": "renquant-orchestrator", "entrypoint": "some.OtherTask"}
        fx.run_intent_path.write_text(json.dumps(raw))

        errors = verify_canonical_run_intent(fx.run_intent_path, repo_root=fx.tmp_path)
        assert any("CANONICAL_PRODUCERS allowlist" in e for e in errors)

    def test_dirty_model_checkout_fails(self, tmp_path):
        fx = _CanonicalFixture(tmp_path)
        (tmp_path / "renquant-model" / "f.txt").write_text("dirty")

        errors = verify_canonical_run_intent(fx.run_intent_path, repo_root=fx.tmp_path)
        assert any("code_pins.renquant-model" in e and "dirty" in e for e in errors)

    def test_stale_pipeline_commit_vs_lock_fails(self, tmp_path):
        fx = _CanonicalFixture(tmp_path)
        raw = json.loads(fx.run_intent_path.read_text())
        raw["code_pins"]["renquant-pipeline"]["commit"] = "0" * 40
        fx.run_intent_path.write_text(json.dumps(raw))

        errors = verify_canonical_run_intent(fx.run_intent_path, repo_root=fx.tmp_path)
        assert any(
            "does not match subrepos.lock.json pin" in e
            or "does not match pinned commit" in e
            for e in errors
        )

    def test_missing_run_intent_file_fails_closed(self, tmp_path):
        errors = verify_canonical_run_intent(tmp_path / "nope.json", repo_root=tmp_path)
        assert any("not found" in e for e in errors)


class TestWriteCanonicalRunIntent:
    def test_write_is_atomic_tmp_rename_and_schema_correct(self, tmp_path):
        out_dir = tmp_path / "training_output"
        path = write_canonical_run_intent(
            out_dir,
            run_id="r1",
            run_type="daily_full",
            producer={
                "repo": "renquant-orchestrator",
                "entrypoint": "daily.TrainGbdtArtifactTask",
            },
            strategy_manifest_fingerprint="sha256:s",
            data_manifest_fingerprint="sha256:d",
            strategy_config_digest="sha256:sc",
            model_config_digest="sha256:mc",
            calendar_universe_digest="sha256:u",
            as_of="2026-07-16",
            code_pins={},
        )
        assert path.exists()
        assert not path.with_suffix(".json.tmp").exists()
        payload = json.loads(path.read_text())
        assert payload["schema_version"] == 1
        assert payload["kind"] == "canonical-run-intent"
        assert payload["workflow_class"] == "canonical"
        assert payload["created_at"].endswith("Z")


class TestCanonicalProvenance:
    """End-to-end kind='canonical' coverage through the REAL
    verify_artifact_provenance/validate_artifact_manifest entrypoints --
    the digest-field binding this whole design exists to establish (F-7
    canonical follow-up design doc, section 4: "the manifest field
    identifying an artifact is already `fingerprint`... the fix: the
    provenance record itself carries that same digest")."""

    def test_genuine_canonical_provenance_reference_is_accepted(self, tmp_path):
        """Genuine positive path: a real run_intent.json (real code pins via
        temp git repos), published to a real canonical publication store via
        register_canonical_publication (WITH the full repo_root environment
        verification at publish time -- the trusted-publisher gate), then a
        manifest built with build_canonical_provenance_reference is accepted
        end-to-end at promotion_status='prod' because the authoritative
        record resolves from the store (round-4 boundary)."""
        fx = _CanonicalFixture(tmp_path)
        pubs = tmp_path / "registry" / "canonical_publications"
        register_canonical_publication(
            pubs,
            run_intent_path=fx.run_intent_path,
            artifact_digest="sha256:candidate-canonical",
            artifact_uri="object://renquant-artifacts/canonical-gbdt-20260716.bin",
            repo_root=fx.tmp_path,
        )
        manifest = {
            "artifact_id": "canonical-gbdt-20260716",
            "model_family": "gbdt-panel-ltr",
            "strategy": "renquant_104",
            "fingerprint": "sha256:candidate-canonical",
            "uri": "object://renquant-artifacts/canonical-gbdt-20260716.json",
            "promotion_status": "prod",
            "metrics": {"accepted": True},
            "provenance": build_canonical_provenance_reference(
                fx.run_intent_path, "sha256:candidate-canonical",
            ),
        }
        report = validate_artifact_manifest(manifest, canonical_publications_dir=pubs)
        assert report["ok"] is True

    def test_artifact_digest_mismatch_from_a_different_run_rejected(self, tmp_path):
        """Scenario B(i) (F-7 canonical follow-up design doc, section 8):
        artifact_digest copied from a DIFFERENT real canonical run's
        provenance onto THIS manifest -- verify_artifact_provenance must
        raise on the digest mismatch, unconditionally, before any local
        resolution of run_intent_path is even attempted."""
        fx = _CanonicalFixture(tmp_path)
        real_reference = build_canonical_provenance_reference(
            fx.run_intent_path, "sha256:the-real-artifact-this-run-actually-produced",
        )
        manifest = {
            "artifact_id": "a-different-artifact",
            "model_family": "gbdt-panel-ltr",
            "strategy": "renquant_104",
            # Dishonest: this manifest's OWN fingerprint does not match
            # real_reference["artifact_digest"], even though run_intent_path
            # / run_intent_digest are genuinely real and unmodified.
            "fingerprint": "sha256:a-completely-different-artifact",
            "uri": "object://renquant-artifacts/a-different-artifact.json",
            "promotion_status": "prod",
            "metrics": {"accepted": True},
            "provenance": real_reference,
        }
        with pytest.raises(ValueError, match="does not match this manifest's own fingerprint"):
            validate_artifact_manifest(manifest)

    def test_end_to_end_rejects_unknown_producer_via_tampered_run_intent(self, tmp_path):
        """Scenario B(ii), exercised end-to-end through
        validate_artifact_manifest rather than calling
        verify_canonical_run_intent directly: a run_intent.json tampered to
        name a producer outside CANONICAL_PRODUCERS must fail the
        SUPPLEMENTAL local diagnostics
        (_verify_canonical_run_intent_if_resolvable) once its digest is
        recomputed to match (isolating this from the separate tamper-digest
        check).

        promotion_status is non-prod here (round-4 follow-up): at prod, the
        mandatory publication-record boundary rejects this manifest FIRST
        (no publication exists for it) before the local diagnostics this
        test exists to exercise would even matter -- see
        TestCanonicalPublicationRecord::test_forged_record_with_unallowlisted_producer_rejected
        for the prod-side producer-allowlist coverage through the persisted
        registry record."""
        fx = _CanonicalFixture(tmp_path)
        raw = json.loads(fx.run_intent_path.read_text())
        raw["producer"] = {"repo": "renquant-orchestrator", "entrypoint": "some.OtherTask"}
        fx.run_intent_path.write_text(json.dumps(raw))

        manifest = {
            "artifact_id": "canonical-gbdt-tampered-producer",
            "model_family": "gbdt-panel-ltr",
            "strategy": "renquant_104",
            "fingerprint": "sha256:candidate-tampered-producer",
            "uri": "object://renquant-artifacts/canonical-gbdt-tampered-producer.json",
            "promotion_status": "diagnostic",
            "metrics": {"accepted": True},
            # Built AFTER tampering, so run_intent_digest matches the
            # tampered bytes exactly -- isolates the producer-allowlist
            # failure from the separate tamper/digest-mismatch check.
            "provenance": build_canonical_provenance_reference(
                fx.run_intent_path, "sha256:candidate-tampered-producer",
            ),
        }
        with pytest.raises(ValueError, match="failed verification"):
            validate_artifact_manifest(manifest)

    def test_canonical_over_registered_experiment_output_rejected(self, tmp_path):
        """Design doc: 'Also keep the round-5 negative check available for
        canonical (does output_dir carry a real EXPLORATORY_ONLY marker?)
        by reusing the existing reject_exploratory_promotion call.' A
        candidate manifest whose own local_artifact_path resolves under a
        real, registered EXPLORATORY_ONLY experiment output must be
        rejected even though it declares kind='canonical' with an
        otherwise-valid artifact_digest binding."""
        run_dir = tmp_path / "sim_output" / "exp-canonical-bypass"
        write_experiment_classification(
            run_dir, experiment_id="exp-canonical-bypass",
            manifest_path="experiments/manifests/exp-canonical-bypass.json",
            manifest_digest="sha256:canonical-bypass", config_digest="sha256:c",
        )
        index_path = tmp_path / "experiments" / "manifests" / "INDEX.json"
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(
            json.dumps({"exp-canonical-bypass": {"digest": "sha256:canonical-bypass", "path": "x"}})
        )
        candidate_file = run_dir / "candidate_run" / "output.json"
        candidate_file.parent.mkdir(parents=True)
        candidate_file.write_text("{}")

        manifest = {
            "artifact_id": "candidate-from-exploratory-run",
            "model_family": "gbdt-panel-ltr",
            "strategy": "renquant_104",
            "fingerprint": "sha256:candidate",
            "uri": "object://renquant-artifacts/candidate.json",
            "promotion_status": "prod",
            "metrics": {"accepted": True},
            "local_artifact_path": str(candidate_file),
            "provenance": {
                "kind": "canonical",
                "run_intent_path": "store://nonexistent/run_intent.json",
                "run_intent_digest": "sha256:" + "0" * 64,
                "artifact_digest": "sha256:candidate",
            },
        }
        with pytest.raises(ValueError, match="EXPLORATORY_ONLY classification record"):
            validate_artifact_manifest(manifest)

    def test_canonical_kind_missing_required_subkeys_rejected(self, tmp_path):
        manifest = {
            "artifact_id": "candidate",
            "model_family": "gbdt-panel-ltr",
            "strategy": "renquant_104",
            "fingerprint": "sha256:candidate",
            "uri": "object://renquant-artifacts/candidate.json",
            "promotion_status": "prod",
            "metrics": {"accepted": True},
            "provenance": {"kind": "canonical"},
        }
        with pytest.raises(ValueError, match="missing required keys"):
            validate_artifact_manifest(manifest)


# ── round-4: the authoritative canonical publication store ─────────────────


def _write_valid_intent(run_dir: Path, run_id: str) -> Path:
    """A run_intent.json that passes intrinsic verification (allowlisted
    producer, well-formed pins, non-empty evidence) without needing real
    git checkouts -- publication-store tests exercise the registry
    boundary, which by design performs no environment I/O."""
    return write_canonical_run_intent(
        run_dir,
        run_id=run_id,
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
            for name in (
                "renquant-strategy-104", "renquant-pipeline", "renquant-model",
            )
        },
    )


class TestCanonicalPublicationRecord:
    """Codex round-4 review (2026-07-17) on this PR: "canonical provenance
    still fails open when the run-intent record is not resolvable... A
    promotion boundary cannot make local file visibility optional." Every
    promotion_status='prod' kind='canonical' manifest must now resolve the
    authoritative, producer-written publication record from the registry's
    own canonical publication store -- keyed by the manifest's OWN
    fingerprint -- and is rejected when that record is absent, unreadable,
    mismatched, or fails verification. Local path visibility is
    supplemental diagnostics only; it never decides whether canonical
    evidence is checked."""

    def _manifest(self, fingerprint: str, provenance: dict, **overrides) -> dict:
        manifest = {
            "artifact_id": "canonical-candidate",
            "model_family": "gbdt-panel-ltr",
            "strategy": "renquant_104",
            "fingerprint": fingerprint,
            "uri": "object://renquant-artifacts/canonical-candidate.bin",
            "promotion_status": "prod",
            "metrics": {"accepted": True},
            "provenance": provenance,
        }
        manifest.update(overrides)
        return manifest

    def test_prod_with_fabricated_nonlocal_run_intent_rejected(self, tmp_path):
        """THE negative test round 4 explicitly required: "a fabricated
        nonlocal run_intent_path plus matching self-declared digest is
        rejected." The store is real and non-empty (an UNRELATED genuine
        publication exists, proving this is not just an empty-store
        artifact); the candidate's provenance fields are perfectly
        self-consistent (artifact_digest == fingerprint, an opaque
        store:// run_intent_path nothing can resolve, a plausible
        run_intent_digest) -- and it is still rejected, because
        self-consistent fields are not authority: no publication is
        registered for THIS artifact digest."""
        pubs = tmp_path / "pubs"
        unrelated_intent = _write_valid_intent(tmp_path / "runs" / "other", "run-other")
        register_canonical_publication(
            pubs,
            run_intent_path=unrelated_intent,
            artifact_digest="sha256:some-other-artifact",
            artifact_uri="object://renquant-artifacts/other.bin",
        )

        manifest = self._manifest(
            "sha256:fabricated-candidate",
            {
                "kind": "canonical",
                "run_intent_path": "store://renquant-artifacts/fabricated/run_intent.json",
                "run_intent_digest": "sha256:" + "f" * 64,
                "artifact_digest": "sha256:fabricated-candidate",
            },
        )
        with pytest.raises(ValueError, match="no canonical publication record is registered"):
            validate_artifact_manifest(manifest, canonical_publications_dir=pubs)

    def test_prod_fails_closed_when_no_store_is_resolvable(self, tmp_path):
        """With NO caller-supplied store location, the default resolves to
        this repo's own registry/canonical_publications -- which carries no
        INDEX (no canonical publication has ever been reviewed into the
        registry). A prod canonical manifest must be rejected, not waved
        through: an unresolvable authoritative record is absence, and
        absence fails closed."""
        manifest = self._manifest(
            "sha256:no-store-candidate",
            {
                "kind": "canonical",
                "run_intent_path": "store://renquant-artifacts/x/run_intent.json",
                "run_intent_digest": "sha256:" + "a" * 64,
                "artifact_digest": "sha256:no-store-candidate",
            },
        )
        with pytest.raises(ValueError, match="canonical publication"):
            validate_artifact_manifest(manifest)

    def test_prod_registered_digest_mismatch_rejected(self, tmp_path):
        """A genuine publication exists for this artifact digest, but the
        manifest declares a DIFFERENT run_intent_digest than the one the
        publication binds -- rejected as mismatched (a manifest cannot
        re-attribute a published artifact to another run)."""
        pubs = tmp_path / "pubs"
        intent = _write_valid_intent(tmp_path / "runs" / "real", "run-real")
        register_canonical_publication(
            pubs,
            run_intent_path=intent,
            artifact_digest="sha256:published-artifact",
            artifact_uri="object://renquant-artifacts/published.bin",
        )
        manifest = self._manifest(
            "sha256:published-artifact",
            {
                "kind": "canonical",
                "run_intent_path": "store://renquant-artifacts/real/run_intent.json",
                "run_intent_digest": "sha256:" + "b" * 64,  # NOT the registered one
                "artifact_digest": "sha256:published-artifact",
            },
        )
        with pytest.raises(ValueError, match="does not match the registered publication binding"):
            validate_artifact_manifest(manifest, canonical_publications_dir=pubs)

    def test_prod_tampered_publication_record_rejected(self, tmp_path):
        """The persisted record file is edited AFTER publication (e.g. to
        swap the producer). The store is content-addressed: recomputing the
        record's sha256 against the indexed run_intent_digest detects the
        edit with no local build-machine path involved -- rejected."""
        pubs = tmp_path / "pubs"
        intent = _write_valid_intent(tmp_path / "runs" / "tamper", "run-tamper")
        register_canonical_publication(
            pubs,
            run_intent_path=intent,
            artifact_digest="sha256:tamper-artifact",
            artifact_uri="object://renquant-artifacts/tamper.bin",
        )
        digest = artifact_sha256(intent)
        record_path = pubs / _record_filename(digest)
        record = json.loads(record_path.read_text())
        record["producer"] = {"repo": "evil", "entrypoint": "evil.Task"}
        record_path.write_text(json.dumps(record))

        manifest = self._manifest(
            "sha256:tamper-artifact",
            {
                "kind": "canonical",
                "run_intent_path": "store://renquant-artifacts/tamper/run_intent.json",
                "run_intent_digest": digest,
                "artifact_digest": "sha256:tamper-artifact",
            },
        )
        with pytest.raises(ValueError, match="modified after publication"):
            validate_artifact_manifest(manifest, canonical_publications_dir=pubs)

    def test_forged_record_with_unallowlisted_producer_rejected(self, tmp_path):
        """Prod-side producer-allowlist enforcement through the PERSISTED
        registry record (the round-4 replacement for relying on local
        re-verification): an attacker forges a store entry wholesale --
        record bytes, content-addressed filename, and index entry all
        internally consistent -- but the record names a producer outside
        CANONICAL_PRODUCERS. The resolver's intrinsic verification of the
        persisted record rejects it."""
        pubs = tmp_path / "pubs"
        pubs.mkdir(parents=True)
        forged = {
            "schema_version": 1,
            "kind": "canonical-run-intent",
            "run_id": "run-forged",
            "run_type": "daily_full",
            "created_at": "2026-07-18T00:00:00Z",
            "producer": {"repo": "somewhere-else", "entrypoint": "not.Allowlisted"},
            "workflow_class": "canonical",
            "strategy_manifest_fingerprint": "sha256:s",
            "data_manifest_fingerprint": "sha256:d",
            "strategy_config_digest": "sha256:sc",
            "model_config_digest": "sha256:mc",
            "calendar_universe_digest": "sha256:u",
            "as_of": "2026-07-18",
            "code_pins": {
                name: {"commit": "0" * 40, "remote": "https://example.com/r"}
                for name in (
                    "renquant-strategy-104", "renquant-pipeline", "renquant-model",
                )
            },
        }
        forged_path = tmp_path / "forged_intent.json"
        forged_path.write_text(json.dumps(forged))
        digest = artifact_sha256(forged_path)
        (pubs / _record_filename(digest)).write_bytes(forged_path.read_bytes())
        (pubs / CANONICAL_PUBLICATIONS_INDEX_FILENAME).write_text(json.dumps({
            "sha256:forged-artifact": {
                "run_intent_digest": digest,
                "record": _record_filename(digest),
                "artifact_uri": "object://renquant-artifacts/forged.bin",
                "registered_at": "2026-07-18T00:00:00Z",
            }
        }))

        manifest = self._manifest(
            "sha256:forged-artifact",
            {
                "kind": "canonical",
                "run_intent_path": "store://renquant-artifacts/forged/run_intent.json",
                "run_intent_digest": digest,
                "artifact_digest": "sha256:forged-artifact",
            },
        )
        with pytest.raises(ValueError, match="CANONICAL_PRODUCERS allowlist"):
            validate_artifact_manifest(manifest, canonical_publications_dir=pubs)

    def test_prod_opaque_object_store_identity_accepted_via_publication_record(self, tmp_path):
        """The normal object-store identity shape round 3/4 called out as
        the primary registry deployment shape: the artifact's ONLY identity
        is an opaque object:// URI, run_intent_path is an opaque store://
        reference, and NOTHING resolves locally on the validating machine.
        Acceptance is decided entirely by resolving the authoritative
        publication record from the store -- proving the boundary no longer
        depends on local file visibility in either direction."""
        pubs = tmp_path / "pubs"
        intent = _write_valid_intent(tmp_path / "runs" / "opaque", "run-opaque")
        digest = artifact_sha256(intent)
        register_canonical_publication(
            pubs,
            run_intent_path=intent,
            artifact_digest="sha256:opaque-artifact",
            artifact_uri="object://renquant-artifacts/opaque.bin",
        )
        manifest = self._manifest(
            "sha256:opaque-artifact",
            {
                "kind": "canonical",
                # Deliberately NOT the local path the intent was written to:
                # the validating machine sees only the opaque reference.
                "run_intent_path": "store://renquant-artifacts/opaque/run_intent.json",
                "run_intent_digest": digest,
                "artifact_digest": "sha256:opaque-artifact",
            },
        )
        report = validate_artifact_manifest(manifest, canonical_publications_dir=pubs)
        assert report["ok"] is True

    def test_local_visibility_never_substitutes_for_publication_record(self, tmp_path):
        """Round-4 requirement 4, exercised in the direction that matters:
        a FULLY verifiable local run-intent (real git checkouts, matching
        pins -- the strongest possible local evidence) with NO publication
        in the store is still rejected at prod. The local walk is
        supplemental diagnostics; it is never the boundary, and local
        visibility cannot stand in for the authoritative registry record."""
        fx = _CanonicalFixture(tmp_path)
        pubs = tmp_path / "pubs"  # exists as a store location, but nothing registered
        pubs.mkdir(parents=True)
        manifest = self._manifest(
            "sha256:local-only-candidate",
            build_canonical_provenance_reference(
                fx.run_intent_path, "sha256:local-only-candidate",
            ),
        )
        with pytest.raises(ValueError, match="canonical publication"):
            validate_artifact_manifest(manifest, canonical_publications_dir=pubs)

    def test_register_is_append_only_per_artifact_digest(self, tmp_path):
        """A publication may never be silently rebound: registering the
        same artifact digest against a DIFFERENT run-intent record raises,
        while re-registering the identical binding is an idempotent no-op."""
        pubs = tmp_path / "pubs"
        intent_a = _write_valid_intent(tmp_path / "runs" / "a", "run-a")
        intent_b = _write_valid_intent(tmp_path / "runs" / "b", "run-b")
        register_canonical_publication(
            pubs, run_intent_path=intent_a,
            artifact_digest="sha256:artifact-x",
            artifact_uri="object://renquant-artifacts/x.bin",
        )
        # Idempotent re-registration of the identical binding.
        register_canonical_publication(
            pubs, run_intent_path=intent_a,
            artifact_digest="sha256:artifact-x",
            artifact_uri="object://renquant-artifacts/x.bin",
        )
        with pytest.raises(ValueError, match="may never be rebound"):
            register_canonical_publication(
                pubs, run_intent_path=intent_b,
                artifact_digest="sha256:artifact-x",
                artifact_uri="object://renquant-artifacts/x.bin",
            )
        entry, record, errors = resolve_canonical_publication("sha256:artifact-x", pubs)
        assert errors == []
        assert entry["run_intent_digest"] == artifact_sha256(intent_a)
        assert record["run_id"] == "run-a"

    def test_register_refuses_unverifiable_run_intent(self, tmp_path):
        """The publisher-side gate: the store never accepts a record that
        fails intrinsic verification (here: empty code_pins), so nothing
        unverifiable can ever become the authoritative publication."""
        run_dir = tmp_path / "runs" / "bad"
        bad_intent = write_canonical_run_intent(
            run_dir,
            run_id="run-bad",
            run_type="daily_full",
            producer={
                "repo": "renquant-orchestrator",
                "entrypoint": "daily.TrainGbdtArtifactTask",
            },
            strategy_manifest_fingerprint="sha256:s",
            data_manifest_fingerprint="sha256:d",
            strategy_config_digest="sha256:sc",
            model_config_digest="sha256:mc",
            calendar_universe_digest="sha256:u",
            as_of="2026-07-18",
            code_pins={},
        )
        with pytest.raises(ValueError, match="failed intrinsic verification"):
            register_canonical_publication(
                tmp_path / "pubs",
                run_intent_path=bad_intent,
                artifact_digest="sha256:bad-artifact",
                artifact_uri="object://renquant-artifacts/bad.bin",
            )
