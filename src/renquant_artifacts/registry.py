"""Artifact registry and resolver pipeline."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from renquant_common import Job, Pipeline, Task

from .validation import validate_artifact_manifest


@dataclass
class ArtifactRegistryContext:
    """Mutable context for resolving one artifact manifest from a registry."""

    registry_dir: Path
    artifact_id: str | None = None
    strategy: str | None = None
    model_family: str | None = None
    promotion_status: str | None = None
    candidates: list[tuple[Path, dict[str, Any]]] = field(default_factory=list)
    selected_path: Path | None = None
    manifest: dict[str, Any] | None = None
    validation_report: dict[str, Any] = field(default_factory=dict)


class LoadArtifactRegistryTask(Task):
    def run(self, ctx: ArtifactRegistryContext) -> bool | None:
        if not ctx.registry_dir.exists():
            raise FileNotFoundError(f"artifact registry does not exist: {ctx.registry_dir}")
        if not ctx.registry_dir.is_dir():
            raise NotADirectoryError(f"artifact registry is not a directory: {ctx.registry_dir}")
        candidates: list[tuple[Path, dict[str, Any]]] = []
        for path in sorted(ctx.registry_dir.glob("*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            candidates.append((path, payload))
        if not candidates:
            raise ValueError(f"artifact registry has no JSON manifests: {ctx.registry_dir}")
        ctx.candidates = candidates
        return True


class SelectArtifactManifestTask(Task):
    def run(self, ctx: ArtifactRegistryContext) -> bool | None:
        matches = []
        for path, manifest in ctx.candidates:
            if ctx.artifact_id is not None and manifest.get("artifact_id") != ctx.artifact_id:
                continue
            if ctx.strategy is not None and manifest.get("strategy") != ctx.strategy:
                continue
            if ctx.model_family is not None and manifest.get("model_family") != ctx.model_family:
                continue
            if (
                ctx.promotion_status is not None
                and manifest.get("promotion_status") != ctx.promotion_status
            ):
                continue
            matches.append((path, manifest))
        if not matches:
            raise ValueError(
                "no artifact manifest matched "
                f"artifact_id={ctx.artifact_id!r} strategy={ctx.strategy!r} "
                f"model_family={ctx.model_family!r} promotion_status={ctx.promotion_status!r}"
            )
        if len(matches) > 1:
            names = [str(path.name) for path, _ in matches]
            raise ValueError(f"ambiguous artifact manifest selection: {names}")
        ctx.selected_path, ctx.manifest = matches[0]
        return True


class ValidateSelectedArtifactManifestTask(Task):
    def run(self, ctx: ArtifactRegistryContext) -> bool | None:
        if ctx.manifest is None:
            raise ValueError("manifest must be selected before validation")
        ctx.validation_report = validate_artifact_manifest(ctx.manifest)
        ctx.validation_report["path"] = str(ctx.selected_path)
        return True


class ArtifactManifestResolverJob(Job):
    @property
    def tasks(self) -> list[Task]:
        return [
            LoadArtifactRegistryTask(),
            SelectArtifactManifestTask(),
            ValidateSelectedArtifactManifestTask(),
        ]


class ArtifactManifestResolverPipeline(Pipeline):
    def __init__(self) -> None:
        super().__init__([ArtifactManifestResolverJob()], name="artifact-manifest-resolver")


def resolve_artifact_manifest(
    registry_dir: str | Path,
    *,
    artifact_id: str | None = None,
    strategy: str | None = None,
    model_family: str | None = None,
    promotion_status: str | None = None,
) -> dict[str, Any]:
    """Resolve and validate exactly one artifact manifest from a registry."""
    ctx = ArtifactRegistryContext(
        registry_dir=Path(registry_dir),
        artifact_id=artifact_id,
        strategy=strategy,
        model_family=model_family,
        promotion_status=promotion_status,
    )
    ArtifactManifestResolverPipeline().run(ctx)
    if ctx.manifest is None:
        raise ValueError("artifact manifest resolver finished without a manifest")
    return ctx.manifest


def load_artifact_manifest(path: str | Path) -> dict[str, Any]:
    """Load and validate a single artifact manifest file."""
    manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    validate_artifact_manifest(manifest)
    return manifest
