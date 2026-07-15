# Experiment registry: verified pins + real promotion-boundary enforcement

**Date:** 2026-07-14
**PR:** artifacts g3/f7-experiment-registry
**Companion:** RenQuant#471 "fix(sim): default run_sim_104.py to pinned strategy config (F-7)", round 7
**Trigger:** Codex round-5 review of RenQuant#471 (quoted in full in the RenQuant#471 comment thread)

## What

Added `src/renquant_artifacts/experiment_registry.py` as the canonical, shared
implementation of the "registered experiment" governance contract that
`scripts/run_sim_104.py` (umbrella repo, F-7) previously implemented as a
local, unreferenced helper. Any other producer of non-production sim/
backtest/score-backfill output (e.g. renquant-model's score-backfill tooling)
should import and reuse these functions rather than hand-rolling an
equivalent — this mirrors the "triple-impl fingerprint" incident
(`renquant_common.model_fingerprint`) where three independently hand-copied
`model_content_sha256` implementations silently diverged.

### 1. Promotion-boundary enforcement (Codex finding 1)

`reject_exploratory_promotion()` was an unreferenced helper in
`run_sim_104.py` — nothing in the umbrella repo, this repo, or
renquant-pipeline called it, so the EXPLORATORY_ONLY marker was not
enforcement.

Wired it into `ValidateArtifactManifestTask.run()`
(`src/renquant_artifacts/validation.py`) — the ONE function every real
promotion/admission caller across the multirepo funnels through:
`renquant_pipeline.inference.ValidateRuntimeInputsTask` (live/shadow/sim
runtime, calls `validate_artifact_manifest` before any decision/order-intent
is produced) and `renquant_artifacts.registry.{load,resolve}_artifact_manifest`
(registry resolution). A candidate manifest that declares
`"provenance_dir": "<sim/backfill run's output directory>"` is now rejected
if that directory carries an EXPLORATORY_ONLY classification.

`tests/test_experiment_registry.py::TestPromotionBoundaryIntegration` proves
this against the REAL (non-mocked) entrypoints: it writes a real
`_experiment_classification.json` via `write_experiment_classification`,
builds a real candidate artifact manifest referencing it, and calls the real
`validate_artifact_manifest` / `load_artifact_manifest` /
`resolve_artifact_manifest` directly, asserting each raises. A regression
control (`test_non_exploratory_provenance_dir_is_accepted`,
`test_manifest_without_provenance_dir_is_unaffected`) proves the check is
specific to the marker, not merely "has a provenance_dir."

### 2. Pins required AND verified (Codex finding 2)

`verify_experiment_pins()` verifies all 5 required pin categories against
the ACTUAL environment, not just their presence:

| Pin | Verified against |
|---|---|
| `strategy_config` | `renquant-strategy-104` entry's commit/remote in the caller's `subrepos.lock.json`, cross-checked against the live checkout (HEAD/dirty/remote) via `verify_code_pin` — same discipline `run_sim_104._verify_pin` already applies to the strategy config specifically, generalized |
| `pipeline_version` | Same, for `renquant-pipeline` |
| `data_snapshot` | `fingerprint` field of a caller-supplied data manifest (schema: `renquant_base_data.validate_data_manifest`) |
| `model_artifact` | Full-file hash (`renquant_common.model_fingerprint.artifact_sha256`) of a caller-supplied artifact path |
| `calendar_universe` | Stable hash (`hash_jsonable`) of the caller's resolved, sorted, de-duplicated universe/watchlist |

A category whose supporting evidence (`data_manifest`,
`model_artifact_path`, `universe`) is not supplied is an ERROR, not a
silently-skipped pass. `verify_code_pin` also cross-checks the declared pin
against the caller's OWN `subrepos.lock.json` pin (not only the live
checkout), so a manifest can't claim reproducibility against a commit the
lock has since moved past.

### 3. Manifest registry (Codex finding 3)

