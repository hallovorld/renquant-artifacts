"""Phase-3 binding of the store's ``pair_validator`` seam (GOAL-5 AC4).

Normative: RFC RenQuant#492 §2.5 (call sites) + §5 (ownership — pipeline
is a publish-time peer dep of the artifacts publisher). Phase 1 =
renquant-artifacts#25 (the seam), phase 2 = renquant-pipeline#206 (the
public ``validate_pair`` API + fixture vectors).

Three surfaces:

1. importability WITHOUT renquant-pipeline — subprocess whose import
   machinery blocks ``renquant_pipeline``: ``import renquant_artifacts``
   must succeed (the lazy factory import is the ONLY pipeline touchpoint)
   and ``create_pair_validator()`` must fail-closed with
   ``PairValidatorUnavailableError``;
2. factory wiring — ``create_default_store`` binds ``validate_pair`` into
   the seam, refuses a caller-supplied ``pair_validator``, and threads
   ``accept_legacy_stamps`` through;
3. publish integration on the phase-2 contract vectors, read from the
   sibling renquant-pipeline checkout (this repo's cross-repo test
   convention — pytest ``pythonpath`` sibling entries + CI sibling
   checkout; the vectors' canonical home moves to renquant-common in
   phase-3 PR-B, and these reads switch there in a follow-up):
   publish-ACCEPT on both matching-pair cases, publish-REJECT on the
   mismatched / cross-schema / missing-binding cases with the staged
   bundle deleted and ACTIVE untouched.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from renquant_artifacts import (
    BundleValidationError,
    PairValidatorUnavailableError,
    create_default_store,
    create_pair_validator,
)
from tests.bundle_helpers import (
    CAL,
    PANEL,
    TickingClock,
    make_authorization,
    make_bindings,
    publish_simple,
)

pipeline_bundle_contract = pytest.importorskip(
    "renquant_pipeline.bundle_contract",
    reason="renquant-pipeline sibling checkout not available (publish-time "
    "peer dep; CI always provides it — see .github/workflows/ci.yml)",
)


def _vectors_path() -> Path:
    """The phase-2 vectors in the sibling renquant-pipeline checkout.

    Resolved from the imported module's location (src layout:
    ``<repo>/src/renquant_pipeline/bundle_contract.py`` →
    ``<repo>/tests/fixtures/bundle_contract/vectors.json``). If the module
    imports but the fixture is missing, that is a broken checkout — FAIL,
    never skip (a silent skip here would drop the RFC §4 write-authority
    coverage in CI).
    """
    repo_root = Path(pipeline_bundle_contract.__file__).resolve().parents[2]
    path = repo_root / "tests" / "fixtures" / "bundle_contract" / "vectors.json"
    if not path.is_file():
        pytest.fail(
            f"renquant_pipeline.bundle_contract imported from {repo_root} but "
            f"the phase-2 fixture vectors are missing at {path}; the sibling "
            "checkout is incomplete"
        )
    return path


VECTORS = json.loads(_vectors_path().read_text(encoding="utf-8"))
CASES = {case["name"]: case for case in VECTORS["cases"]}

ACCEPT_CASES = ("matching_pair_legacy_schema", "matching_pair_v1_schema")
REJECT_CASES = {
    "mismatched_pair": "fingerprint_mismatch",
    "missing_binding": "missing_binding",
    "cross_schema_comparison_refused": "cross_schema_refused",
}


def _serialize(payload: dict) -> bytes:
    """The member serialization pinned inside vectors.json."""
    assert VECTORS["member_serialization"] == (
        "json.dumps(payload, sort_keys=True, indent=2) + '\\n' (utf-8)"
    )
    return (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _case_members(case: dict) -> dict[str, bytes]:
    return {
        PANEL: _serialize(case["scorer_payload"]),
        CAL: _serialize(case["calibrator_payload"]),
    }


def _make_default_store(root: Path, **kwargs):
    kwargs.setdefault("local_mount_guard", lambda p: (True, "test-injected"))
    kwargs.setdefault("clock", TickingClock())
    return create_default_store(root, **kwargs)


# ---------------------------------------------------------------------------
# 1. Importable without renquant-pipeline (lazy import is the ONLY touchpoint)
# ---------------------------------------------------------------------------

def test_package_imports_and_factory_fails_closed_without_pipeline() -> None:
    """With ``renquant_pipeline`` blocked at the import machinery,
    ``import renquant_artifacts`` succeeds and the factory raises the
    dedicated fail-closed error (RFC §5: pipeline is a peer dep at publish
    time ONLY)."""
    code = textwrap.dedent(
        """
        import sys

        class _BlockPipeline:
            def find_spec(self, name, path=None, target=None):
                if name == "renquant_pipeline" or name.startswith("renquant_pipeline."):
                    raise ModuleNotFoundError(
                        f"renquant_pipeline blocked by test: {name}"
                    )
                return None

        sys.meta_path.insert(0, _BlockPipeline())

        # Package import must NOT touch renquant_pipeline.
        import renquant_artifacts
        from renquant_artifacts import (
            PairValidatorUnavailableError,
            create_default_store,
            create_pair_validator,
        )

        try:
            create_pair_validator()
        except PairValidatorUnavailableError as exc:
            assert "publish" in str(exc).lower(), str(exc)
        else:
            raise AssertionError("create_pair_validator did not fail closed")

        try:
            create_default_store("/tmp/never-created")
        except PairValidatorUnavailableError:
            pass
        else:
            raise AssertionError("create_default_store did not fail closed")

        print("OK-without-pipeline")
        """
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert "OK-without-pipeline" in proc.stdout


# ---------------------------------------------------------------------------
# 2. Factory wiring
# ---------------------------------------------------------------------------

def test_create_pair_validator_returns_seam_compatible_verdicts(
    tmp_path: Path,
) -> None:
    validator = create_pair_validator()
    case = CASES["matching_pair_legacy_schema"]
    for name, data in _case_members(case).items():
        (tmp_path / name).write_bytes(data)
    member_paths = {PANEL: tmp_path / PANEL, CAL: tmp_path / CAL}

    verdict = validator(case["manifest"], member_paths)
    assert bool(verdict.ok) is True
    assert verdict.matched_schema == "legacy"

    bad = CASES["mismatched_pair"]
    for name, data in _case_members(bad).items():
        (tmp_path / name).write_bytes(data)
    verdict = validator(bad["manifest"], member_paths)
    # The seam rule (phase 1): .ok falsy => reject.
    assert bool(verdict.ok) is False
    assert "fingerprint_mismatch" in verdict.reason_codes


def test_create_default_store_refuses_pair_validator_override(
    tmp_path: Path,
) -> None:
    with pytest.raises(TypeError, match="pair_validator"):
        create_default_store(
            tmp_path,
            pair_validator=lambda m, p: True,
            local_mount_guard=lambda p: (True, "test-injected"),
        )


def test_accept_legacy_stamps_false_threads_through_to_publish(
    tmp_path: Path,
) -> None:
    """Flag-off (post-migration-window policy): the legacy matching pair is
    a version-gap REJECT at publication, same as at serve time."""
    store = _make_default_store(tmp_path, accept_legacy_stamps=False)
    case = CASES["matching_pair_legacy_schema"]
    with pytest.raises(BundleValidationError, match="version_gap"):
        store.publish(
            _case_members(case),
            bindings=make_bindings(),
            authorization=make_authorization(),
        )
    # The v1-schema pair is unaffected by the flag.
    result = store.publish(
        _case_members(CASES["matching_pair_v1_schema"]),
        bindings=make_bindings(),
        authorization=make_authorization(),
    )
    assert result.generation == 1


# ---------------------------------------------------------------------------
# 3. Publish integration on the phase-2 contract vectors
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("case_name", ACCEPT_CASES)
def test_publish_accepts_matching_pair(tmp_path: Path, case_name: str) -> None:
    store = _make_default_store(tmp_path)
    case = CASES[case_name]
    assert case["expected"]["ok"] is True

    result = store.publish(
        _case_members(case),
        bindings=make_bindings(),
        authorization=make_authorization(),
    )
    assert result.generation == 1

    with store.resolve_active() as resolved:
        assert resolved.bundle_id == result.bundle_id
        assert resolved.generation == 1
        assert resolved.read_member(PANEL) == _serialize(case["scorer_payload"])


@pytest.mark.parametrize("case_name", sorted(REJECT_CASES))
def test_publish_rejects_invalid_pair(tmp_path: Path, case_name: str) -> None:
    """Writer step 6 REJECT: BundleValidationError carrying the vector's
    expected reason code, staged bundle dir deleted, no ACTIVE pointer, no
    PREPARE record — the store is exactly as if publish was never called."""
    store = _make_default_store(tmp_path)
    case = CASES[case_name]
    assert case["expected"]["ok"] is False
    expected_reason = REJECT_CASES[case_name]
    assert case["expected"]["reason_codes"] == [expected_reason]

    with pytest.raises(BundleValidationError, match=expected_reason):
        store.publish(
            _case_members(case),
            bindings=make_bindings(),
            authorization=make_authorization(),
        )

    # Staged bundle deleted under lock; pointer never created.
    leftover = [
        p.name
        for p in store.bundles_dir.iterdir()
        if p.name not in (".lock", "OPERATIONS.jsonl")
    ]
    assert leftover == []
    assert not store.active_path.exists()
    if store.operations_path.exists():
        assert store.operations_path.read_text(encoding="utf-8").strip() == ""


def test_reject_after_accept_leaves_active_serving_the_good_bundle(
    tmp_path: Path,
) -> None:
    """The 07-14→16 incident shape at the publication boundary: a good pair
    is ACTIVE; an orphaned-binding pair is refused publication and the
    serving state is untouched (RFC §1 — pair-level invariant enforced
    BEFORE the flip, not repaired after)."""
    store = _make_default_store(tmp_path)
    good = store.publish(
        _case_members(CASES["matching_pair_legacy_schema"]),
        bindings=make_bindings(),
        authorization=make_authorization(),
    )

    with pytest.raises(BundleValidationError, match="fingerprint_mismatch"):
        store.publish(
            _case_members(CASES["mismatched_pair"]),
            bindings=make_bindings(),
            authorization=make_authorization(),
        )

    active_line = store.active_path.read_text(encoding="utf-8").strip()
    assert active_line == f"1 {good.bundle_id}"
    with store.resolve_active() as resolved:
        assert resolved.bundle_id == good.bundle_id
        assert resolved.generation == 1
    # Only the good bundle remains archived.
    dirs = sorted(
        p.name for p in store.bundles_dir.iterdir() if p.is_dir()
    )
    assert dirs == [good.bundle_id]

    # And the store still accepts a subsequent valid pair (gen 2).
    nxt = store.publish(
        _case_members(CASES["matching_pair_v1_schema"]),
        bindings=make_bindings(),
        authorization=make_authorization(),
    )
    assert nxt.generation == 2


def test_default_store_keeps_schema_checks_of_undecorated_store(
    tmp_path: Path,
) -> None:
    """The binding ADDS pair validation; it must not weaken phase-1 checks
    (authorization contract still enforced through the factory-built
    store)."""
    store = _make_default_store(tmp_path)
    with pytest.raises(Exception, match="hand-edit"):
        publish_simple(store, authorization=make_authorization(tool="hand-edit"))
