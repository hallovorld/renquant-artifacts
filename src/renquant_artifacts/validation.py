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
