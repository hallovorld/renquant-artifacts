# Crypto model registry entry and promotion contract

**Date:** 2026-07-12
**PR:** artifacts feat/crypto-model-registry
**Deliverable:** D-C13 (crypto trading RFC)

## What

- Added `registry/crypto-xgb-diagnostic.json` — diagnostic-level manifest
  for the crypto XGB model (no model trained yet; placeholder contract).
- Added `validate_crypto_promotion_contract()` to `validation.py` — enforces
  crypto-specific promotion gates:
  - diagnostic: no extra requirements
  - shadow: requires `paper_battery_pass=true` (D-C12 stage-0 battery)
  - prod: requires `accepted=true` + `shadow_days` reported
- 7 new tests covering the manifest validation and promotion ladder.

## Why

The crypto trading sleeve (G2) needs an artifact registry entry before the
model training pipeline can register trained artifacts.  The promotion
contract ensures no crypto model reaches shadow/prod without passing the
paper trading battery first.

## Status

- 39/39 tests pass (0 regressions).
- No crypto model artifact exists yet — this is the contract scaffold only.

## Revision note (2026-07-12, pre-Codex-review self-fix)

Independent review (before Codex reviewed this PR) found the promotion gate
was fail-open on three inputs that matter precisely because this is a
capital-risk gate on hand-editable JSON registry files:

1. `paper_battery_pass` was checked with a plain truthy test
   (`if not metrics.get("paper_battery_pass")`), inconsistent with the
   stricter `is not True` already used a few lines below for `accepted` in
   the *same function*. A hand-edited manifest carrying the string `"false"`
   (truthy in Python) would silently satisfy the battery-pass gate. Fixed to
   `is not True` for consistency and to close the gap.
2. `shadow_days` was checked with a plain truthy test — a string `"7"`, or
   `True` (a `bool`, which is an `int` subclass in Python), would pass.
   Fixed to require a real, positive, non-bool numeric value.
3. `target_status` was not validated against the known set
   (`diagnostic`/`shadow`/`prod`) — any other string (e.g. a typo like
   `"prod-scaled"`) fell through both `if` blocks and returned `ok=True` with
   zero checks applied. Fixed to raise `ValueError` on any unrecognized
   status.

Added 5 regression tests for these cases. Full suite: 44/44 pass.
