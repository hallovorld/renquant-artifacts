# Bundle store phase 1: AUTHORITATIVE transactional store (GOAL-5 AC4)

Date: 2026-07-18
Spec: RFC "transactional artifact bundles for the 104 serving pair"
(RenQuant#492, `doc/design/2026-07-17-artifact-bundle-transactionality.md`,
r4 text = final). This PR implements the renquant-artifacts-owned pieces
ONLY (RFC §5): bundle identity/schema, the writer/reader protocols, the
operation-log authorization contract, reference-rooted GC, and the
break-glass tool. Library code + tests only — NOTHING touches the live
production store; migration is RFC §3 with its own census gate.

## Delivered

- `src/renquant_artifacts/bundle_schema.py` — schema v1 (§2.2): closed
  manifest field set (unknown fields rejected, recursively), exact member
  set, per-member `{sha256, bytes}`, canonical serialization (UTF-8,
  sorted keys, compact, trailing LF), `manifest_digest` over the manifest
  without that field, derived `bundle_id = <utc-ts>Z-<digest16>`,
  authorization block validation (§2.4) incl. the mandatory
  `source.incident_ref` for the restamp class and rejection of
  `tool="hand-edit"`.
- `src/renquant_artifacts/bundle_store.py` — `BundleStore`:
  - writer (§2.3): the 10-step durability-ordered commit under
    `flock(bundles/.lock, EX)`; PREPARE fsync'd to `OPERATIONS.jsonl`
    BEFORE the pointer flip; ACTIVATE bound to its PREPARE line by
    sha256; pointer `"<generation> <bundle_id>"`, generation strictly
    monotonic (never reused, even across recovered crash intervals);
    bundle_id collision => abort; step-6 failure deletes the dir under
    lock. Host-model guard refuses non-local mounts at open (§2.1),
    fail-closed on unknown platforms, injectable.
  - reader (§2.6): ACTIVE -> PREPARE audit (NO PREPARE = REFUSE;
    PREPARE-no-ACTIVATE = crash interval: serve + alarm + require
    RECOVERY before next mutation) -> generation-regression refusal ->
    dirfd-open -> digest verify -> serve. `resolve_bundle()` gives
    archive resolution for run-bundle replay.
  - GC (§2.6 r4): deletes ONLY non-ACTIVE ∧ outside parent_bundle
    rollback ancestry ∧ unreferenced per the injected reference query;
    NO time/count cutoff exists anywhere (enforced by test on the API
    signature); serializes on the store flock; log-ahead GC_DELETE
    records; unlink-after-open keeps mid-read dirfds valid.
  - `commit_recovery()` appends the RECOVERY record naming the interval,
    with alarm.
- `src/renquant_artifacts/bundle_breakglass.py` — the ONLY sanctioned
  manual tool (§2.4): mandatory `--incident-ref`, same commit protocol,
  `authorization.tool=bundle_breakglass`, ALWAYS alarms,
  `--rollback-to` restricted to parent_bundle ancestors (new higher
  generation; pointer never regresses).

## Verification (§4 subset owned here)

126 new tests (176 total, all green; `make doctor` clean):

- kill-injection matrix: 19 checkpoints — every numbered §2.3 step plus a
  point after each of the 9 fsync sites — each proving the §2.3
  activation-audit invariant (a reader NEVER serves a generation without
  its PREPARE record), SPECIFICALLY the rename->ACTIVATE interval
  (serve + alarm + mutation refusal until RECOVERY; generation burned,
  not reused). Plus rollback-path kill and an adversarial
  flip-before-PREPARE state (refused). Harness = monkeypatched crash
  hook with lock-release-on-death semantics (the §4-sanctioned
  alternative to subprocess SIGKILL); true power-loss buffer reordering
  is out of unit-test reach — fsync ordering is enforced by construction
  and pinned by the checkpoint labels.
- reader/GC races: reader holds dirfd across a GC pass that unlinks its
  bundle; pointer flip mid-read; flip-then-GC combined.
- invalid-schema / extra-member / missing-member injection (writer step 6
  and reader); digest-consistent unknown-field smuggling refused.
- stale-pointer generation regression detected + refused.
- GC retention hierarchy (r4): referenced bundle survives -> reference
  expired under injected policy -> same bundle collected; unreferenced
  sibling collected first; ACTIVE + ancestry never collected.
- break-glass leaves manifest + operation-log records and always alarms;
  non-ancestor rollback refused.

## Phase-1 seams (bound in later phases; NOT improvised here)

- §2.5 pair validation: `BundleStore(pair_validator=...)` with signature
  `pair_validator(manifest_payload: Mapping[str, Any], member_paths:
  Mapping[str, Path]) -> Any` (reject = raise, `False`, or `.ok` falsy).
  To be bound to `renquant_pipeline.bundle_contract.validate_pair` when
  that module lands (pipeline phase). Absent a validator, step 6 still
  does full schema + digest re-verification.
- §2.6 reference query: `collect_garbage(is_referenced: Callable[[str],
  bool])` — the orchestrator owns the run-bundle store; it will supply
  the real query when its phase lands. No default on purpose.
- Alarm surface: `alarm_hook(kind, payload)` — drift-sentinel binding is
  a later phase; the break-glass CLI surfaces alarms on stderr meanwhile.

## Interpretations / RFC ambiguities recorded for review

1. `bundle_id` is DERIVED (directory name), not a stored manifest field:
   storing it would be circular with `manifest_digest` (§2.2 excludes
   only `manifest_digest` from the digest input). It is reproducible
   from `created_at` + `manifest_digest`.
2. "LF" in the canonical form is implemented as a single trailing LF on
   the compact one-line serialization (no other line breaks exist).
3. Restamp-class recognition is by tool name (`restamp` substring or
   `bundle_breakglass`); the RFC names the classes but no explicit
   class field exists in the §2.4 field list.
4. `--rollback-to` accepts proper ancestors only (self is a no-op flip,
   refused).
5. GC additionally sweeps dead `<id>.tmp` dirs (provably dead: writers
   hold the flock across steps 2-10) with `GC_SWEEP_TMP` op-log records —
   a housekeeping addition beyond the RFC letter, flagged for reviewer
   decision.
6. `authorization.inputs` is type-checked (string->digest-string map)
   but may be empty; the RFC does not state a minimum.

## Boundaries respected

No writes to any production path; no other repo touched
(renquant-pipeline `bundle_contract`, orchestrator run-bundle fields, and
umbrella census/views are later phases per the RFC ownership map §5).