`verify_manifest_registered()` checks a manifest's own file digest against
an entry in a caller-provided, git-tracked JSON index
(`{experiment_id: {"digest": ..., "path": ...}}`). "Lives under the
registered directory" is necessary but not sufficient — the digest must
also match a deliberately-registered entry, so editing a manifest after
registration (without updating the index in the same commit) fails closed.

## Where the logic lives vs. who calls it

This package owns the CONTRACT (pin verification, classification marker,
registry-digest check, promotion-boundary enforcement). The umbrella repo
(`scripts/run_sim_104.py`, RenQuant#471 r7) owns the CALL SITE: it builds the
`experiments/manifests/INDEX.json` registry file, resolves the manifest's
`data_manifest_path`/`model_artifact_path`/resolved watchlist, and calls
`verify_experiment_pins` / `verify_manifest_registered` /
`write_experiment_classification` from this package rather than
reimplementing them.

## Tests (round 1)

85 tests pass (0 regressions) — `tests/test_experiment_registry.py` (new,
40 tests) plus the existing 45 tests across `test_artifact_manifest_validation.py`
and `test_artifact_registry.py`. Code-pin tests use real temporary git repos
(not mocks) via `subprocess`, matching the discipline Codex asked for on the
companion PR ("a real temporary git-repo integration test rather than only
mocks for the success path").

## Round 2 (same date): provenance made REQUIRED + bound to the registry

**Trigger:** Codex follow-up review on this PR (#24), `CHANGES_REQUESTED`,
2026-07-14T16:42:29Z, quoted verbatim:

> **[P1] The promotion guard is bypassable because provenance is optional and
> self-declared.**
>
> `ValidateArtifactManifestTask.run()` checks `reject_exploratory_promotion()`
> only when `ctx.manifest.get("provenance_dir")` is truthy. An
> experiment-derived result can therefore be promoted simply by omitting
> `provenance_dir`; no schema, registry, or publication path requires an
> immutable relation between the artifact and the producing run. A local
> filesystem path is also not durable provenance for a registry artifact.
>
> Make provenance a required, typed lineage record at artifact
> publication/registration, not an optional field checked late in
> validation. It should bind the artifact to an immutable run bundle/manifest
> digest, and the producer must write it rather than accept it from
> arbitrary caller input. Validation must resolve that record and reject
> `EXPLORATORY_ONLY` deterministically. Add an end-to-end negative test: an
> artifact produced from a marked experiment but missing or falsifying
> provenance must be rejected, not accepted.

Codex was right on both counts: (1) `if provenance_dir:` meant a manifest
that simply omitted the key skipped exploratory-status checking entirely,
and (2) `reject_exploratory_promotion()` treated a MISSING marker file as
"not exploratory, proceed" — a second layer of the exact same bypass (a
caller could point `provenance_dir` at an empty/fake directory).

### Fix

**Provenance is now a REQUIRED, typed field** (`verify_artifact_provenance`,
called unconditionally from `ValidateArtifactManifestTask.run()` — no more
`if`). Every candidate manifest must carry `"provenance": {"kind": ...}`
with `kind` drawn from a closed, 2-value allowlist (`PROVENANCE_KINDS`):

* **`"experiment"`** — the artifact derives from a registered
  `run_sim_104.py`-style experiment/sim run. Requires `dir` (the run's
  output directory) and `registry_index_path` (the immutable, git-tracked
  manifest-registry index this same PR already built for the experiment
  side — `verify_manifest_registered`/`experiments/manifests/INDEX.json`).
  Verification, in `reject_exploratory_promotion()` (rewritten, not just
  re-called):
  1. The classification marker (`_experiment_classification.json`) must
     EXIST at `dir` — a missing marker now raises instead of silently
     passing (closes the second bypass layer Codex's review asked me to
     check for specifically).
  2. The marker's own `manifest_digest`/`experiment_id` are cross-checked
     against `verify_manifest_registered()`. A GENUINELY REGISTERED
     experiment manifest is rejected UNCONDITIONALLY, regardless of what
     the marker's own (filesystem-writable, hence falsifiable)
     `classification` field claims — registration, not the mutable marker
     value, is the tamper-resistant ground truth, because every registered
     experiment manifest is EXPLORATORY_ONLY by construction
     (`verify_and_classify_experiment` in `run_sim_104.py` never writes any
     other classification for a registered manifest). This specifically
     closes "falsify the classification field, keep a real registered
     digest" — the exact bypass variant the review asked for a negative
     test on.
  3. If NOT registered and the marker does not itself self-report
     `EXPLORATORY_ONLY`, the record is rejected as ambiguous/unverifiable —
     once a caller has affirmatively claimed experiment provenance, an
     unverifiable claim is treated as unsafe, not safe.
* **`"none"`** — an explicit, narrow, auditable declaration that this
  artifact was NOT produced by a registered experiment/sim run (e.g.
  ordinary model-factory training artifacts, which never run through
  `run_sim_104.py` experiment mode and carry their own, separate WF-gate
  evidence contract — `validate_panel_artifact_contract`/
  `validate_model_evidence_contract` — enforced upstream by the producer
  before this validator runs). This is the narrow allowlist exemption the
  review explicitly permits ("If there's a legitimate exemption, it must be
  an explicit, narrow, auditable allowlist — not 'absence of the field
  defaults to pass'"). **Honestly documented residual limit:** `kind="none"`
  is self-declared with no cryptographic binding to producer identity — the
  same residual-trust status as this codebase's other self-declared
  evidence fields (`code_commit`, `config_fingerprint`). What it DOES fix is
  the specific bypass Codex found: silent OMISSION of the whole
  `provenance` field is no longer possible; every manifest must make an
  explicit, git-reviewable act either way.

**Immutable binding, producer-written.** `build_experiment_provenance_reference()`
is the ONE function that constructs a `provenance` reference (mirroring the
`model_content_sha256` triple-impl-avoidance idiom this module already
follows). `run_sim_104.py`'s `verify_and_classify_experiment()` calls it
right after writing the classification marker, using the output directory
and registry-index path it already resolved as the producer — never
accepting them as free-form caller input. Any future code that builds an
artifact manifest from a registered-experiment run's output must call this
same helper rather than hand-rolling the dict shape.

### Cross-repo impact — READ BEFORE PINNING

`provenance` being required is a **breaking change** to the
`validate_artifact_manifest` contract for every existing caller. Verified
callers today (`renquant-model`'s `renquant_model_gbdt`/
`renquant_model_patchtst` `BuildArtifactManifestTask`, and
`renquant_pipeline.inference`) do **not** currently set `provenance` at all
— their manifests will start raising `ValueError` the moment this package
is upgraded, until they add `"provenance": {"kind": "none"}` (or the
`"experiment"` form, if applicable) to their manifest construction. This
repo's own pre-existing fixtures needed the same one-line update
(`tests/test_artifact_registry.py`, `registry/example-artifact.json`) —
see those diffs for the exact shape.

**Recommendation:** merge this PR to `main` (it's correct and the review
is resolved), but do NOT bump the `renquant-artifacts` pin in
`RenQuant/subrepos.lock.json` (or any other consumer's lock) until
`renquant-model`'s `BuildArtifactManifestTask` is updated to declare
`provenance`. Codex's issue-comment on RenQuant#471 (2026-07-14T16:42:46Z)
independently flagged the same ordering concern for THAT PR's dependency on
this one ("Merging #471 before artifacts#24 plus an umbrella pin update
produces an import failure..."). Required order: merge/fix artifacts#24 →
patch renquant-model's manifest builder → update the umbrella pin → merge
RenQuant#471 → materialize a clean pinned checkout and re-run the
integration tests.

## Tests (round 2)

93 tests pass in this repo (0 regressions in the properties already
proven): `tests/test_experiment_registry.py` grew from 34 to 48 tests (new
`TestProvenanceBypassClosed` class with 8 tests proving the exact
missing/falsified-provenance negative-test scenarios Codex asked for, plus
updated `TestPromotionBoundaryIntegration`/`TestClassificationAndRejection`
tests reflecting the new required-provenance / fail-closed-on-missing-marker
semantics — each renamed test carries a docstring explaining what changed
and why). Full local suite: 93 passed (up from 85; net +8 from the new
class, with pre-existing tests' construction updated in place rather than
counted as new).

Companion repo (RenQuant, umbrella): `tests/test_run_sim_104_config_resolution.py`
updated (2 tests renamed to assert the new fail-closed semantics, 2 new
end-to-end negative tests added: omitted-provenance and
falsified/decoy-directory provenance against the REAL
`renquant_artifacts.validate_artifact_manifest`) — 105 tests pass across
the 4 F-7-relevant test files (was 103). Full umbrella suite run twice
against an UNMODIFIED baseline (same environment) to establish the noise
floor: 76 failed/35 errors, then 64 failed/35 errors on a second identical
run of the SAME code — confirming this suite's `pytest -n auto` run is
order/worker-dependent and noisy independent of any code change (documented
by the prior r7 round too). The branch run (93 failed/1 error) diffed
against both baseline runs by test name: zero new failures in any file this
PR touches (`run_sim_104.py`, `test_run_sim_104_config_resolution.py`,
`test_resolve_strategy_config.py`) or in `renquant_artifacts` itself; every
differing test name is pre-existing flakiness (walkforward-loader/
wf-loader-fingerprint-dispatch/umbrella-gates-ledger tests that also flip
between the two baseline runs of identical code).

## Round 3 (same date): `kind="none"` was still a direct bypass

**Trigger:** Codex follow-up review on this PR (#24), quoted verbatim:

> **[P1] `provenance.kind="none"` remains a direct bypass of the experiment
> gate.**
>
> The required field closes omission, but it does not establish
> producer-written lineage: an artifact built from a registered experiment
> can set `{"kind": "none"}` and `verify_artifact_provenance()` returns
> immediately. `run_sim_104.py` only logs the reference returned by
> `build_experiment_provenance_reference()`; it does not emit an artifact
> manifest or bind that reference into the registry publication path. The
> required negative test is therefore still missing: create a real
> registered experiment output, construct its candidate artifact with
> `provenance={"kind": "none"}`, and prove validation rejects it. It
> currently accepts.
>
> Fix the ownership boundary rather than strengthening another
> caller-supplied dict. Artifact publication/registration must derive and
> persist provenance from an immutable producer run bundle (run ID +
> content digest / immutable URI), and validation must resolve that record.
> A legitimate non-experiment artifact needs a verified producer class and
> its own evidence contract, not an unverified `none` assertion. `dir` and
> `registry_index_path` are local mutable paths, so they also cannot be the
> durable registry identity.
>
> This remains a breaking cross-repo migration: update the model
> artifact-manifest producer first, then merge artifacts, bump the umbrella
> pin, and materialize a clean integration checkout. Do not merge or pin
> #471 until that chain proves both experiment rejection and normal model
> publication.

Codex was right: round 2 closed silent OMISSION, but `kind="none"` itself
was still an unconditional, unverified pass — `verify_artifact_provenance`
returned immediately for it with zero inspection of anything.

### Where the real producer(s) actually live (investigated first)

Neither `renquant-artifacts` (this repo — read/validate only,
`registry.py`'s `load_artifact_manifest`/`resolve_artifact_manifest` never
WRITE a manifest) nor `run_sim_104.py` (RenQuant, sim-only — produces
APY/Sharpe/MaxDD, never emitted an artifact manifest at all before this
round) is the real manifest producer. It is
**`BuildArtifactManifestTask.run()`** in
`renquant-model/src/renquant_model_gbdt/pipelines.py` (and its twin
`BuildPatchTstArtifactManifestTask.run()` in
`renquant_model_patchtst/pipelines.py`) — confirmed by reading the code:
both already call `validate_panel_artifact_contract`/
`validate_model_evidence_contract` (strict) against the trained artifact
BEFORE building the manifest dict, then call
`renquant_artifacts.validate_artifact_manifest(manifest)`. This is exactly
the "verified producer class... its own evidence contract" the review
describes — it already exists and is already enforced upstream by the
real producer; it just wasn't wired into the provenance check itself. A
secondary, ad hoc producer (`renquant-model/experiments/
gbdt_scratch_from_archived_20260528/promote_candidate.py`) hand-builds an
equivalent manifest and writes directly into this repo's `registry/`
directory; it lives on an unmerged branch (`feat/g4-score-backfill`) and is
flagged as a known follow-up rather than touched directly (out-of-scope
surgery on another in-flight branch).

### Fix: producer-bound, on-disk-verifiable `kind="none"`

`verify_artifact_provenance()` now takes the **full candidate manifest**
(not just the `provenance` sub-dict) so a `kind="none"` claim can be
checked against the manifest's OWN real identity fields —
`local_artifact_path` / `artifact_path` / a `file://` `uri` — the same
fields `BuildArtifactManifestTask` and `promote_candidate.py` already set
for the artifact to be locatable at all, not fields invented for this
check. New `_verify_none_provenance()`: if any of those fields resolves to
a real, on-disk directory that itself (or a bounded ancestor) carries a
genuine `_experiment_classification.json` marker, the `kind="none"` claim
is provably false and `reject_exploratory_promotion()` is called on it
unconditionally — reusing the SAME self-report/registration logic already
built for `kind="experiment"`, not a second implementation.

**Honestly disclosed residual limit** (same status as the round-2 `"none"`
limit): an artifact whose ONLY identity is an opaque `store://`/`object://`
reference with no locally-resolvable path gives this check nothing on disk
to inspect, so it still passes. This is materially narrower than the prior
gap (which accepted EVERY `kind="none"` unconditionally, including one
built directly from a real, locally-visible experiment output directory)
and is not silently claimed to be fully closed.

I deliberately did NOT make `kind="none"` require re-running
`validate_model_evidence_contract`/`validate_panel_artifact_contract`
against every manifest — that would force EVERY existing non-panel/model
registry entry (calibrators, crypto diagnostic placeholders, hand-curated
governance records already committed under `registry/*.json`) through a
strict WF-gate evidence check they were never designed to carry, which is
a much larger, unrelated blast radius than the bypass this round closes.

### The exact negative test Codex asked for (before/after proven)

`tests/test_experiment_registry.py::TestProvenanceBypassClosed::test_provenance_kind_none_over_registered_experiment_output_rejected`:
writes a REAL `_experiment_classification.json` + registers it in a real
`INDEX.json` (same fixtures round 1/2 already use), then builds a candidate
manifest whose `local_artifact_path` points at that real experiment
output's directory but declares `provenance={"kind": "none"}`. Proven via
`git stash` before writing the fix: **this manifest was accepted**
(`DID NOT RAISE`) against the pre-fix code — the live, reproducible gap
Codex described. After the fix: rejected with a message naming the real
classification record found. Two more tests
(`test_provenance_kind_none_via_artifact_path_or_file_uri_also_rejected`,
`test_provenance_kind_none_over_opaque_store_uri_has_nothing_to_check`)
cover the other two identity fields and the honestly-disclosed residual
limit respectively.

### Cross-repo sequencing (per Codex's explicit order)

1. **renquant-model first** (branch `fix/f7-provenance-none` off `main`):
   `BuildArtifactManifestTask`/`BuildPatchTstArtifactManifestTask` now set
   `"provenance": {"kind": "none"}` on every manifest they build — the
   honest, git-reviewable act round 2 already required, now backed by the
   round-3 on-disk check. Proven this is the correct migration order by
   running renquant-model's FULL suite (796 tests) against THIS branch's
   fixed `renquant_artifacts` (via `ARTIFACTS_SRC=<this-worktree>/src make
   test`): all 796 pass. Then reverted just the renquant-model provenance
   fix and re-ran against the SAME fixed `renquant_artifacts` — 4 tests
   fail with `ValueError: artifact manifest is missing a required
   'provenance' record`, proving the breaking-migration risk Codex warned
   about is real and that fixing the producer first is necessary.
2. **renquant-artifacts** (this PR): the fix described above.
3. **RenQuant** (branch `g3/f7-sim-pinned-config`, r9): new
   `write_candidate_artifact_manifest()` — the missing connection Codex's
   round-3 review named directly ("does not emit an artifact manifest or
   bind that reference into the registry publication path"). Called right
   after `run_backtest()` completes for an `EXPLORATORY_ONLY` run; writes
   `<output_dir>/candidate_artifact_manifest.json` with `provenance` baked
   in via `build_experiment_provenance_reference()` at the source (not a
   later, disconnected caller). New tests prove: (a) the written manifest's
   `provenance` matches the canonical reference exactly, (b) the real
   `validate_artifact_manifest` correctly refuses to promote it
   (EXPLORATORY_ONLY, as it should), and (c) a dishonest hand-built
   manifest that copies this SAME output's `local_artifact_path` but lies
   `kind="none"` is rejected by this round's fix.
4. **Umbrella pin bump**: NOT done — none of these 3 PRs are merged yet
   (branch protection requires Codex's separate approval; I am not
   authorized to merge). This is the explicit follow-up once all three
   land, in the order above.

### Integration verification actually performed (paired worktrees, not a live pin bump)

Could not materialize a real "clean pinned checkout" (nothing is merged),
so I paired isolated git worktrees instead, matching established practice
for cross-repo dependent PRs this session:

* `renquant-model` worktree (branch `fix/f7-provenance-none`) run against
  THIS branch's `renquant-artifacts` via `PYTHONPATH`/`ARTIFACTS_SRC`
  override — 796 passed, 0 regressions (proves normal model publication).
* This repo's own suite — 96 passed (was 93; net +3 new tests, 1 renamed).
* RenQuant worktree (branch `g3/f7-sim-pinned-config`, r9) run against THIS
  branch's `renquant-artifacts` the same way — 49/49 in
  `test_run_sim_104_config_resolution.py` (was 46), 116/116 across the 5
  F-7-adjacent test files. Full umbrella suite (`pytest -n auto`, ~22k
  tests): 93 failed/2 errors on the branch vs. 73 failed/2 errors then
  93 failed/2 errors (different set!) across two identical baseline runs
  of UNMODIFIED code — same order/worker-dependent noise floor round 2
  already documented. Diffed by test name against both baselines: zero
  overlap with any file this round touches; every differing test name
  (`test_shadow_scoring.py`, `test_umbrella_gates_ledger.py`,
  `test_sim_pipeline_smoke.py`, `test_training_modules.py`, etc.) passes
  100% in isolation (`pytest <file>` alone, no xdist) — confirmed directly
  as an extra check this round, not merely inferred from the baseline diff.

**What still needs a follow-up integration pass:** once renquant-model#TBD,
this PR, and RenQuant's r9 push all have Codex approval and merge, the
umbrella `subrepos.lock.json` pins for `renquant-model` and
`renquant-artifacts` need bumping together (never independently, per the
cross-repo impact note above), and a fresh `pip install` of the pinned
versions (not worktree `PYTHONPATH` overrides) should re-run both suites
once more as the final real-pin proof.
