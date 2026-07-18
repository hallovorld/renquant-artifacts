"""Artifact-manifest validation pipeline."""
from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from typing import Any

from renquant_common import Job, Pipeline, Task

from .experiment_registry import provenance_required, verify_artifact_provenance
from .canonical_registry import CanonicalPublicationSnapshot

#: FutureWarning emitted (instead of raising) when a manifest with NO
#: ``provenance`` key validates through the standard funnel while the F-7
#: enforcement window is still open -- see
#: ``experiment_registry.PROVENANCE_REQUIRED_AFTER`` for the window contract.
_MISSING_PROVENANCE_WARNING = (
    "artifact manifest has no 'provenance' record. The F-7 provenance "
    "contract (renquant-artifacts#24) makes this REQUIRED once the "
    "sequenced consumer migrations land -- renquant-model#55 and "
    "renquant-orchestrator#518 are the migrations that close this "
    "tolerance window by stamping/threading provenance on every consumer "
    "manifest. Until then a provenance-less (pre-F-7) manifest validates "
    "with this warning only; on/after PROVENANCE_REQUIRED_AFTER "
    "(2026-08-15), or earlier with RQ_REQUIRE_PROVENANCE=1 / "
    "require_provenance=True, it fails closed with ValueError. Manifests "
    "that DO carry a provenance record are always fully verified, "
    "window or no window."
)


@dataclass
class ArtifactManifestContext:
    manifest: dict[str, Any]
    validation_report: dict[str, Any] = field(default_factory=dict)
    #: Exact, clean registry checkout pinned by the trusted validating caller;
    #: never inferred from the manifest or an ambient writable directory.
    canonical_publication_snapshot: CanonicalPublicationSnapshot | None = None
    #: Trusted-caller opt-in that closes the F-7 provenance tolerance
    #: window for THIS validation regardless of date/environment. It can
    #: only strengthen enforcement, never weaken it.
    require_provenance: bool = False


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

        # F-7 promotion-boundary enforcement (Codex review 2026-07-14 on
        # RenQuant#471 / renquant-artifacts#24). Round 1 wired
        # reject_exploratory_promotion() in here conditionally on a
        # caller-supplied ``provenance_dir`` string -- Codex's follow-up
        # review correctly flagged that as still bypassable: "provenance is
        # optional and self-declared... An experiment-derived result can
        # therefore be promoted simply by omitting provenance_dir." A local
        # filesystem path is also not durable provenance for a registry
        # artifact on its own.
        #
        # provenance is now a REQUIRED, typed lineage record (see
        # verify_artifact_provenance / PROVENANCE_KINDS) resolved
        # deterministically for EVERY candidate manifest -- there is no
        # longer a code path where a manifest validates successfully
        # without an explicit provenance/exploratory-status determination
        # being made. Every real caller across the multirepo funnels
        # through this one function --
        # renquant_pipeline.inference.ValidateRuntimeInputsTask (live/
        # shadow/sim runtime) and
        # renquant_artifacts.registry.{load,resolve}_artifact_manifest
        # (registry resolution) -- so wiring the check here makes the
        # EXPLORATORY_ONLY marker real enforcement instead of an inert log
        # line nothing consumes.
        #
        # Round-3 follow-up (Codex, same PR): "provenance.kind='none'
        # remains a direct bypass of the experiment gate... an artifact
        # built from a registered experiment can set {'kind': 'none'} and
        # verify_artifact_provenance() returns immediately." Fixed by
        # passing the FULL manifest (not just ctx.manifest["provenance"])
        # so kind="none" can be independently checked against the
        # manifest's own on-disk identity fields -- see
        # verify_artifact_provenance / _verify_none_provenance.
        #
        # Round-4 follow-up (Codex, 2026-07-17): for
        # promotion_status='prod' + kind='canonical', the authoritative,
        # producer-written publication record must resolve from the
        # registry's canonical publication store and verify -- local file
        # visibility never decides whether canonical evidence is checked.
        # See _verify_canonical_publication_record.
        #
        # Enforcement-window follow-up (2026-07-18): #24's own review
        # ordering sequenced the consumer migrations (renquant-model#55,
        # renquant-orchestrator#518) AFTER this contract change, but the
        # required-provenance raise landed unconditionally -- a flag-day
        # break of every consumer repo's CI on any fresh run (backtesting
        # 2 tests, model 4, orchestrator 26). The requirement is therefore
        # GOVERNED here, mirroring the umbrella resolver's
        # ARTIFACT_DIGEST_REQUIRED_AFTER precedent: while the window is
        # open (see experiment_registry.PROVENANCE_REQUIRED_AFTER /
        # provenance_required), a manifest with NO provenance key at all
        # -- the pre-F-7 legacy shape -- validates with a FutureWarning
        # instead of raising. Everything else stays strict: a PRESENT
        # provenance record (even a malformed one) is always fully
        # verified, verify_artifact_provenance itself is unchanged for
        # direct callers, and the #24 canonical-publication paths keep
        # unconditional enforcement (new surfaces, no legacy callers).
        if (
            "provenance" in ctx.manifest
            or ctx.require_provenance
            or provenance_required()
        ):
            verify_artifact_provenance(
                ctx.manifest,
                canonical_publication_snapshot=ctx.canonical_publication_snapshot,
            )
        else:
            warnings.warn(_MISSING_PROVENANCE_WARNING, FutureWarning, stacklevel=2)

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


