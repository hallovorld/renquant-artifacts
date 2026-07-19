# Governed enforcement window for the F-7 required-provenance contract

Date: 2026-07-18
Author: haorensjtu-dev
Scope: cross-repo CI unbreak — no weakening of the F-7 contract for opted-in
callers; canonical-publication paths stay unconditionally strict.

## Problem — #24 enforced ahead of its own sequencing

renquant-artifacts#24 (merged 2026-07-18T16:44Z) made the `provenance`
record REQUIRED in the registry/manifest validation funnel unconditionally.
Its own review ordering sequenced the consumer migrations — renquant-model#55
and renquant-orchestrator#518, which stamp/thread provenance on every
consumer manifest — to land LATER. The unconditional raise was therefore a
flag-day break of every consumer repo's CI on any fresh run:

| repo | failing tests | error |
|---|---|---|
| renquant-backtesting | 2 (`tests/test_runtime_parity.py`) | `ValueError: artifact manifest is missing a required 'provenance' record` |
| renquant-model | 4 (gbdt/patchtst training-pipeline tests) | same |
| renquant-orchestrator | 26 (`test_cli` / `test_contract_fixture` / `test_daily_run_pipeline`) | same |

Mains' green badges merely predate the merge. This blocked ALL merges in the
three repos, including two already-approved evidence PRs (backtesting#73,
orch#552).

## Fix — enforcement window, mirroring the fleet's existing precedent

The requirement is now GOVERNED instead of flag-day, following the umbrella
manifest-URI resolver's `ARTIFACT_DIGEST_REQUIRED_AFTER` precedent
(tolerate + warn before a dated cutoff so existing manifests keep
validating; fail closed on/after it), combined with this repo's own `RQ_*`
environment-flag convention (`RQ_ROOT`, `RQ_BUNDLE_STORE_ROOT`) for
opt-in-early:

- `PROVENANCE_REQUIRED_AFTER = date(2026, 8, 15)`
  (`experiment_registry.py`) — a ~4-week landing window for model#55 /
  orch#518 from today. On/after this date, missing provenance fails closed
  unconditionally.
- `RQ_REQUIRE_PROVENANCE=1` (`PROVENANCE_ENFORCEMENT_ENV`) — per-environment
  early opt-in. One-way: it can only ENABLE enforcement early; no value can
  disable it after the date (same fail-closed one-way semantics as the
  umbrella's `digest_required`).
- `require_provenance=True` — per-call trusted-caller opt-in threaded
  through `validate_artifact_manifest` / `load_artifact_manifest` /
  `resolve_artifact_manifest` / both pipeline contexts. This is the hook
  the sequenced migrations flip per-callsite as they land. Also one-way.
- `provenance_required(now=..., environ=...)` — the injectable window
  predicate (deterministic tests, no wall-clock coupling).

Why the date pattern as primary rather than an opt-in-only flag defaulting
OFF: a flag that defaults OFF forever is a permanent weakening that needs a
second, easily-forgotten PR to flip — the exact "deployed-but-dark"
failure mode this fleet has already paid for. The dated window self-closes;
if the migrations land early the flag/param close it earlier, and if they
slip past 2026-08-15 the gate activates anyway (fail-closed direction, and
the safe side for a promote gate — the prior pinned model stays).

## What is NOT relaxed

- **`verify_artifact_provenance` itself is unchanged** — unconditionally
  strict for every direct caller.
- **A manifest that CARRIES a `provenance` key is always fully verified**,
  window or no window — only complete ABSENCE of the key (the pre-F-7
  legacy shape) is tolerated, so the window cannot be used as a bypass:
  a present-but-malformed record, `kind="none"` + `promotion_status="prod"`,
  and a prod canonical manifest without a resolvable publication record all
  still raise exactly as on main.
- **The #24 canonical-publication paths (`canonical_registry.py`) are
  untouched** — new surfaces with no legacy callers keep unconditional
  strict enforcement (`register_canonical_publication` /
  `resolve_canonical_publication` / the round-4 prod boundary).
- The in-window tolerance emits a `FutureWarning` naming model#55/orch#518
  as the migrations that close the window — never a silent acceptance.

The two #24 tests that pinned the flag-day shape
(`test_manifest_without_provenance_key_now_rejected`,
`test_omitted_provenance_bypass_closed`) now pin the identical strict
behavior through the per-call switch (the post-migration behavior); the
in-window warn shape is pinned in the new
`tests/test_provenance_enforcement_window.py` (19 tests, all
date-injected — nothing in the suite flips when the window date passes,
and a canary test refuses a window date that is already in the past).

## Evidence

Artifacts suite: **313 passed** (294 baseline at #24 + 19 new), verified on
both the repo venv path and system python.

Cross-repo verification — fresh scratch clones of the three consumers at
main, siblings of this branch, consumer repos untouched:

| repo | vs artifacts main (7859f8d) | vs this branch |
|---|---|---|
| renquant-backtesting | 2 failed, 325 passed | **327 passed, 0 failed** |
| renquant-model | 4 failed, 835 passed | **839 passed, 0 failed** |
| renquant-orchestrator | 26 failed, 4060 passed | **4086 passed, 0 failed** |

Strict preservation proof (cross-repo): re-running the two backtesting
tests with `RQ_REQUIRE_PROVENANCE=1` against this branch reproduces the
exact #24 rejection (2 failed) — strict mode is fully intact and opt-in
works end-to-end.

## Follow-ups

- model#55 and orch#518 land their provenance stamping and flip
  `require_provenance=True` (or set `RQ_REQUIRE_PROVENANCE=1` in their CI)
  — closing the window early per-callsite.
- After both land (and no later than 2026-08-15), a small PR deletes the
  tolerance branch and the window constants; the canary test enforces that
  the constant is never quietly re-bumped without review.
