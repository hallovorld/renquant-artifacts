from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).parents[1]
STORE = ROOT / "store"

#: The store-relative remainders of the D6-2a experiment refs (the part of
#: each config ref after the "artifacts/" component) — the orchestrator's
#: run-manifest artifact_store binding resolves <store>/<remainder>.
EXPERIMENT_REMAINDERS = (
    "patchtst_shadow/pt07_strict_trainfit_embargo60_20260522/seed_44/"
    "hf_patchtst_all_seed44_model.pt",
    "shadow/panel-rank-calibration."
    "hf_patchtst_seed44_trainfit_20230103_20240409.json",
)


def test_every_manifest_entry_exists_and_hashes_clean() -> None:
    manifest = json.loads((STORE / "STORE-MANIFEST.json").read_text())
    assert manifest, "store manifest must not be empty"
    for rel, expected in manifest.items():
        blob = STORE / rel
        assert blob.is_file(), f"{rel} listed but missing"
        assert hashlib.sha256(blob.read_bytes()).hexdigest() == expected, (
            f"{rel}: content sha drifted from STORE-MANIFEST.json"
        )


def test_experiment_refs_resolve_from_this_pinned_checkout() -> None:
    """Integration shape used by orchestrator #464: store-addressed config
    refs resolve to <this checkout>/store/<remainder>. No path outside this
    repository is consulted."""
    manifest = json.loads((STORE / "STORE-MANIFEST.json").read_text())
    for remainder in EXPERIMENT_REMAINDERS:
        blob = STORE / remainder
        assert blob.is_file(), f"experiment ref unresolvable: {remainder}"
        assert blob.stat().st_size > 0
        assert remainder in manifest, f"{remainder} must be sha-pinned"
    # the model blob specifically (the r1 regression: .gitignore *.pt
    # silently dropped it and only sidecars were committed)
    model = STORE / EXPERIMENT_REMAINDERS[0]
    assert model.suffix == ".pt" and model.stat().st_size > 100_000
