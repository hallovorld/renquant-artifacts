"""Artifact-manifest validation pipeline."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from renquant_common import Job, Pipeline, Task


@dataclass
class ArtifactManifestContext:
    manifest: dict[str, Any]
    validation_report: dict[str, Any] = field(default_factory=dict)


class ValidateArtifactManifestTask(Task):
    def run(self, ctx: ArtifactManifestContext) -> bool | None:
        required = (
            "artifact_id",
            "model_family",
            "strategy",
            "fingerprint",
            "uri",
            "promotion_status",
            "metrics",
        )
        missing = [key for key in required if not ctx.manifest.get(key)]
        if missing:
            raise ValueError(f"artifact manifest missing required keys: {missing}")
        if ctx.manifest.get("draft") is True:
            raise ValueError(
                "artifact manifest is marked draft/placeholder "
                f"({ctx.manifest.get('artifact_id')!r}) and cannot be validated "
                "as a real artifact; no consumer may resolve, load, or promote it"
            )
        if ctx.manifest["promotion_status"] == "prod" and ctx.manifest["metrics"].get("accepted") is not True:
            raise ValueError("prod artifact must have accepted=true metrics")
        if ctx.manifest["uri"].startswith("/Users/"):
            raise ValueError("artifact uri must not be developer-local absolute path")
        ctx.validation_report = {
            "artifact_id": ctx.manifest["artifact_id"],
            "fingerprint": ctx.manifest["fingerprint"],
            "ok": True,
        }
        return True


class ArtifactManifestValidationJob(Job):
    @property
    def tasks(self) -> list[Task]:
        return [ValidateArtifactManifestTask()]


class ArtifactManifestValidationPipeline(Pipeline):
    def __init__(self) -> None:
        super().__init__([ArtifactManifestValidationJob()], name="artifact-manifest-validation")


def validate_artifact_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Validate an artifact manifest and return its audit report."""
    ctx = ArtifactManifestContext(manifest)
    ArtifactManifestValidationPipeline().run(ctx)
    return ctx.validation_report


def validate_triad_sidecar_contract(
    manifest: dict[str, Any],
    *,
    required: bool = False,
) -> dict[str, Any]:
    """Validate the leakage-triad sidecar surface for one artifact manifest.

    This preflight is intentionally explicit for the first retrofit PR: callers
    can audit current prod/shadow manifests without changing the default
    manifest gate until sidecars exist everywhere.
    """
    sidecar = manifest.get("triad_report") or manifest.get("leakage_triad")
    if sidecar is None:
        if required:
            raise ValueError("artifact manifest missing leakage triad sidecar")
        return {
            "ok": True,
            "required": False,
            "present": False,
            "warnings": ["leakage triad sidecar is absent"],
        }
    if not isinstance(sidecar, dict):
        raise ValueError("leakage triad sidecar must be an object")

    required_keys = ("candidate", "baseline", "shadow", "leakage_safe")
    missing = [key for key in required_keys if key not in sidecar]
    if missing:
        raise ValueError(f"leakage triad sidecar missing required keys: {missing}")
    if sidecar.get("leakage_safe") is not True:
        raise ValueError("leakage triad sidecar must declare leakage_safe=true")

    roles = {
        key: (sidecar.get(key) or {}).get("role")
        for key in ("candidate", "baseline", "shadow")
        if isinstance(sidecar.get(key), dict)
    }
    for expected in ("candidate", "baseline", "shadow"):
        if roles.get(expected) != expected:
            raise ValueError(f"leakage triad {expected} role must be {expected!r}")

    return {
        "ok": True,
        "required": bool(required),
        "present": True,
        "roles": roles,
    }


_CRYPTO_PROMOTION_STATUSES = ("diagnostic", "shadow", "prod")

# Legal single-step promotion edges. Skip-stage jumps (diagnostic -> prod),
# staying put (X -> X), and moving backwards are all illegal.
_CRYPTO_PROMOTION_TRANSITIONS = {
    ("diagnostic", "shadow"),
    ("shadow", "prod"),
}


