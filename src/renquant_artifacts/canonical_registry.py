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

    missing = CANONICAL_RUN_INTENT_REQUIRED_KEYS - run_intent.keys()
    if missing:
        return [f"run_intent.json missing required keys: {sorted(missing)}"]

    errors: list[str] = []

    producer = run_intent.get("producer")
    producer_tuple = (
        (producer.get("repo"), producer.get("entrypoint"))
        if isinstance(producer, dict)
        else None
    )
    if producer_tuple not in CANONICAL_PRODUCERS:
        errors.append(
            f"run_intent.json producer {producer!r} is not in the "
            f"CANONICAL_PRODUCERS allowlist {sorted(CANONICAL_PRODUCERS)}"
        )

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
