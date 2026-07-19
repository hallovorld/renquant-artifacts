"""F-7 required-provenance enforcement window (renquant-artifacts#24 follow-up).

#24 made the ``provenance`` record unconditionally required in the standard
manifest validation funnel while its own review ordering sequenced the
consumer migrations (renquant-model#55, renquant-orchestrator#518) to land
LATER -- a flag-day break of every consumer repo's CI. These tests pin the
governed replacement, mirroring the umbrella resolver's
``ARTIFACT_DIGEST_REQUIRED_AFTER`` enforcement-window precedent:

* window open + manifest with NO ``provenance`` key -> FutureWarning, not
  ValueError (the pre-F-7 legacy shape keeps validating);
* window closed (date / RQ_REQUIRE_PROVENANCE=1 / require_provenance=True)
  -> the exact #24 strict behavior, unchanged;
* a PRESENT provenance record is ALWAYS fully verified -- window or no
  window -- so the tolerance is not a bypass;
* ``verify_artifact_provenance`` itself and the #24 canonical-publication
  paths stay unconditionally strict.

All date-sensitive assertions inject ``now``/``environ`` or monkeypatch the
window predicate -- nothing here flips on the wall clock when
PROVENANCE_REQUIRED_AFTER passes.
"""
from __future__ import annotations

import json
import warnings
from datetime import date, timedelta
from pathlib import Path

import pytest

from renquant_artifacts import (
    PROVENANCE_ENFORCEMENT_ENV,
    PROVENANCE_REQUIRED_AFTER,
    load_artifact_manifest,
    provenance_required,
    resolve_artifact_manifest,
    validate_artifact_manifest,
    verify_artifact_provenance,
)

_DAY = timedelta(days=1)
_BEFORE = PROVENANCE_REQUIRED_AFTER - _DAY
_AFTER = PROVENANCE_REQUIRED_AFTER + _DAY


def _legacy_manifest(**overrides) -> dict:
    """A pre-F-7 manifest shape: valid in every respect except that the
    ``provenance`` key does not exist at all (exactly what every consumer
    repo's committed fixtures and registries still publish until model#55 /
    orch#518 land)."""
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
    return payload


def _force_window(monkeypatch: pytest.MonkeyPatch, open_: bool) -> None:
    """Pin the funnel's window predicate for wall-clock-independent tests."""
    monkeypatch.setattr(
        "renquant_artifacts.validation.provenance_required", lambda: not open_,
    )
    monkeypatch.delenv(PROVENANCE_ENFORCEMENT_ENV, raising=False)


