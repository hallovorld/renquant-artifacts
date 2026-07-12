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