def validate_artifact_manifest(
    manifest: dict[str, Any],
    *,
    canonical_publication_snapshot: CanonicalPublicationSnapshot | None = None,
    require_provenance: bool = False,
) -> dict[str, Any]:
    """Validate an artifact manifest and return its audit report.

    ``require_provenance=True`` closes the F-7 provenance tolerance window
    for this call (see :class:`ArtifactManifestContext`); it can only
    strengthen enforcement, never weaken it.
    """
    ctx = ArtifactManifestContext(
        manifest,
        canonical_publication_snapshot=canonical_publication_snapshot,
        require_provenance=require_provenance,
    )
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


class EvidenceBoundPromotionNotImplementedError(NotImplementedError):
    """Raised by :func:`validate_crypto_promotion_contract` for every
    diagnostic -> shadow or shadow -> prod request.

    An earlier version of this function accepted three self-attested,
    hand-editable manifest metrics (``paper_battery_pass``, ``accepted``,
    ``shadow_days``) as proof of Stage-0/model/shadow readiness and
    returned ``ok=True``. Codex review 2026-07-13 (round 2) on
    artifacts#23 correctly rejected documenting that as a "dormant" known
    limitation: a function named ``validate_crypto_promotion_contract``
    that returns a successful-looking result is a reusable API whose name
    and return value advertise real authorization semantics, regardless
    of whether anything calls it today. A later caller could wire it into
    a real decision path without ever touching this file again.

    This function therefore refuses every shadow/prod request outright
    until a real, cross-repo, content-addressed evidence design exists:
      - a Stage-0 paper-readiness record (run_id, environment, verdict,
        report digest, producer identity) produced by
        orchestrator/execution,
      - a model-evaluation/acceptance evidence artifact (manifest digest,
        pre-registered metrics, decision) produced by renquant-model,
      - a timestamped shadow-observation coverage record (start/end,
        required-session coverage) produced by orchestrator.
    Once that design lands, this function should validate typed,
    content-addressed references to those artifacts -- never boolean/
    numeric fields hand-set on the manifest itself.
    """


def validate_crypto_promotion_contract(
    manifest: dict[str, Any],
    *,
    current_status: str | None = None,
    target_status: str = "shadow",
) -> dict[str, Any]:
    """Validate crypto promotion PRECONDITIONS -- draft/placeholder status,
    explicit current_status, and a legal transition-graph edge.

    Callers must pass an explicit ``current_status`` (the promotion stage the
    artifact is being promoted *from*). It is cross-checked against the
    manifest's own ``promotion_status`` field (a mismatch is rejected -- the
    caller's belief about where the artifact is must agree with the record),
    and the ``(current_status, target_status)`` pair must be a legal single
    step in the ``diagnostic -> shadow -> prod`` ladder.

    Every request that clears those preconditions -- i.e. every
    diagnostic -> shadow or shadow -> prod request -- unconditionally raises
    :class:`EvidenceBoundPromotionNotImplementedError`. This function does
    NOT inspect ``paper_battery_pass``, ``accepted``, or ``shadow_days`` at
    all: those are self-attested, hand-editable manifest fields with no
    binding to any real evidence artifact, and a function that accepted them
    at face value would be indistinguishable from real authorization to a
    future caller. See :class:`EvidenceBoundPromotionNotImplementedError`
    for what a real implementation requires.
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

    raise EvidenceBoundPromotionNotImplementedError(
        f"crypto promotion {current_status} -> {target_status} refused: "
        "evidence-bound promotion validation is not implemented. This "
        "function does not verify paper_battery_pass/accepted/shadow_days "
        "against any real evidence artifact -- do not treat any return "
        "value from this function as authorization for a shadow/prod "
        "promotion until a cross-repo evidence-artifact design lands "
        "(see EvidenceBoundPromotionNotImplementedError)."
    )
