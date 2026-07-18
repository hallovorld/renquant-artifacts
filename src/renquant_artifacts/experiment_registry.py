"""Experiment-registry contract: pin verification + EXPLORATORY_ONLY enforcement.

This is the canonical, shared implementation of the "registered experiment"
governance contract used by any producer of non-production sim/backtest/
score-backfill output across the RenQuant multirepo -- e.g. the umbrella
repo's ``scripts/run_sim_104.py`` (PR #471, F-7) and renquant-model's
score-backfill tooling (``experiments/ensemble_phase0/backfill_scores.py``).
Producers MUST reuse these functions rather than hand-rolling their own pin
verification or EXPLORATORY_ONLY marker logic -- see the Codex round-5/6
review of umbrella PR #471 and the "triple-impl fingerprint" incident this
mirrors (three independently hand-copied ``model_content_sha256``
implementations silently diverged; the permanent fix was one shared impl in
``renquant_common.model_fingerprint``, not three audited copies).

Five pin categories are REQUIRED for a registered experiment manifest:

* ``strategy_config``   -- commit pin for the renquant-strategy-104 subrepo
* ``pipeline_version``  -- commit pin for the renquant-pipeline subrepo
* ``data_snapshot``     -- fingerprint of the data manifest the run reads
* ``model_artifact``    -- fingerprint of the model artifact file the run reads
* ``calendar_universe`` -- digest of the trading universe/watchlist the run scores

Each category is verified against the ACTUAL current environment before a
run is allowed to proceed. A declared pin that is merely *present* proves
nothing about reproducibility on its own -- that was exactly the gap Codex
flagged 2026-07-14: "pins is optional and is not verified... A config hash
alone cannot make an ensemble or score-backfill result reproducible."

This module also owns the durable EXPLORATORY_ONLY classification marker
and the ``reject_exploratory_promotion`` enforcement helper that
:mod:`renquant_artifacts.validation` wires into the real promotion/admission
path (:class:`ValidateArtifactManifestTask`) -- a marker file nothing
consumes is not governance, it is decoration.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from renquant_common.model_fingerprint import artifact_sha256

from .contracts import hash_jsonable

EXPERIMENT_MANIFEST_REQUIRED_KEYS = frozenset(
    {
        "experiment_id", "config_path", "config_digest", "status", "pins",
        # A registered experiment must supply the ACTUAL evidence needed to
        # verify pins.data_snapshot and pins.model_artifact against reality
        # (see verify_experiment_pins below) -- a declared pin value with
        # nothing to check it against is not verifiable, so the manifest
        # must point at the concrete files.
        "data_manifest_path", "model_artifact_path",
    }
)
EXPERIMENT_MANIFEST_VALID_STATUSES = frozenset({"ACTIVE", "COMPLETED", "RETIRED"})

EXPERIMENT_PINS_REQUIRED_KEYS = frozenset(
    {"data_snapshot", "model_artifact", "strategy_config",
     "pipeline_version", "calendar_universe"}
)

#: Recognized ``provenance.kind`` values for a CANDIDATE ARTIFACT manifest
#: (the promotion-boundary side -- not to be confused with the experiment
#: manifest schema above). Codex review 2026-07-14 (round covering this same
#: PR): "The promotion guard is bypassable because provenance is optional
#: and self-declared... An experiment-derived result can therefore be
#: promoted simply by omitting provenance_dir." ``provenance`` is now a
#: REQUIRED manifest field with an explicit, narrow, auditable kind:
#:
#: * ``"experiment"`` -- the artifact derives from a registered
#:   ``run_sim_104.py``-style experiment/sim run. Verified deterministically
#:   via :func:`reject_exploratory_promotion` against the immutable manifest
#:   registry (see :func:`verify_manifest_registered`) -- see that
#:   function's docstring for the full fail-closed contract.
#: * ``"none"`` -- an explicit declaration that this artifact was NOT
#:   produced by a registered experiment/sim run (e.g. artifacts published
#:   through the model-factory training pipeline, which never runs through
#:   experiment mode and carries its own separate WF-gate evidence contract
#:   -- ``validate_panel_artifact_contract`` / ``validate_model_evidence_contract``,
#:   already enforced upstream by the producer before this validator runs).
#:   This is the narrow allowlist the gap review explicitly permits ("If
#:   there's a legitimate exemption, it must be an explicit, narrow,
#:   auditable allowlist -- not 'absence of the field defaults to pass'").
#:   It is honest about a real residual limit: ``kind="none"`` is a
#:   self-declared field with no cryptographic binding to producer identity,
#:   the same residual-trust status as this codebase's other self-declared
#:   evidence fields (``code_commit``, ``config_fingerprint``). What it DOES
#:   fix is the specific bypass Codex found: silent OMISSION of the whole
#:   ``provenance`` field is no longer possible -- every manifest must make
#:   an explicit, git-reviewable act either way. As of the F-7 canonical
#:   follow-up, ``kind="none"`` additionally can never be combined with
#:   ``promotion_status="prod"`` -- see :func:`verify_artifact_provenance`.
#: * ``"canonical"`` -- the artifact derives from an automated, unreviewed
#:   canonical daily-full training run (no per-run PR the way a registered
#:   experiment has -- see :mod:`renquant_artifacts.canonical_registry`'s
#:   module docstring for why that path can't reuse the experiment INDEX.json
#:   pattern verbatim). Verified via the ``provenance.artifact_digest ==
#:   manifest["fingerprint"]`` binding, checked unconditionally and first --
#:   see :func:`verify_artifact_provenance` for the full contract.
PROVENANCE_KINDS = frozenset({"experiment", "canonical", "none"})

#: Required keys inside ``provenance`` when ``kind == "experiment"``.
PROVENANCE_EXPERIMENT_REQUIRED_KEYS = frozenset({"dir", "registry_index_path"})

#: Required keys inside ``provenance`` when ``kind == "canonical"``. See
#: :mod:`renquant_artifacts.canonical_registry` for the record these
#: reference and :func:`verify_artifact_provenance` for how they are
#: enforced -- ``artifact_digest`` must equal the manifest's own
#: ``fingerprint`` UNCONDITIONALLY, which is what makes this binding work for
#: opaque ``store://``/``object://``-only artifact identities with no local
#: path to resolve at all.
PROVENANCE_CANONICAL_REQUIRED_KEYS = frozenset(
    {"run_intent_path", "run_intent_digest", "artifact_digest"}
)

#: Manifest keys that MAY carry a real, locally-resolvable filesystem
#: reference to the artifact's own on-disk content -- as opposed to an
#: opaque content-addressed ``store://``/``object://`` URI resolved by
#: out-of-repo object-store infrastructure this package has no access to.
#: Used ONLY to detect -- never to accept -- a real EXPLORATORY_ONLY
#: classification marker sitting at/above the artifact's own real content;
#: see :func:`_verify_none_provenance`. These are the SAME fields real
#: producers already set for an entirely independent, load-bearing reason
#: (so the artifact can actually be located/read at all) --
#: ``renquant_model_gbdt``/``renquant_model_patchtst``'s
#: ``BuildArtifactManifestTask`` thread ``local_artifact_path``/
#: ``artifact_path`` onto the manifest from ``_RUNTIME_ARTIFACT_FIELDS``,
#: and ``experiments/gbdt_scratch_from_archived_20260528/promote_candidate.py``
#: sets ``local_artifact_path`` by hand -- this is not a field invented for
#: this check.
_LOCAL_ARTIFACT_PATH_KEYS = ("local_artifact_path", "artifact_path", "uri")

#: Bound on how far up the directory tree :func:`_verify_none_provenance`
#: walks looking for a classification marker, so an artifact living deep
#: under an unrelated tree cannot trigger a pathological/unbounded scan.
_MAX_MARKER_SEARCH_LEVELS = 6

#: Which subrepo (per subrepos.lock.json ``name``) each code-pin category
#: verifies against. Only the two code-commit categories participate here;
#: the other three pin categories verify against content fingerprints
#: rather than a git commit.
CODE_PIN_SUBREPOS: dict[str, str] = {
    "strategy_config": "renquant-strategy-104",
    "pipeline_version": "renquant-pipeline",
}

CLASSIFICATION_FILENAME = "_experiment_classification.json"


# ---------------------------------------------------------------------------
# Code-pin verification (git HEAD / dirty state / remote)
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ("git", "-C", str(repo), *args), text=True, stderr=subprocess.DEVNULL,
    ).strip()


def _normalize_remote(url: str) -> str:
    url = url.strip().rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    return url.lower()


def verify_code_pin(
    repo_path: Path, expected_commit: str, expected_remote: str,
) -> list[str]:
    """Check HEAD, dirty state, and remote URL against a declared code pin.

    Fails closed (non-empty return) if the commit hash or remote URL is
    missing, if HEAD does not match, if the checkout is dirty, or if the
    remote does not match (after trailing-slash/``.git``/case
    normalization). This is the same discipline
    ``scripts/run_sim_104.py``'s ``_verify_pin`` already applies to the
    strategy config specifically, generalized so every code-pin category
    gets identical treatment.
    """
    errors: list[str] = []
    if not expected_commit:
        errors.append("pin has no commit hash")
    if not expected_remote:
        errors.append("pin has no remote URL")
    if errors:
        return errors

    try:
        head = _git(repo_path, "log", "-1", "--format=%H")
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        return [f"git metadata failed: {exc}"]

    if not head.startswith(expected_commit):
        errors.append(
            f"HEAD {head[:12]} does not match pinned commit {expected_commit[:12]}"
        )

    try:
        dirty = bool(_git(repo_path, "status", "--porcelain"))
    except subprocess.CalledProcessError as exc:
        return [f"git dirty check failed: {exc}"]
    if dirty:
        errors.append("working tree is dirty")

    try:
        actual_remote = _git(repo_path, "remote", "get-url", "origin")
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        return [f"could not read remote URL: {exc}"]

    if _normalize_remote(actual_remote) != _normalize_remote(expected_remote):
        errors.append(
            f"remote URL mismatch: pin={expected_remote} vs local={actual_remote}"
        )

    return errors


# ---------------------------------------------------------------------------
# Content-fingerprint pin verification (data / model artifact / universe)
# ---------------------------------------------------------------------------


def verify_data_snapshot_pin(
    expected_fingerprint: str, data_manifest: dict[str, Any],
) -> list[str]:
    """Check a declared ``data_snapshot`` pin against an actual data manifest.

    ``data_manifest`` should already satisfy
    :func:`renquant_base_data.validate_data_manifest`; this only checks that
    its ``fingerprint`` field matches what the experiment declared.
    """
    if not expected_fingerprint:
        return ["pin has no data_snapshot fingerprint"]
    actual = data_manifest.get("fingerprint")
    if not actual:
        return ["data manifest has no fingerprint"]
    if actual != expected_fingerprint:
        return [f"data_snapshot mismatch: pin={expected_fingerprint} vs manifest={actual}"]
    return []


def verify_model_artifact_pin(
    expected_fingerprint: str, artifact_path: Path | str,
) -> list[str]:
    """Check a declared ``model_artifact`` pin against the artifact file's hash.

    Uses :func:`renquant_common.model_fingerprint.artifact_sha256` -- the one
    canonical full-file hash implementation -- rather than a locally
    hand-rolled hash, per the fingerprint triple-impl lesson.
    """
    if not expected_fingerprint:
        return ["pin has no model_artifact fingerprint"]
    path = Path(artifact_path)
    if not path.exists():
        return [f"model artifact not found: {path}"]
    actual = artifact_sha256(path)
    if actual != expected_fingerprint:
        return [f"model_artifact mismatch: pin={expected_fingerprint} vs file={actual}"]
    return []


def verify_calendar_universe_pin(
    expected_digest: str, universe: list[str],
) -> list[str]:
    """Check a declared ``calendar_universe`` pin against the resolved universe.

    Uses :func:`renquant_artifacts.contracts.hash_jsonable` over the sorted,
    de-duplicated ticker list -- the same stable-hash convention already
    used for ``watchlist_hash`` in run-bundle provenance.
    """
    if not expected_digest:
        return ["pin has no calendar_universe digest"]
    actual = hash_jsonable(sorted({str(s) for s in universe}))
    if actual != expected_digest:
        return [f"calendar_universe mismatch: pin={expected_digest} vs resolved={actual}"]
    return []


def verify_experiment_pins(
    pins: dict[str, Any],
    *,
    repo_root: Path | str,
    data_manifest: dict[str, Any] | None = None,
    model_artifact_path: Path | str | None = None,
    universe: list[str] | None = None,
    subrepos_lock: dict[str, Any] | None = None,
) -> list[str]:
    """Verify all 5 required pin categories against the actual environment.

    Fails closed: a category whose supporting evidence (``data_manifest``,
    ``model_artifact_path``, or ``universe``) was not supplied by the caller
    is an ERROR, not a silently-skipped pass -- a registered experiment must
    supply everything needed to prove reproducibility, not merely declare
    pin values (Codex review 2026-07-14, finding 2: "pins is optional and is
    not verified").
    """
    missing = EXPERIMENT_PINS_REQUIRED_KEYS - pins.keys()
    if missing:
        return [f"pins missing required keys: {sorted(missing)}"]

    errors: list[str] = []
    repo_root = Path(repo_root)

    if subrepos_lock is None:
        lock_path = repo_root / "subrepos.lock.json"
        if not lock_path.exists():
            return [f"subrepos.lock.json not found at {lock_path} -- cannot verify code pins"]
        subrepos_lock = json.loads(lock_path.read_text())

    lock_by_name = {
        entry.get("name"): entry for entry in subrepos_lock.get("subrepos", [])
    }

    for pin_key, subrepo_name in CODE_PIN_SUBREPOS.items():
        entry = lock_by_name.get(subrepo_name)
        if entry is None:
            errors.append(
                f"pins.{pin_key}: {subrepo_name} not found in subrepos.lock.json"
            )
            continue
        expected_commit = str(pins[pin_key])
        local_path = Path(entry.get("local_path", ""))
        if not local_path.is_absolute():
            local_path = (repo_root / local_path).resolve()
        pin_errors = verify_code_pin(local_path, expected_commit, entry.get("remote", ""))
        errors.extend(f"pins.{pin_key}: {e}" for e in pin_errors)

        # The declared pin must ALSO match the lock's own current pin, not
        # only the checkout someone happens to be sitting on locally. An
        # experiment that pins a commit the lock has since moved past is not
        # "verified" just because a stale local checkout still matches it.
        locked_commit = entry.get("commit", "")
        if locked_commit and expected_commit != locked_commit:
            errors.append(
                f"pins.{pin_key}: declared {expected_commit[:12]} does not "
                f"match subrepos.lock.json pin {locked_commit[:12]}"
            )

    if data_manifest is None:
        errors.append("pins.data_snapshot: no data_manifest supplied to verify against")
    else:
        errors.extend(
            f"pins.data_snapshot: {e}"
            for e in verify_data_snapshot_pin(pins["data_snapshot"], data_manifest)
        )

    if model_artifact_path is None:
        errors.append("pins.model_artifact: no model_artifact_path supplied to verify against")
    else:
        errors.extend(
            f"pins.model_artifact: {e}"
            for e in verify_model_artifact_pin(pins["model_artifact"], model_artifact_path)
        )

    if universe is None:
        errors.append("pins.calendar_universe: no universe supplied to verify against")
    else:
        errors.extend(
            f"pins.calendar_universe: {e}"
            for e in verify_calendar_universe_pin(pins["calendar_universe"], universe)
        )

    return errors


# ---------------------------------------------------------------------------
# Manifest registry (immutable location + digest-bound registration)
# ---------------------------------------------------------------------------


def verify_manifest_registered(
    manifest_digest: str,
    experiment_id: str,
    index_path: Path | str,
) -> list[str]:
    """Check that a manifest digest is a registered entry in an immutable index.

    The index is a small git-tracked JSON file mapping
    ``experiment_id -> {"digest": ..., "path": ...}``. Registering a
    manifest is a deliberate, auditable act (append an entry to the index in
    the same commit/PR that adds the manifest) -- an arbitrary,
    caller-supplied manifest must never be accepted just because it happens
    to live under the right directory (Codex review 2026-07-14, finding 3:
    "Require a registered manifest location or an immutable registry
    record").
    """
    index_path = Path(index_path)
    if not index_path.exists():
        return [f"manifest registry index not found: {index_path}"]
    index = json.loads(index_path.read_text())
    entry = index.get(experiment_id)
    if entry is None:
        return [f"experiment_id {experiment_id!r} is not registered in {index_path}"]
    registered_digest = entry.get("digest")
    if not registered_digest:
        return [f"registry entry for {experiment_id!r} has no digest"]
    if registered_digest != manifest_digest:
        return [
            f"manifest digest mismatch for {experiment_id!r}: "
            f"registered={registered_digest} actual={manifest_digest}"
        ]
    return []


# ---------------------------------------------------------------------------
# EXPLORATORY_ONLY classification marker + enforcement
# ---------------------------------------------------------------------------


def write_experiment_classification(
    output_dir: Path | str,
    *,
    experiment_id: str,
    manifest_path: str,
    manifest_digest: str,
    config_digest: str,
    extra: dict[str, Any] | None = None,
) -> Path:
    """Atomically write a durable EXPLORATORY_ONLY classification marker.

    Written BEFORE any reusable output is produced, using write-to-tmp-then-
    rename so a crash mid-run never leaves a partially-written marker (or an
    output directory with real results but no marker at all).
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    classification: dict[str, Any] = {
        "classification": "EXPLORATORY_ONLY",
        "experiment_id": experiment_id,
        "manifest_path": manifest_path,
        "manifest_digest": manifest_digest,
        "config_digest": config_digest,
    }
    if extra:
        classification.update(extra)
    out = output_dir / CLASSIFICATION_FILENAME
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(classification, indent=2) + "\n")
    tmp.rename(out)
    return out


def reject_exploratory_promotion(
    output_dir: Path | str,
    *,
    registry_index_path: Path | str | None = None,
) -> None:
    """Raise unless ``output_dir`` proves verifiable, non-exploratory origin.

    This is the enforcement point:
    :class:`renquant_artifacts.validation.ValidateArtifactManifestTask` calls
    this (via :func:`verify_artifact_provenance`) for any candidate manifest
    that declares ``provenance.kind == "experiment"``.

    Fails closed on every unverifiable case (Codex review 2026-07-14,
    "the promotion guard is bypassable because provenance is optional and
    self-declared"):

    * **Missing marker.** A missing ``_experiment_classification.json`` is
      NO LONGER silently treated as "not exploratory, proceed" -- that was
      "a second layer of the same bypass" (a caller could point
      ``output_dir`` at an empty/fake directory). A missing marker now
      raises: it is treated as unverifiable provenance, not proof of a
      clean run.
    * **Unregistered / falsified marker.** When ``registry_index_path`` is
      supplied, the marker's own ``manifest_digest``/``experiment_id`` are
      cross-checked against :func:`verify_manifest_registered` -- the same
      immutable, content-addressed registry index this PR already built for
      the experiment-manifest side. A GENUINELY REGISTERED experiment
      manifest is rejected UNCONDITIONALLY, regardless of what the
      classification file's own (filesystem-writable, hence falsifiable)
      ``classification`` field claims: registration -- not the mutable
      marker value -- is the tamper-resistant ground truth, because every
      registered experiment manifest is EXPLORATORY_ONLY by construction
      (``run_sim_104.py``'s experiment mode never writes any other
      classification for a registered manifest). This closes the
      "falsified classification field" variant of the bypass: copying a
      real registered digest into a hand-edited marker that claims
      non-exploratory status does not help.
    * **Ambiguous / unverifiable.** If the digest is NOT registered and the
      marker does not itself self-report EXPLORATORY_ONLY, the record is
      REJECTED anyway when ``registry_index_path`` was supplied --
      unverifiable provenance is treated as unsafe, not safe, once a caller
      has affirmatively claimed experiment provenance.
    * If ``registry_index_path`` is omitted (legacy direct callers with no
      registry to check against), the marker must still exist, and rejection
      falls back to the marker's own self-reported ``classification`` field.
    """
    marker = Path(output_dir) / CLASSIFICATION_FILENAME
    if not marker.exists():
        raise ValueError(
            f"cannot verify promotion safety: no experiment classification "
            f"record found at {marker} -- a missing marker is not proof of "
            "non-exploratory origin (Codex review 2026-07-14: a missing "
            "marker must not be silently treated as 'not exploratory')"
        )
    raw = json.loads(marker.read_text())
    self_reported_exploratory = raw.get("classification") == "EXPLORATORY_ONLY"

    if registry_index_path is not None:
        manifest_digest = raw.get("manifest_digest")
        experiment_id = raw.get("experiment_id")
        if not manifest_digest or not experiment_id:
            registry_errors = [
                "classification record has no manifest_digest/experiment_id "
                "to verify registration against"
            ]
        else:
            registry_errors = verify_manifest_registered(
                manifest_digest, experiment_id, registry_index_path,
            )
        if not registry_errors:
            raise ValueError(
                f"Cannot promote output of registered experiment "
                f"(experiment_id={experiment_id!r}, "
                f"manifest_digest={manifest_digest}): registered "
                "experiment-manifest output is EXPLORATORY_ONLY by "
                "construction and is never eligible for promotion, "
                "regardless of the classification record's self-reported "
                "'classification' value"
            )
        if not self_reported_exploratory:
            raise ValueError(
                f"classification record at {marker} could not be verified: "
                f"not a registered experiment ({registry_errors}) and does "
                "not self-report EXPLORATORY_ONLY -- ambiguous/unverifiable "
                "provenance is rejected, not accepted"
            )

    if self_reported_exploratory:
        raise ValueError(
            f"Cannot promote EXPLORATORY_ONLY output "
            f"(experiment_id={raw.get('experiment_id')}, "
            f"manifest={raw.get('manifest_path')})"
        )


def _resolve_local_path(raw: Any) -> Path | None:
    """Resolve a single manifest identity-field value to a real,
    locally-existing :class:`Path`, or ``None`` if it isn't a usable local
    reference -- an opaque ``store://``/``object://`` URI (resolved by
    object-store infrastructure outside this repo, with no local filesystem
    truth to inspect), or a path that simply doesn't exist on this
    filesystem.

    Shared by :func:`_candidate_local_artifact_dirs` (the ``kind="none"``
    check) and the ``kind="canonical"`` supplemental local check in
    :func:`verify_artifact_provenance` -- one "is this actually a local
    path" rule rather than two independently hand-copied ones.
    """
    if not raw or not isinstance(raw, str):
        return None
    if "://" in raw:
        if not raw.startswith("file://"):
            return None
        raw = raw[len("file://"):]
    path = Path(raw)
    if not path.exists():
        return None
    return path


def _candidate_local_artifact_dirs(manifest: dict[str, Any]) -> list[Path]:
    """Resolve real, on-disk directories implied by a manifest's OWN
    identity fields -- never a value read from inside the caller-supplied
    ``provenance`` dict itself (that would just reintroduce the
    caller-asserted-``dir`` bypass ``kind="experiment"`` already had to
    close in the prior round). Opaque ``store://``/``object://`` references
    are skipped: they are resolved by object-store infrastructure outside
    this repo and carry no local filesystem truth to inspect.
    """
    dirs: list[Path] = []
    for key in _LOCAL_ARTIFACT_PATH_KEYS:
        path = _resolve_local_path(manifest.get(key))
        if path is None:
            continue
        dirs.append(path if path.is_dir() else path.parent)
    return dirs


def _find_repo_root_with_subrepos_lock(start: Path) -> Path | None:
    """Walk upward from ``start``, bounded by :data:`_MAX_MARKER_SEARCH_LEVELS`,
    looking for a directory containing ``subrepos.lock.json`` -- used to
    auto-derive the ``repo_root``
    :func:`renquant_artifacts.canonical_registry.verify_canonical_run_intent`
    needs for its code-pin checks, when the supplemental local
    ``kind="canonical"`` check (:func:`_verify_canonical_run_intent_if_resolvable`)
    has no caller-supplied ``repo_root`` to work with. Mirrors
    :func:`_find_experiment_classification_marker`'s bounded-walk idiom.
    """
    current = start.resolve()
    for _ in range(_MAX_MARKER_SEARCH_LEVELS):
        if (current / "subrepos.lock.json").exists():
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


def _verify_canonical_not_exploratory(manifest: dict[str, Any]) -> None:
    """Round-5-style negative check for ``kind="canonical"``, mirroring
    :func:`_verify_none_provenance`: does the artifact's own on-disk
    identity resolve under a real ``EXPLORATORY_ONLY`` classification
    marker? A ``"canonical"`` declaration is exactly as dishonest as a
    ``"none"`` declaration would be in that case, so this reuses the same
    :func:`reject_exploratory_promotion` enforcement rather than
    re-implementing the self-report/registration logic a second time.
    """
    for candidate_dir in _candidate_local_artifact_dirs(manifest):
        marker = _find_experiment_classification_marker(candidate_dir)
        if marker is None:
            continue
        try:
            reject_exploratory_promotion(marker.parent)
        except ValueError as exc:
            raise ValueError(
                "artifact manifest declares provenance.kind='canonical' but "
                f"its own artifact path ({candidate_dir}) resolves under a "
                f"real EXPLORATORY_ONLY classification record ({marker}) -- "
                "a 'canonical' declaration is not honest here"
            ) from exc


def _verify_canonical_publication_record(
    manifest: dict[str, Any],
    provenance: dict[str, Any],
    canonical_publications_dir: Path | str | None,
) -> None:
    """MANDATORY, fail-closed resolution of the authoritative canonical
    publication record for a ``promotion_status="prod"`` manifest -- the
    round-4 promotion boundary (Codex review 2026-07-17 on
    renquant-artifacts#24: "A promotion boundary cannot make local file
    visibility optional... verify_artifact_provenance must resolve that
    record for every promotion_status=prod canonical manifest and reject it
    if absent, unreadable, mismatched, or if its ... evidence does not
    verify").

    The record is resolved from the canonical publication store
    (:mod:`renquant_artifacts.canonical_registry`,
    ``registry/canonical_publications/`` by default -- a review-gated,
    content-addressed, append-only store inside the artifact registry
    itself), keyed by the manifest's OWN ``fingerprint``. Self-consistent
    manifest fields are NOT authority: a fabricated
    ``run_intent_path``/``run_intent_digest`` pair -- local, nonlocal, or
    opaque ``store://``/``object://`` -- fails here unless a genuine,
    producer-written publication for this exact artifact digest exists in
    the store AND its registered ``run_intent_digest`` matches the one the
    manifest declares AND the persisted record itself verifies. This runs
    UNCONDITIONALLY for prod: no local-path resolvability check gates it.
    """
    from . import canonical_registry

    if canonical_publications_dir is None:
        canonical_publications_dir = canonical_registry.default_canonical_publications_dir()
    entry, _record, errors = canonical_registry.resolve_canonical_publication(
        manifest["fingerprint"], canonical_publications_dir,
    )
    if errors:
        raise ValueError(
            "prod canonical provenance rejected: the authoritative canonical "
            "publication record could not be resolved and verified "
            "(promotion_status='prod' requires a registered publication in "
            "the canonical publication store -- self-consistent manifest "
            f"fields are not authority): {errors}"
        )
    registered_digest = entry.get("run_intent_digest")
    if registered_digest != provenance.get("run_intent_digest"):
        raise ValueError(
            "prod canonical provenance rejected: provenance.run_intent_digest "
            f"({provenance.get('run_intent_digest')!r}) does not match the "
            f"registered publication binding for this artifact "
            f"({registered_digest!r})"
        )


def _verify_canonical_run_intent_if_resolvable(provenance: dict[str, Any]) -> None:
    """SUPPLEMENTAL local diagnostics for ``kind="canonical"`` provenance --
    never the promotion boundary, and never a condition for whether
    canonical evidence is checked (Codex round-4 review, requirement 4:
    "Keep any local path walk only as supplemental diagnostics, never as
    the condition that decides whether canonical evidence is checked").
    For ``promotion_status="prod"`` the mandatory boundary is
    :func:`_verify_canonical_publication_record`, which has ALREADY run
    unconditionally by the time this is called and depends on no local
    path visibility at all.

    What this adds when (and only when) local truth happens to be visible:
    if ``provenance["run_intent_path"]`` resolves to a real local file (or
    a directory containing one), this recomputes its content hash and
    checks it against ``run_intent_digest`` (a tamper check on the local
    copy), then -- if a ``subrepos.lock.json`` can be discovered via a
    bounded upward walk from that file -- runs the full
    :func:`renquant_artifacts.canonical_registry.verify_canonical_run_intent`
    check (required keys, producer allowlist, and all 3 code pins against
    the actual current checkout). It can therefore only ADD rejections on
    machines where the producing environment is inspectable; its silence on
    machines where nothing is local decides nothing.
    """
    # Local import: canonical_registry imports verify_code_pin FROM this
    # module, so importing it back at module scope here would be circular.
    from . import canonical_registry

    raw = provenance.get("run_intent_path")
    path = _resolve_local_path(raw if isinstance(raw, str) else None)
    if path is None:
        return
    if path.is_dir():
        path = path / canonical_registry.CANONICAL_RUN_INTENT_FILENAME
        if not path.exists():
            return

    actual_digest = artifact_sha256(path)
    expected_digest = provenance.get("run_intent_digest")
    if actual_digest != expected_digest:
        raise ValueError(
            f"provenance.run_intent_digest does not match the actual "
            f"content hash of {path} (expected {expected_digest!r}, got "
            f"{actual_digest!r}) -- the run_intent.json record appears to "
            "have been tampered with after the digest was computed"
        )

    repo_root = _find_repo_root_with_subrepos_lock(path.parent)
    if repo_root is None:
        # No discoverable subrepos.lock.json nearby -- skip the deeper
        # code-pin/producer check. The tamper check above already ran, and
        # the artifact_digest binding is authoritative regardless.
        return

    errors = canonical_registry.verify_canonical_run_intent(path, repo_root=repo_root)
    if errors:
        raise ValueError(
            f"canonical run-intent record at {path} failed verification: {errors}"
        )


def _find_experiment_classification_marker(start: Path) -> Path | None:
    """Walk upward from ``start`` looking for a real
    ``_experiment_classification.json`` marker -- the SAME atomic,
    producer-written record :func:`reject_exploratory_promotion` already
    trusts for ``kind="experiment"``. Bounded by
    :data:`_MAX_MARKER_SEARCH_LEVELS`.
    """
    current = start.resolve()
    for _ in range(_MAX_MARKER_SEARCH_LEVELS):
        candidate = current / CLASSIFICATION_FILENAME
        if candidate.exists():
            return candidate
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


def _verify_none_provenance(manifest: dict[str, Any]) -> None:
    """Verify a ``kind="none"`` declaration is not hiding a registered
    experiment's classified output.

    Codex review 2026-07-14 (this PR's round-3 follow-up, quoted in full in
    :func:`verify_artifact_provenance`'s docstring): "provenance.kind=
    'none' remains a direct bypass of the experiment gate... an artifact
    built from a registered experiment can set {"kind": "none"} and
    verify_artifact_provenance() returns immediately."

    ``kind="none"`` carries no caller-supplied ``dir`` (unlike
    ``kind="experiment"``) -- there is no argument for a dishonest caller
    to lie about directly. Instead, this derives the check target from the
    SAME real, on-disk identity fields (``local_artifact_path`` /
    ``artifact_path`` / a ``file://`` ``uri``) a producer already has to set
    for the artifact to be locatable/consumable at all -- fields that exist
    for a completely independent, load-bearing reason, not invented for
    this check (see :data:`_LOCAL_ARTIFACT_PATH_KEYS`). If ANY of them
    resolves to a directory that itself (or a bounded ancestor) carries a
    real ``_experiment_classification.json`` marker, the "none" declaration
    is provably false regardless of what the caller wrote, and is rejected
    by calling :func:`reject_exploratory_promotion` UNCONDITIONALLY on that
    directory -- reusing the exact self-report/registration logic already
    built for ``kind="experiment"`` rather than re-implementing it a second
    time (the same triple-impl-avoidance idiom this module already
    documents).

    Residual, honestly-disclosed limit (same status as this module's other
    self-declared fields, e.g. ``code_commit``): an artifact whose ONLY
    identity is an opaque ``store://``/``object://`` URI with no
    locally-resolvable path at all gives this check nothing on disk to
    inspect, so it passes -- there is no local filesystem truth to
    consult in that case. This is materially narrower than the prior gap
    (which accepted ANY ``kind="none"`` declaration unconditionally,
    including one built directly from a real, locally-visible experiment
    output directory) and is exactly the scenario the follow-up review's
    required negative test constructs.
    """
    for candidate_dir in _candidate_local_artifact_dirs(manifest):
        marker = _find_experiment_classification_marker(candidate_dir)
        if marker is None:
            continue
        try:
            reject_exploratory_promotion(marker.parent)
        except ValueError as exc:
            raise ValueError(
                "artifact manifest declares provenance.kind='none' but its "
                f"own artifact path ({candidate_dir}) resolves under a real "
                f"EXPLORATORY_ONLY classification record ({marker}) -- a "
                "'none' declaration is not honest here (Codex review "
                "2026-07-14: 'provenance.kind=\"none\" remains a direct "
                f"bypass of the experiment gate'): {exc}"
            ) from exc


def verify_artifact_provenance(
    manifest: dict[str, Any],
    *,
    canonical_publications_dir: Path | str | None = None,
) -> None:
    """Require and verify a candidate artifact manifest's lineage record.

    ``canonical_publications_dir`` optionally overrides where the
    authoritative canonical publication store lives (defaults to this
    repo's own ``registry/canonical_publications/`` via
    :func:`renquant_artifacts.canonical_registry.default_canonical_publications_dir`).
    It is validation-infrastructure configuration supplied by the trusted
    calling code -- NEVER read from the candidate manifest itself -- and an
    unresolvable store fails closed for ``promotion_status="prod"``.

    This is the F-7 promotion-boundary fix, now covering BOTH of Codex's
    2026-07-14 findings on this same PR:

    * Round 2 (already fixed below): "The promotion guard is bypassable
      because provenance is optional and self-declared... no schema,
      registry, or publication path requires an immutable relation between
      the artifact and the producing run."
    * Round 3 (fixed by this function taking the FULL manifest, not just
      the ``provenance`` sub-dict, and by :func:`_verify_none_provenance`):
      "provenance.kind=\"none\" remains a direct bypass of the experiment
      gate. The required field closes omission, but it does not establish
      producer-written lineage: an artifact built from a registered
      experiment can set {\"kind\": \"none\"} and
      verify_artifact_provenance() returns immediately."

    ``provenance`` is a REQUIRED field on every candidate artifact
    manifest -- there is no code path where a manifest validates
    successfully without an explicit lineage determination being made. It
    must be a dict with a ``"kind"`` key drawn from :data:`PROVENANCE_KINDS`:

    * ``"none"`` -- see :data:`PROVENANCE_KINDS` docstring for the narrow,
      explicit, auditable allowlist this represents. No longer an
      unconditional pass: :func:`_verify_none_provenance` independently
      checks the manifest's own real, on-disk identity fields for a nearby
      EXPLORATORY_ONLY classification marker and rejects the declaration if
      one is found (see that function's docstring for the full contract
      and its honestly-disclosed residual limit). F-7 canonical follow-up:
      ``kind="none"`` can additionally never be combined with
      ``promotion_status="prod"`` -- a real production artifact must use
      ``kind="canonical"`` (a verified run-intent binding) instead, not the
      narrow non-production allowlist.
    * ``"canonical"`` -- requires ``run_intent_path``, ``run_intent_digest``,
      and ``artifact_digest`` (see :data:`PROVENANCE_CANONICAL_REQUIRED_KEYS`).
      ``artifact_digest`` is checked against ``manifest["fingerprint"]``
      UNCONDITIONALLY and FIRST: a dishonest manifest that copies a real
      ``run_intent_path``/``run_intent_digest`` from a genuine canonical run
      onto a DIFFERENT artifact fails here, because that different
      artifact's ``fingerprint`` won't match ``artifact_digest``. For
      ``promotion_status="prod"`` the boundary is
      :func:`_verify_canonical_publication_record`: the authoritative,
      producer-written publication record MUST resolve from the registry's
      own canonical publication store (keyed by the manifest's
      ``fingerprint``) and verify, regardless of whether ANY path is
      locally visible -- self-consistent manifest fields alone are never
      authority (Codex round-4 review, 2026-07-17). The round-5-style
      exploratory-marker check (:func:`_verify_canonical_not_exploratory`)
      and the strictly-supplemental local diagnostics
      (:func:`_verify_canonical_run_intent_if_resolvable`) also run; the
      latter can only ADD rejections and never gates the record check.
    * ``"experiment"`` -- requires ``dir`` (the run's output directory,
      expected to carry ``_experiment_classification.json``) and
      ``registry_index_path`` (the immutable, git-tracked manifest-registry
      index -- see :func:`verify_manifest_registered`). Verified via
      :func:`reject_exploratory_promotion`, which fails closed on a
      missing/unregistered/unverifiable marker rather than treating "no
      marker" as "not exploratory" -- see that function's docstring.

    Raises ``ValueError`` if ``provenance`` is absent, malformed, declares
    an unrecognized ``kind``, or fails verification for its kind.
    """
    if not isinstance(manifest, dict):
        raise ValueError(
            "verify_artifact_provenance requires the full candidate "
            "manifest dict (not just the 'provenance' sub-dict) -- the "
            "kind='none' check needs the manifest's OWN identity fields "
            "(local_artifact_path/artifact_path/uri) to verify against"
        )
    provenance = manifest.get("provenance")
    if not isinstance(provenance, dict) or not provenance.get("kind"):
        raise ValueError(
            "artifact manifest is missing a required 'provenance' record "
            "(must be an object with a 'kind' key) -- provenance is no "
            "longer optional / self-declared-by-omission; every candidate "
            "manifest must resolve an explicit lineage determination before "
            "it can validate (Codex review 2026-07-14: 'the promotion "
            "guard is bypassable because provenance is optional and "
            "self-declared')"
        )
    kind = provenance["kind"]
    if kind not in PROVENANCE_KINDS:
        raise ValueError(
            f"artifact manifest provenance.kind must be one of "
            f"{sorted(PROVENANCE_KINDS)}, got {kind!r}"
        )
    if kind == "none":
        if manifest.get("promotion_status") == "prod":
            raise ValueError(
                "provenance.kind='none' cannot be combined with "
                "promotion_status='prod' -- a real production artifact must "
                "carry provenance.kind='canonical' (a registered, verified "
                "run-intent publication) instead"
            )
        _verify_none_provenance(manifest)
        return
    if kind == "canonical":
        missing = PROVENANCE_CANONICAL_REQUIRED_KEYS - provenance.keys()
        if missing:
            raise ValueError(
                f"artifact manifest provenance.kind='canonical' missing "
                f"required keys: {sorted(missing)}"
            )
        if provenance["artifact_digest"] != manifest.get("fingerprint"):
            raise ValueError(
                "provenance.artifact_digest does not match this manifest's "
                "own fingerprint -- a canonical provenance record for a "
                "DIFFERENT artifact cannot be attached to this one"
            )
        _verify_canonical_not_exploratory(manifest)
        if manifest.get("promotion_status") == "prod":
            # The round-4 promotion boundary: MANDATORY, fail-closed
            # resolution of the authoritative publication record from the
            # registry's own canonical publication store. Runs before --
            # and entirely independently of -- any local-path diagnostics
            # below; nothing about local file visibility gates it.
            _verify_canonical_publication_record(
                manifest, provenance, canonical_publications_dir,
            )
        _verify_canonical_run_intent_if_resolvable(provenance)
        return
    missing = PROVENANCE_EXPERIMENT_REQUIRED_KEYS - provenance.keys()
    if missing:
        raise ValueError(
            f"artifact manifest provenance.kind='experiment' missing "
            f"required keys: {sorted(missing)}"
        )
    reject_exploratory_promotion(
        provenance["dir"], registry_index_path=provenance["registry_index_path"],
    )


def build_experiment_provenance_reference(
    output_dir: Path | str, registry_index_path: Path | str,
) -> dict[str, str]:
    """Build the canonical ``provenance`` reference for an artifact manifest.

    Any code that constructs an artifact manifest from a
    ``run_sim_104.py``-style registered-experiment run's output MUST call
    this rather than hand-building the ``provenance`` dict, so there is
    exactly ONE implementation of the reference's shape -- mirroring the
    ``model_content_sha256`` triple-impl lesson this module's docstring
    already cites (three independently hand-copied implementations silently
    diverged; the fix was one shared impl, not audited copies). Called by
    the producer (e.g. ``run_sim_104.py``'s ``verify_and_classify_experiment``)
    right after the run's classification marker is written, using values it
    already resolved -- never accepted as arbitrary caller input.
    """
    return {
        "kind": "experiment",
        "dir": str(output_dir),
        "registry_index_path": str(registry_index_path),
    }