class TestProvenanceRequiredPredicate:
    """Unit contract of provenance_required(now=..., environ=...)."""

    def test_open_before_date_without_env(self) -> None:
        assert provenance_required(now=_BEFORE, environ={}) is False

    def test_closed_on_and_after_date(self) -> None:
        assert provenance_required(now=PROVENANCE_REQUIRED_AFTER, environ={}) is True
        assert provenance_required(now=_AFTER, environ={}) is True

    def test_env_flag_opts_in_early(self) -> None:
        env = {PROVENANCE_ENFORCEMENT_ENV: "1"}
        assert provenance_required(now=_BEFORE, environ=env) is True

    def test_env_flag_any_nonzero_value_opts_in(self) -> None:
        env = {PROVENANCE_ENFORCEMENT_ENV: "true"}
        assert provenance_required(now=_BEFORE, environ=env) is True

    def test_env_zero_or_empty_is_not_an_opt_in(self) -> None:
        assert provenance_required(now=_BEFORE, environ={PROVENANCE_ENFORCEMENT_ENV: "0"}) is False
        assert provenance_required(now=_BEFORE, environ={PROVENANCE_ENFORCEMENT_ENV: ""}) is False

    def test_env_can_never_reopen_a_closed_window(self) -> None:
        # Fail-closed one-way semantics: after the date, no environment
        # value weakens enforcement.
        env = {PROVENANCE_ENFORCEMENT_ENV: "0"}
        assert provenance_required(now=_AFTER, environ=env) is True

    def test_defaults_are_wall_clock_and_process_environ(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(PROVENANCE_ENFORCEMENT_ENV, "1")
        assert provenance_required() is True


class TestWindowOpenToleratesOnlyAbsence:
    """While the window is open, ONLY the missing-key legacy shape warns."""

    def test_missing_provenance_warns_and_validates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _force_window(monkeypatch, open_=True)
        with pytest.warns(FutureWarning, match=r"renquant-model#55.*renquant-orchestrator#518"):
            report = validate_artifact_manifest(_legacy_manifest())
        assert report == {
            "artifact_id": "panel-ltr-prod",
            "fingerprint": "sha256:artifact",
            "ok": True,
        }

    def test_load_artifact_manifest_tolerates_legacy_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _force_window(monkeypatch, open_=True)
        path = tmp_path / "legacy.json"
        path.write_text(json.dumps(_legacy_manifest()), encoding="utf-8")
        with pytest.warns(FutureWarning, match=r"provenance"):
            manifest = load_artifact_manifest(path)
        assert manifest["artifact_id"] == "panel-ltr-prod"

    def test_resolve_artifact_manifest_tolerates_legacy_registry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _force_window(monkeypatch, open_=True)
        registry = tmp_path / "registry"
        registry.mkdir()
        (registry / "legacy.json").write_text(
            json.dumps(_legacy_manifest()), encoding="utf-8",
        )
        with pytest.warns(FutureWarning, match=r"PROVENANCE_REQUIRED_AFTER"):
            manifest = resolve_artifact_manifest(registry, artifact_id="panel-ltr-prod")
        assert manifest["fingerprint"] == "sha256:artifact"

    def test_present_but_malformed_provenance_still_raises(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A manifest CARRYING the key was written post-F-7 -- it has no
        # legacy excuse and must verify fully even inside the window.
        _force_window(monkeypatch, open_=True)
        with pytest.raises(ValueError, match="missing a required 'provenance' record"):
            validate_artifact_manifest(_legacy_manifest(provenance={}))
        with pytest.raises(ValueError, match="missing a required 'provenance' record"):
            validate_artifact_manifest(_legacy_manifest(provenance="none"))

    def test_present_none_kind_prod_combination_still_raises(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _force_window(monkeypatch, open_=True)
        with pytest.raises(ValueError, match="cannot be combined with"):
            validate_artifact_manifest(_legacy_manifest(provenance={"kind": "none"}))

    def test_prod_canonical_without_publication_record_still_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The #24 round-4 canonical-publication boundary is NOT relaxed by
        # the window: a prod manifest with self-consistent canonical fields
        # but no resolvable publication record fails closed exactly as on
        # main.
        _force_window(monkeypatch, open_=True)
        manifest = _legacy_manifest(
            provenance={
                "kind": "canonical",
                "run_intent_path": str(tmp_path / "nowhere" / "run_intent.json"),
                "run_intent_digest": "sha256:" + "0" * 64,
                "artifact_digest": "sha256:artifact",
            },
        )
        with pytest.raises(ValueError, match="required registry bindings|trusted registry snapshot"):
            validate_artifact_manifest(manifest)

    def test_verify_artifact_provenance_primitive_stays_strict(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The exported strict primitive is untouched by the window: direct
        # callers (including the canonical-publication surfaces and the
        # migrated consumers) always get the full #24 behavior.
        _force_window(monkeypatch, open_=True)
        with pytest.raises(ValueError, match="missing a required 'provenance' record"):
            verify_artifact_provenance(_legacy_manifest())


class TestStrictModeIsTheFullF7Contract:
    """Closing the window (by any of the three switches) restores the exact
    #24 behavior for the missing-provenance shape."""

    def test_window_closed_missing_provenance_raises(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _force_window(monkeypatch, open_=False)
        with pytest.raises(ValueError, match="missing a required 'provenance' record"):
            validate_artifact_manifest(_legacy_manifest())

    def test_env_flag_closes_window_end_to_end(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # No monkeypatching of the predicate here: the real environment
        # flag must force strictness regardless of today's date.
        monkeypatch.setenv(PROVENANCE_ENFORCEMENT_ENV, "1")
        with pytest.raises(ValueError, match="missing a required 'provenance' record"):
            validate_artifact_manifest(_legacy_manifest())

    def test_require_provenance_param_closes_window_per_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _force_window(monkeypatch, open_=True)
        with pytest.raises(ValueError, match="missing a required 'provenance' record"):
            validate_artifact_manifest(_legacy_manifest(), require_provenance=True)

        path = tmp_path / "legacy.json"
        path.write_text(json.dumps(_legacy_manifest()), encoding="utf-8")
        with pytest.raises(ValueError, match="missing a required 'provenance' record"):
            load_artifact_manifest(path, require_provenance=True)

        registry = tmp_path / "registry"
        registry.mkdir()
        (registry / "legacy.json").write_text(
            json.dumps(_legacy_manifest()), encoding="utf-8",
        )
        with pytest.raises(ValueError, match="missing a required 'provenance' record"):
            resolve_artifact_manifest(
                registry, artifact_id="panel-ltr-prod", require_provenance=True,
            )

    def test_strict_mode_emits_no_tolerance_warning(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A compliant post-migration manifest validates silently in strict
        # mode -- the warning exists only for the tolerated legacy shape.
        _force_window(monkeypatch, open_=False)
        manifest = _legacy_manifest(
            promotion_status="shadow",
            metrics={"accepted": False},
            provenance={"kind": "none"},
        )
        with warnings.catch_warnings():
            warnings.simplefilter("error", FutureWarning)
            report = validate_artifact_manifest(manifest)
        assert report["ok"] is True


def test_window_constant_is_a_realistic_post_migration_date() -> None:
    # Guard against the window being (re)set to a date that has already
    # passed at authoring time -- that would silently re-create the #24
    # flag-day. If this fails because the date HAS legitimately passed,
    # the migrations have had their window: delete the tolerance branch
    # rather than bumping the constant without a review.
    assert PROVENANCE_REQUIRED_AFTER > date(2026, 7, 18)
