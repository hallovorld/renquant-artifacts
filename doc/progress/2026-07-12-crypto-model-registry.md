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

## Revision note (2026-07-12, Codex CHANGES_REQUESTED round 1 fixes, PR #23)

Codex reviewed the PR and requested changes on 3 findings. This round fixes
findings 2 and 3 mechanically and documents finding 1 honestly without
implementing it (rationale below).

### Finding 2 — fixed: explicit source status + strict transition graph

`validate_crypto_promotion_contract` previously took only `target_status`
and never checked the manifest's own current `promotion_status`, so a caller
could request `target_status="prod"` against a still-`diagnostic` manifest
and have it evaluated only against the prod metrics gate — an apparent
diagnostic-to-prod skip.

Fixed:
- The function now requires an explicit `current_status` keyword argument
  (no default that lets a caller skip declaring it). Passing none raises.
- `current_status` is cross-checked against the manifest's own
  `promotion_status` field; a mismatch is rejected (`"crypto promotion
  current_status mismatch"`).
- A strict transition table now permits only `diagnostic -> shadow` and
  `shadow -> prod` as single-step promotions. Skip-stage jumps
  (`diagnostic -> prod`), same-status no-ops, unknown statuses, and
  backwards transitions are all rejected with `"illegal promotion
  transition"`.
- Per the reviewer's own scoping note, this round does **not** bind the
  resulting promotion record to predecessor artifact/evidence digests —
  that is part of finding 1's larger evidence-binding design (see below).

New tests (`tests/test_artifact_manifest_validation.py`): skip-stage
rejection, mismatched-current-status rejection, unknown-current-status
rejection, same-status rejection, backwards-transition rejection, and the
`current_status`-required check. All previously-passing valid-transition
tests (`diagnostic -> shadow`, `shadow -> prod`) were updated to pass the
new required argument and continue to pass.

### Finding 3 — fixed: placeholder manifest is now structurally non-resolvable

`crypto-xgb-diagnostic.json` says no model exists, carries a placeholder
fingerprint (`sha256:placeholder-awaiting-first-train`), and references an
unresolved store URI — yet the generic `ArtifactManifestValidationPipeline`
reported it `ok=True`, indistinguishable from a real artifact.

Fixed:
- Added a `"draft": true` field to the manifest, marking it explicitly as a
  placeholder (not a new promotion-status value — status stays
  `diagnostic`; `draft` is an orthogonal "this isn't a real artifact yet"
  flag).
- `ValidateArtifactManifestTask` (the core check underlying
  `validate_artifact_manifest`, and therefore both `load_artifact_manifest`
  and `resolve_artifact_manifest` in `registry.py`) now raises when
  `manifest.get("draft") is True`. A draft manifest can no longer be loaded
  or resolved by any consumer.
- `validate_crypto_promotion_contract` also rejects any manifest with
  `draft is True` outright, regardless of `target_status` — a placeholder
  cannot be promoted even if its metrics block is (incorrectly) fully
  populated.

New tests: the real `crypto-xgb-diagnostic.json` fixture now raises from
`ArtifactManifestValidationPipeline` (was previously asserted `ok=True`);
`resolve_artifact_manifest`/`load_artifact_manifest` reject a draft-marked
manifest even when it is the sole candidate matching the query
(`tests/test_artifact_registry.py`); `validate_crypto_promotion_contract`
rejects a draft manifest with fully-populated metrics.

### Finding 1 — documented only, not implemented (deliberate scope decision)

Codex's finding: `paper_battery_pass`, `accepted`, and `shadow_days` are
hand-editable manifest metrics with no binding to real evidence (a Stage-0
paper-readiness record, a model-evaluation/acceptance artifact from
renquant-model, or an actual timestamped shadow-observation history). This
is a genuine gap — but closing it means designing a new cross-repo,
content-addressed evidence-binding schema (digest/run_id references into
orchestrator's Stage-0 readiness records and renquant-model's
evaluation-acceptance evidence). That is a real architecture decision
spanning 3 repos, analogous to how a trust-anchor design decision earlier
this cycle (orchestrator PR #501) was handled conservatively — a documented
safety-tightening restriction, not an invented cryptographic/reference
scheme picked unilaterally by an agent.

Verified before deciding to defer:
- Grepped every sibling repo (orchestrator, execution, model, pipeline,
  strategy-104, backtesting, base-data, common) for
  `validate_crypto_promotion_contract`: **zero external callers**. Nothing
  outside `renquant-artifacts` itself currently consumes this function's
  output to authorize anything.
- The orchestrator's crypto entry pipeline is independently hard-blocked by
  `ENTRY_AUTHORIZATION_TRUST_ANCHOR_READY = False` in
  `renquant_orchestrator/crypto_session.py:988`, which blocks every crypto
  entry in every mode regardless of what this promotion contract says.

So the self-attestation gap is real but currently **dormant** — it cannot
presently authorize any live capital-risk action. Rather than invent a
digest/run_id evidence schema unilaterally inside this mechanical fix PR, a
docstring on `validate_crypto_promotion_contract` now states honestly: (a)
these three fields are self-attested, not verified against real evidence;
(b) the gap is known and not wired into any live decision path (with the
zero-caller verification cited); (c) a future PR must replace these
booleans with content-addressed evidence references before this contract
can be trusted as a real authorization gate, and that schema is a
cross-repo design decision for a human operator to make, not something to
invent here.

### Verification

- Full suite: 52/52 pass (was 44/44 before this round; net +8 new tests).
- Confirmed load-bearing: stashed the `validation.py` + fixture changes
  (keeping only the new/updated tests) and reran — 19 of the touched/new
  tests failed against the pre-fix code (mostly
  `TypeError: unexpected keyword argument 'current_status'`, plus 2
  `DID NOT RAISE` on the draft-rejection tests), 11 unaffected tests still
  passed. Restored the fix — all 52 pass again.
