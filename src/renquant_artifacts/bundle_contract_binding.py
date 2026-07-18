"""Bind the bundle store's ``pair_validator`` seam to the public contract API.

Phase 3 (PR-A) of the GOAL-5 AC4 bundle-transactionality RFC
(RenQuant#492, ``doc/design/2026-07-17-artifact-bundle-transactionality.md``
§2.5): the artifacts-owned PUBLISHER invokes the renquant-pipeline PUBLIC
pair-validation API (``renquant_pipeline.bundle_contract.validate_pair``,
phase 2, renquant-pipeline#206) at writer-protocol step 6 BEFORE
publication. This module is that binding — a thin adapter, no validation
logic of its own.

Dependency direction (RFC §5): renquant-artifacts is the publication
authority; renquant-pipeline is a PEER dependency at publish time only.
``renquant_pipeline.bundle_contract`` is therefore imported lazily INSIDE
the factory function — importing this module (and the package
``__init__``) stays pipeline-free, so every non-publishing surface
(reader, GC, schema, break-glass rollback plumbing) remains fully usable
without renquant-pipeline installed. ``tests/test_bundle_contract_binding.py``
pins this with a subprocess whose import machinery blocks
``renquant_pipeline``.

Seam contract (phase 1, ``BundleStore._run_pair_validator``): reject =
raise, ``False``, or ``.ok`` falsy. ``validate_pair`` returns a
``PairVerdict`` whose ``ok`` is exactly that load-bearing field; a
rejection surfaces as ``BundleValidationError`` at step 6 with the
verdict's stable ``reason_codes`` in the message, the staged bundle
directory deleted under the store lock, and ACTIVE untouched.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping

from .bundle_store import BundleStore

#: The store-seam validator signature (phase 1): reject = raise / ``False``
#: / ``.ok`` falsy.
PairValidator = Callable[[Mapping[str, Any], Mapping[str, Path]], Any]


class PairValidatorUnavailableError(RuntimeError):
    """renquant-pipeline (the publish-time peer dependency) is missing.

    Raised by :func:`create_pair_validator` (and therefore
    :func:`create_default_store`) when ``renquant_pipeline.bundle_contract``
    cannot be imported. Fail-closed by design: a store without the public
    pair validator must not be handed out as the default PUBLISHING
    configuration. Read-only resolution and GC do not need the validator —
    construct :class:`~renquant_artifacts.bundle_store.BundleStore`
    directly for those surfaces.
    """


def create_pair_validator(
    *, accept_legacy_stamps: bool | None = None
) -> PairValidator:
    """Return the §2.5 public pair validator bound for the store seam.

    Lazily imports ``renquant_pipeline.bundle_contract.validate_pair``
    (see module docstring for why) and closes over
    ``accept_legacy_stamps`` — the M6 migration-window flag whose policy
    is owned by strategy config (phase-2 recorded ambiguity 1). Writer
    tools that have a resolved strategy config MUST pass its value;
    ``None`` (default) delegates to the contract's own runtime-window
    default, which is never looser than serve-time acceptance under
    default policy.

    Raises :class:`PairValidatorUnavailableError` if renquant-pipeline is
    not importable.
    """
    try:
        from renquant_pipeline.bundle_contract import validate_pair
    except ModuleNotFoundError as exc:
        raise PairValidatorUnavailableError(
            "renquant_pipeline.bundle_contract is not importable — "
            "renquant-pipeline is required to PUBLISH bundles (RFC §2.5: "
            "writer step 6 runs the public pair-validation API; RFC §5: "
            "pipeline is a publish-time peer dependency of the artifacts "
            "store). Install renquant-pipeline or add its src to "
            "PYTHONPATH. Read-only resolution and GC do not need it: "
            "construct BundleStore directly for those."
        ) from exc

    def pair_validator(
        manifest_payload: Mapping[str, Any],
        member_paths: Mapping[str, Path],
    ) -> Any:
        return validate_pair(
            manifest_payload,
            member_paths,
            accept_legacy_stamps=accept_legacy_stamps,
        )

    return pair_validator


def create_default_store(
    root: str | Path,
    *,
    accept_legacy_stamps: bool | None = None,
    **store_kwargs: Any,
) -> BundleStore:
    """The DEFAULT publishing configuration: a :class:`BundleStore` with
    the phase-2 public pair validator wired into the ``pair_validator``
    seam.

    Every writer tool (promote / refresh / break-glass — RFC §5: they
    INVOKE the artifacts-owned publication API) should obtain its store
    through this factory so step-6 pair validation can never be silently
    omitted. Additional :class:`BundleStore` keyword arguments
    (``alarm_hook``, ``clock``, …) pass through unchanged;
    ``pair_validator`` itself is NOT overridable here — callers that need
    a different validator are not constructing the default store and must
    say so by calling ``BundleStore`` directly.

    Fail-closed: raises :class:`PairValidatorUnavailableError` at
    construction time (not at first publish) when renquant-pipeline is
    absent.
    """
    if "pair_validator" in store_kwargs:
        raise TypeError(
            "create_default_store wires the phase-2 public pair validator "
            "itself; to supply a custom pair_validator construct "
            "BundleStore directly"
        )
    return BundleStore(
        root,
        pair_validator=create_pair_validator(
            accept_legacy_stamps=accept_legacy_stamps
        ),
        **store_kwargs,
    )


__all__ = [
    "PairValidator",
    "PairValidatorUnavailableError",
    "create_default_store",
    "create_pair_validator",
]