def validate_crypto_promotion_contract(
    manifest: dict[str, Any],
    *,
    current_status: str | None = None,
    target_status: str = "shadow",
) -> dict[str, Any]:
    """Validate crypto-specific promotion requirements.

    Crypto artifacts must pass the stage-0 paper battery before promotion
    beyond diagnostic.  Shadow and prod require progressively stricter
    evidence.

    Callers must pass an explicit ``current_status`` (the promotion stage the
    artifact is being promoted *from*). It is cross-checked against the
    manifest's own ``promotion_status`` field (a mismatch is rejected -- the
    caller's belief about where the artifact is must agree with the record),
    and the ``(current_status, target_status)`` pair must be a legal single
    step in the ``diagnostic -> shadow -> prod`` ladder. This closes a gap
    where a caller could request ``target_status="prod"`` directly against a
    still-``diagnostic`` manifest and have it evaluated only against the prod
    metrics gate, silently skipping the shadow stage.

    KNOWN LIMITATION (Codex CHANGES_REQUESTED #23, finding 1, not fixed by
    this function): ``paper_battery_pass``, ``accepted``, and ``shadow_days``
    below are self-attested, hand-editable manifest fields. This function
    does **not** verify them against any real evidence artifact -- there is
    no check against a Stage-0 paper-readiness record produced by the
    orchestrator, a model-evaluation/acceptance evidence artifact produced by
    renquant-model, or an actual timestamped shadow-observation history. A
    caller can hand-edit a manifest's metrics block and this function will
    accept it at face value. This is a known, currently *dormant* gap:
    grepping every sibling repo (orchestrator, execution, model, pipeline,
    strategy, backtesting) shows zero external callers of this function, and
    the orchestrator's crypto entry pipeline is independently hard-blocked by
    ``ENTRY_AUTHORIZATION_TRUST_ANCHOR_READY = False`` in
    ``crypto_session.py`` regardless of what this contract reports -- so this
    gap cannot currently authorize any live capital-risk action. Closing it
    for real requires a cross-repo, content-addressed evidence-binding design
    (digest/run_id references into orchestrator's Stage-0 readiness records,
    renquant-model's evaluation-acceptance evidence, and a shadow-observation
    evidence artifact with timestamps) -- a design decision reserved for a
    human operator to make deliberately, not something to invent unilaterally
    inside a mechanical bug-fix PR. Do not treat ``ok=True`` from this
    function as proof of real paper/shadow performance until that follow-up
    lands.
    """
    if manifest.get("asset_class") != "crypto":
        raise ValueError("manifest is not a crypto artifact")

    if manifest.get("draft") is True:
        raise ValueError(
            "crypto artifact manifest is marked draft/placeholder "
            f"({manifest.get('artifact_id')!r}) and cannot be promoted"
        )

    if current_status is None:
        raise ValueError(
            "validate_crypto_promotion_contract requires an explicit "
            "current_status (the promotion stage being promoted from); "
            "it must not be inferred implicitly"
        )
    if current_status not in _CRYPTO_PROMOTION_STATUSES:
        raise ValueError(
            f"unknown crypto promotion current_status {current_status!r}; "
            f"expected one of {_CRYPTO_PROMOTION_STATUSES}"
        )
    if target_status not in _CRYPTO_PROMOTION_STATUSES:
        raise ValueError(
            f"unknown crypto promotion target_status {target_status!r}; "
            f"expected one of {_CRYPTO_PROMOTION_STATUSES}"
        )

    manifest_status = manifest.get("promotion_status")
    if manifest_status != current_status:
        raise ValueError(
            "crypto promotion current_status mismatch: caller asserted "
            f"{current_status!r} but manifest promotion_status is "
            f"{manifest_status!r}"
        )

    if (current_status, target_status) not in _CRYPTO_PROMOTION_TRANSITIONS:
        raise ValueError(
            f"illegal promotion transition: {current_status} -> {target_status} "
            "(only diagnostic -> shadow and shadow -> prod are permitted; "
            "skip-stage jumps, same-status no-ops, and backwards transitions "
            "are all rejected)"
        )

    metrics = manifest.get("metrics") or {}
    errors: list[str] = []

    if target_status in ("shadow", "prod"):
        if metrics.get("paper_battery_pass") is not True:
            errors.append("stage-0 paper battery must pass before shadow promotion")

    if target_status == "prod":
        if metrics.get("accepted") is not True:
            errors.append("prod crypto artifact must have accepted=true")
        shadow_days = metrics.get("shadow_days")
        if not isinstance(shadow_days, (int, float)) or isinstance(
            shadow_days, bool
        ) or shadow_days <= 0:
            errors.append(
                "prod crypto artifact must report shadow_days as a positive number"
            )

    if errors:
        raise ValueError(
            f"crypto promotion to {target_status} blocked: {'; '.join(errors)}"
        )

    return {
        "ok": True,
        "asset_class": "crypto",
        "current_status": current_status,
        "target_status": target_status,
        "checks_passed": True,
    }
