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


def validate_crypto_promotion_contract(
    manifest: dict[str, Any],
    *,
    target_status: str = "shadow",
) -> dict[str, Any]:
    """Validate crypto-specific promotion requirements.

    Crypto artifacts must pass the stage-0 paper battery before promotion
    beyond diagnostic.  Shadow and prod require progressively stricter
    evidence.
    """
    if manifest.get("asset_class") != "crypto":
        raise ValueError("manifest is not a crypto artifact")

    if target_status not in _CRYPTO_PROMOTION_STATUSES:
        raise ValueError(
            f"unknown crypto promotion target_status {target_status!r}; "
            f"expected one of {_CRYPTO_PROMOTION_STATUSES}"
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
        "target_status": target_status,
        "checks_passed": True,
    }
