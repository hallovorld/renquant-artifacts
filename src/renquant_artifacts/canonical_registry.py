"""Canonical run-intent record: the F-7 provenance binding for automated
daily-full canonical training runs (as opposed to the reviewed, registered
``experiment`` path :mod:`renquant_artifacts.experiment_registry` already
owns).

Background (F-7 design, ``doc/design/2026-07-16-f7-canonical-provenance.md``
in the umbrella repo -- summarized here so this module is self-explanatory):
Codex repeatedly found that ``provenance.kind`` on a candidate artifact
manifest is a caller-declared string with no independent verification --
``kind="none"`` in particular passes unconditionally for artifacts whose only
identity is an opaque ``store://``/``object://`` URI (no local path to scan a
marker against). Daily canonical training runs have no per-run reviewed PR
the way a registered experiment does (see
:mod:`renquant_artifacts.experiment_registry`'s module docstring, section
"why this can't reuse the experiment INDEX.json pattern verbatim"), so their
trust anchor is different: a canonical run-intent record, written atomically
by a narrow, code-reviewed producer entrypoint BEFORE training starts, whose
own evidence (code pins, config/data fingerprints) is independently
re-verifiable against the actual environment at validation time.

The binding to a specific artifact is a plain field comparison living in the
manifest's own JSON (``provenance.artifact_digest == manifest["fingerprint"]``,
enforced in :func:`renquant_artifacts.experiment_registry.verify_artifact_provenance`)
rather than a filesystem walk -- this is what makes the check work
identically for local and opaque (``store://``/``object://``-only) artifact
identities. This module supplies the run-intent record's schema and the two
supporting operations (write it atomically; verify it against the actual
environment); the digest-comparison boundary itself lives in
``experiment_registry.py`` alongside the rest of ``provenance.kind``
dispatch.

This module deliberately reuses :func:`renquant_artifacts.experiment_registry.verify_code_pin`
for code-pin verification rather than re-implementing git HEAD/dirty/remote
checks a second time -- see that module's own docstring for the
"triple-impl fingerprint" incident this idiom exists to avoid.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from renquant_common.model_fingerprint import artifact_sha256

from .experiment_registry import verify_code_pin

CANONICAL_RUN_INTENT_SCHEMA_VERSION = 1
CANONICAL_RUN_INTENT_FILENAME = "run_intent.json"

#: Required keys inside a canonical run-intent record, EXCLUDING
#: ``schema_version``/``kind`` themselves (those two are fixed structural
#: constants :func:`write_canonical_run_intent` always sets, not caller
#: input to validate).
CANONICAL_RUN_INTENT_REQUIRED_KEYS = frozenset(
    {
        "run_id", "run_type", "created_at", "producer", "workflow_class",
        "strategy_manifest_fingerprint", "data_manifest_fingerprint",
        "strategy_config_digest", "model_config_digest",
        "calendar_universe_digest", "as_of", "code_pins",
    }
)

#: Which subrepo (per ``subrepos.lock.json`` ``name``, and the key under
#: ``code_pins`` in the run-intent record itself) each of the 3 required
#: canonical code-pin categories corresponds to. Generalizes
#: :data:`renquant_artifacts.experiment_registry.CODE_PIN_SUBREPOS`'s 2
#: categories (strategy/pipeline) with a 3rd: the model-training code itself,
#: since a canonical run is a training run in a way a registered experiment's
#: sim/backfill run is not.
CANONICAL_CODE_PIN_SUBREPOS: dict[str, str] = {
    "strategy_config": "renquant-strategy-104",
    "pipeline_version": "renquant-pipeline",
    "model_code": "renquant-model",
}

#: Narrow, explicit allowlist of ``(repo, entrypoint)`` producers permitted
#: to have written a canonical run-intent record -- the same idiom
#: :data:`renquant_artifacts.experiment_registry.PROVENANCE_KINDS` already
#: documents for ``provenance.kind``. A run-intent record naming any other
#: producer is treated as unverifiable, not as "some other legitimate
#: caller we haven't heard of yet".
CANONICAL_PRODUCERS = frozenset(
    {
        ("renquant-orchestrator", "daily.TrainGbdtArtifactTask"),
    }
)

#: Bound on how far up the directory tree callers may search for
#: ``subrepos.lock.json`` when auto-deriving a ``repo_root`` for a
#: supplemental local verification -- mirrors
#: ``experiment_registry._MAX_MARKER_SEARCH_LEVELS``'s bounded-walk idiom so
#: no caller can trigger a pathological/unbounded scan.
MAX_REPO_ROOT_SEARCH_LEVELS = 6

#: The authoritative canonical publication store (Codex round-4 review on
#: renquant-artifacts#24, 2026-07-17: "Persist an authoritative canonical
#: run-intent/publication record in the artifact registry ... keyed by
#: run_intent_digest and artifact digest/immutable URI"). It lives INSIDE
#: the artifact registry (``registry/canonical_publications/`` in this
#: repo), so publication is a deliberate, review-gated act (a commit adding
#: the record + index entry -- the same immutability model as the
#: experiment-manifest ``INDEX.json``), never a runtime filesystem accident:
#:
#: * ``INDEX.json`` maps ``artifact_digest`` (the manifest's own
#:   ``fingerprint``) -> ``{"run_intent_digest", "record", "artifact_uri",
#:   "registered_at"}``.
#: * ``<run_intent_digest hex>.json`` is the byte-verbatim, content-addressed
#:   persisted copy of the producing run's ``run_intent.json``: its filename
#:   and index entry are its own sha256, so any post-publication edit is
#:   detectable by recomputation alone, with no local build-machine path
#:   involved.
CANONICAL_PUBLICATIONS_DIRNAME = "canonical_publications"
CANONICAL_PUBLICATIONS_INDEX_FILENAME = "INDEX.json"


def default_canonical_publications_dir() -> Path | None:
    """The in-repo default location of the canonical publication store:
    ``<repo-root>/registry/canonical_publications``, derived from this
    package's own location (``src/renquant_artifacts/`` -> repo root).

    Returns ``None`` when no ``registry/`` directory exists next to the
    package (e.g. an installed wheel with no repo checkout). Callers on the
    ``promotion_status="prod"`` path MUST treat ``None`` -- and a missing/
    empty store at the returned path -- as fail-closed rejection, never as
    "nothing to check": a promotion boundary cannot make the authoritative
    record optional (Codex round-4 review on renquant-artifacts#24).
    """
    repo_root = Path(__file__).resolve().parent.parent.parent
    registry_dir = repo_root / "registry"
    if not registry_dir.is_dir():
        return None
    return registry_dir / CANONICAL_PUBLICATIONS_DIRNAME


def _verify_run_intent_intrinsic(record: Any, source: str) -> list[str]:
    """Structural + allowlist verification of a run-intent record's OWN
    content -- everything that can (and therefore must) be checked without
    any local environment: shape, schema kind, required keys, producer
    allowlist, code-pin entry shape, and non-empty evidence fields.

    Shared by :func:`verify_canonical_run_intent` (which layers the
    environment code-pin checks on top) and
    :func:`resolve_canonical_publication` (the validation-time resolver,
    which by design runs with NO local environment guarantees) -- one
    implementation, per this package's triple-impl-avoidance idiom.
    """
    if not isinstance(record, dict):
        return [f"run-intent record from {source} is not a JSON object"]

    errors: list[str] = []
    if record.get("kind") != "canonical-run-intent":
        errors.append(
            f"run-intent record from {source} has kind={record.get('kind')!r}, "
            "expected 'canonical-run-intent'"
        )
    missing = CANONICAL_RUN_INTENT_REQUIRED_KEYS - record.keys()
    if missing:
        errors.append(
            f"run-intent record from {source} missing required keys: {sorted(missing)}"
        )
        return errors

    producer = record.get("producer")
    producer_tuple = (
        (producer.get("repo"), producer.get("entrypoint"))
        if isinstance(producer, dict)
        else None
    )
    if producer_tuple not in CANONICAL_PRODUCERS:
        errors.append(
            f"run-intent record producer {producer!r} is not in the "
            f"CANONICAL_PRODUCERS allowlist {sorted(CANONICAL_PRODUCERS)}"
        )

    code_pins = record.get("code_pins")
    if not isinstance(code_pins, dict):
        errors.append("run-intent record code_pins must be an object")
    else:
        for category, subrepo_name in CANONICAL_CODE_PIN_SUBREPOS.items():
            pin_entry = code_pins.get(subrepo_name)
            if (
                not isinstance(pin_entry, dict)
                or not str(pin_entry.get("commit", "")).strip()
                or not str(pin_entry.get("remote", "")).strip()
            ):
                errors.append(
                    f"code_pins.{subrepo_name} ({category}): missing or malformed "
                    "pin entry (commit + remote required)"
                )

    for evidence_key in (
        "run_id", "strategy_manifest_fingerprint", "data_manifest_fingerprint",
        "strategy_config_digest", "model_config_digest",
        "calendar_universe_digest", "as_of",
    ):
        value = record.get(evidence_key)
        if not isinstance(value, str) or not value.strip():
            errors.append(
                f"run-intent record {evidence_key} must be a non-empty string, "
                f"got {value!r}"
            )
    return errors


def write_canonical_run_intent(
    output_dir: Path | str,
    *,
    run_id: str,
    run_type: str,
    producer: dict[str, str],
    strategy_manifest_fingerprint: str,
    data_manifest_fingerprint: str,
    strategy_config_digest: str,
    model_config_digest: str,
    calendar_universe_digest: str,
    as_of: str,
    code_pins: dict[str, dict[str, str]],
) -> Path:
    """Atomically write the pre-training canonical run-intent record.

    Written BEFORE ``TrainingContext`` is constructed (the orchestrator's
    job, not this module's -- this only supplies the write primitive), using
    write-to-tmp-then-rename so a crash mid-run never leaves a partially
    written record -- mirrors
    :func:`renquant_artifacts.experiment_registry.write_experiment_classification`'s
    atomic-write idiom exactly.

    ``created_at`` is stamped here (UTC, second-resolution ISO-8601 with a
    literal ``Z`` suffix) rather than accepted as a caller argument, so every
    canonical run-intent record's timestamp reflects the moment it was
    actually written, not a value a caller could backdate/forge.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    run_intent: dict[str, Any] = {
        "schema_version": CANONICAL_RUN_INTENT_SCHEMA_VERSION,
        "kind": "canonical-run-intent",
        "run_id": run_id,
        "run_type": run_type,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "producer": producer,
        "workflow_class": "canonical",
        "strategy_manifest_fingerprint": strategy_manifest_fingerprint,
        "data_manifest_fingerprint": data_manifest_fingerprint,
        "strategy_config_digest": strategy_config_digest,
        "model_config_digest": model_config_digest,
        "calendar_universe_digest": calendar_universe_digest,
        "as_of": as_of,
        "code_pins": code_pins,
    }
    out = output_dir / CANONICAL_RUN_INTENT_FILENAME
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(run_intent, indent=2) + "\n")
    tmp.rename(out)
    return out


def verify_canonical_run_intent(
    run_intent_path: Path | str,
    *,
    repo_root: Path | str,
    subrepos_lock: dict[str, Any] | None = None,
) -> list[str]:
    """Verify a canonical run-intent record against the ACTUAL environment.

    Fails closed (non-empty return, mirroring
    :func:`renquant_artifacts.experiment_registry.verify_experiment_pins`'s
    contract rather than raising): a missing/unparsable
    ``run_intent.json``, missing required keys, a ``producer`` not in
    :data:`CANONICAL_PRODUCERS`, or any of the 3 :data:`CANONICAL_CODE_PIN_SUBREPOS`
    code pins failing :func:`renquant_artifacts.experiment_registry.verify_code_pin`
    against the actual current checkout (or not matching
    ``subrepos.lock.json``'s own current pin) all produce one or more error
    strings rather than silently passing.
    """
    run_intent_path = Path(run_intent_path)
    if not run_intent_path.exists():
        return [f"run_intent.json not found at {run_intent_path}"]
    try:
        run_intent = json.loads(run_intent_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        return [f"run_intent.json at {run_intent_path} is unparsable: {exc}"]
    if not isinstance(run_intent, dict):
        return [f"run_intent.json at {run_intent_path} is not a JSON object"]

    errors = _verify_run_intent_intrinsic(run_intent, str(run_intent_path))
    if any("missing required keys" in e or "is not a JSON object" in e for e in errors):
        return errors

    repo_root = Path(repo_root)
    if subrepos_lock is None:
        lock_path = repo_root / "subrepos.lock.json"
        if not lock_path.exists():
            errors.append(f"subrepos.lock.json not found at {lock_path} -- cannot verify code pins")
            return errors
        subrepos_lock = json.loads(lock_path.read_text())

    lock_by_name = {
        entry.get("name"): entry for entry in subrepos_lock.get("subrepos", [])
    }
    code_pins = run_intent.get("code_pins")
    if not isinstance(code_pins, dict):
        errors.append("run_intent.json code_pins must be an object")
        return errors

    for category, subrepo_name in CANONICAL_CODE_PIN_SUBREPOS.items():
        pin_entry = code_pins.get(subrepo_name)
        if not isinstance(pin_entry, dict):
            errors.append(
                f"code_pins.{subrepo_name} ({category}): missing or malformed pin entry"
            )
            continue

        lock_entry = lock_by_name.get(subrepo_name)
        if lock_entry is None:
            errors.append(f"code_pins.{subrepo_name}: not found in subrepos.lock.json")
            continue

        expected_commit = str(pin_entry.get("commit", ""))
        expected_remote = str(pin_entry.get("remote", ""))
        local_path = Path(lock_entry.get("local_path", ""))
        if not local_path.is_absolute():
            local_path = (repo_root / local_path).resolve()
        pin_errors = verify_code_pin(local_path, expected_commit, expected_remote)
        errors.extend(f"code_pins.{subrepo_name}: {e}" for e in pin_errors)

        # As with verify_experiment_pins: the declared pin must ALSO match
        # the lock's own CURRENT pin, not only whatever checkout someone
        # happens to be sitting on locally.
        locked_commit = lock_entry.get("commit", "")
        if locked_commit and expected_commit != locked_commit:
            errors.append(
                f"code_pins.{subrepo_name}: declared {expected_commit[:12]} does not "
                f"match subrepos.lock.json pin {locked_commit[:12]}"
            )

    return errors


def build_canonical_provenance_reference(
    run_intent_path: Path | str, artifact_digest: str,
) -> dict[str, str]:
    """Build the canonical ``provenance`` reference for an artifact manifest.

    Mirrors :func:`renquant_artifacts.experiment_registry.build_experiment_provenance_reference`:
    any code that constructs an artifact manifest from a canonical
    daily-full training run MUST call this rather than hand-building the
    ``provenance`` dict, so there is exactly ONE implementation of the
    reference's shape. ``run_intent_digest`` is computed HERE, at call time,
    via :func:`renquant_common.model_fingerprint.artifact_sha256` over the
    actual ``run_intent.json`` bytes -- never accepted as arbitrary caller
    input -- so it always reflects the real, already-written record.
    """
    run_intent_path = Path(run_intent_path)
    return {
        "kind": "canonical",
        "run_intent_path": str(run_intent_path),
        "run_intent_digest": artifact_sha256(run_intent_path),
        "artifact_digest": artifact_digest,
    }


# ---------------------------------------------------------------------------
# Canonical publication store (Codex round-4 review, renquant-artifacts#24)
# ---------------------------------------------------------------------------


def _record_filename(run_intent_digest: str) -> str:
    """Content-addressed record filename for a ``sha256:<hex>`` digest."""
    return run_intent_digest.split(":", 1)[-1] + ".json"


def register_canonical_publication(
    publications_dir: Path | str,
    *,
    run_intent_path: Path | str,
    artifact_digest: str,
    artifact_uri: str,
    repo_root: Path | str | None = None,
) -> Path:
    """Publisher-side write of the authoritative canonical publication record.

    Called by the trusted publication workflow (NOT by manifest consumers,
    and never driven by fields read from a candidate manifest): it derives
    everything from the producer's own already-written ``run_intent.json`` --
    the record's digest is recomputed here from the actual bytes, never
    accepted as caller input -- and persists two things into the store:

    * a byte-verbatim, content-addressed copy of ``run_intent.json`` at
      ``<run_intent_digest hex>.json``;
    * an ``INDEX.json`` entry keyed by ``artifact_digest`` binding that
      artifact to this ``run_intent_digest`` + immutable ``artifact_uri``.

    Fail-closed publisher gates:

    * The run-intent record must pass
      :func:`_verify_run_intent_intrinsic` (schema/producer-allowlist/pin
      shape/evidence fields) BEFORE anything is persisted -- the store never
      accepts an unverifiable record.
    * When ``repo_root`` is supplied (the real producer machine has its
      checkouts available), the FULL :func:`verify_canonical_run_intent`
      environment check (all 3 code pins vs the actual checkouts +
      ``subrepos.lock.json``) must also pass.
    * The store is append-only per ``artifact_digest``: re-registering the
      same artifact with the same record bytes is an idempotent no-op, but
      re-registering it against a DIFFERENT run-intent digest raises -- a
      publication may never be silently replaced (that would be exactly the
      mutable-record laundering the round-4 review rejects).

    Both file writes use the same write-to-tmp-then-rename idiom as
    :func:`write_canonical_run_intent`.
    """
    publications_dir = Path(publications_dir)
    run_intent_path = Path(run_intent_path)

    if not run_intent_path.exists():
        raise ValueError(f"cannot publish: run_intent.json not found at {run_intent_path}")
    raw_bytes = run_intent_path.read_bytes()
    try:
        record = json.loads(raw_bytes)
    except json.JSONDecodeError as exc:
        raise ValueError(f"cannot publish: {run_intent_path} is unparsable: {exc}") from exc

    intrinsic_errors = _verify_run_intent_intrinsic(record, str(run_intent_path))
    if intrinsic_errors:
        raise ValueError(
            "cannot publish canonical run-intent record: it failed intrinsic "
            f"verification (the store never accepts an unverifiable record): "
            f"{intrinsic_errors}"
        )
    if repo_root is not None:
        env_errors = verify_canonical_run_intent(run_intent_path, repo_root=repo_root)
        if env_errors:
            raise ValueError(
                "cannot publish canonical run-intent record: environment "
                f"verification failed: {env_errors}"
            )

    run_intent_digest = artifact_sha256(run_intent_path)
    record_name = _record_filename(run_intent_digest)
    publications_dir.mkdir(parents=True, exist_ok=True)

    record_path = publications_dir / record_name
    if record_path.exists():
        if artifact_sha256(record_path) != run_intent_digest:
            raise ValueError(
                f"canonical publication store corruption: {record_path} exists "
                "but its content does not match its own content-addressed name"
            )
    else:
        tmp = record_path.with_suffix(".json.tmp")
        tmp.write_bytes(raw_bytes)
        tmp.rename(record_path)

    index_path = publications_dir / CANONICAL_PUBLICATIONS_INDEX_FILENAME
    index: dict[str, Any] = (
        json.loads(index_path.read_text()) if index_path.exists() else {}
    )
    existing = index.get(artifact_digest)
    if existing is not None:
        if existing.get("run_intent_digest") != run_intent_digest:
            raise ValueError(
                f"canonical publication for artifact {artifact_digest} already "
                f"exists bound to {existing.get('run_intent_digest')} -- a "
                "publication is append-only and may never be rebound to a "
                f"different run-intent record ({run_intent_digest})"
            )
        return index_path  # idempotent re-registration of the identical binding

    index[artifact_digest] = {
        "run_intent_digest": run_intent_digest,
        "record": record_name,
        "artifact_uri": artifact_uri,
        "registered_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    tmp = index_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(index, indent=2, sort_keys=True) + "\n")
    tmp.rename(index_path)
    return index_path


def resolve_canonical_publication(
    artifact_digest: str,
    publications_dir: Path | str | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, list[str]]:
    """Validation-side resolution of the authoritative publication record.

    Returns ``(index_entry, run_intent_record, errors)``. ``errors`` is
    non-empty (and both records are ``None``) whenever the authoritative
    binding cannot be POSITIVELY established -- every one of the round-4
    review's rejection conditions maps to an explicit error here:

    * no store location at all (``publications_dir is None``), or the store/
      ``INDEX.json`` missing or unparsable  -> "absent";
    * no index entry for ``artifact_digest``, or an entry without a usable
      ``run_intent_digest``/``record``                      -> "absent";
    * the content-addressed record file missing, or its RECOMPUTED sha256
      not equal to the indexed ``run_intent_digest``    -> "mismatched"
      (a post-publication edit of the persisted record is detected by
      recomputation alone -- no local build-machine path is involved);
    * the persisted record failing
      :func:`_verify_run_intent_intrinsic` (schema / producer allowlist /
      pin shape / evidence fields)                       -> "does not verify".

    This function deliberately performs NO environment (git-checkout) I/O:
    it must behave identically on the producer machine, CI, and a pure
    registry/runtime validator -- local file visibility is never the
    condition that decides whether canonical evidence is checked (Codex
    round-4, requirement 4). Environment re-verification is layered
    separately where checkouts exist (:func:`verify_canonical_run_intent`
    at publication time via ``register_canonical_publication(repo_root=...)``,
    and the supplemental local diagnostics at validation time).
    """
    if publications_dir is None:
        return None, None, [
            "no canonical publication store is resolvable (no registry/ "
            "directory next to this package and no explicit "
            "canonical_publications_dir was provided)"
        ]
    publications_dir = Path(publications_dir)
    index_path = publications_dir / CANONICAL_PUBLICATIONS_INDEX_FILENAME
    if not index_path.exists():
        return None, None, [
            f"canonical publication index not found: {index_path}"
        ]
    try:
        index = json.loads(index_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        return None, None, [f"canonical publication index at {index_path} is unreadable: {exc}"]
    if not isinstance(index, dict):
        return None, None, [f"canonical publication index at {index_path} is not a JSON object"]

    entry = index.get(artifact_digest)
    if not isinstance(entry, dict):
        return None, None, [
            f"no canonical publication record is registered for artifact "
            f"digest {artifact_digest} in {index_path}"
        ]
    run_intent_digest = entry.get("run_intent_digest")
    record_name = entry.get("record")
    if not run_intent_digest or not record_name:
        return None, None, [
            f"canonical publication entry for {artifact_digest} has no "
            "run_intent_digest/record binding"
        ]

    record_path = publications_dir / record_name
    if not record_path.exists():
        return None, None, [
            f"canonical publication record file missing: {record_path}"
        ]
    actual_digest = artifact_sha256(record_path)
    if actual_digest != run_intent_digest:
        return None, None, [
            f"canonical publication record {record_path} does not match its "
            f"registered run_intent_digest (registered {run_intent_digest}, "
            f"recomputed {actual_digest}) -- the persisted record appears to "
            "have been modified after publication"
        ]
    try:
        record = json.loads(record_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        return None, None, [f"canonical publication record {record_path} is unreadable: {exc}"]

    intrinsic_errors = _verify_run_intent_intrinsic(record, str(record_path))
    if intrinsic_errors:
        return None, None, [
            "persisted canonical run-intent record failed verification: "
            f"{intrinsic_errors}"
        ]
    return entry, record, []
