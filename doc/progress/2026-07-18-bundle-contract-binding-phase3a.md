# Bundle contract binding — phase 3 PR-A (GOAL-5 AC4)

Date: 2026-07-18
Spec: RFC "transactional artifact bundles for the 104 serving pair"
(RenQuant#492, `doc/design/2026-07-17-artifact-bundle-transactionality.md`
§2.5 call sites, §5 ownership). Phase 1 = renquant-artifacts#25 (the
`pair_validator` seam), phase 2 = renquant-pipeline#206 (the public
`renquant_pipeline.bundle_contract.validate_pair` API + fixture vectors)
— both MERGED. This PR binds the seam to the API. Library + tests only;
nothing touches the live production store (migration stays gated on the
RFC §3 census).

## Delivered

- `src/renquant_artifacts/bundle_contract_binding.py` (new, thin adapter
  — no validation logic of its own):
  - `create_pair_validator(*, accept_legacy_stamps=None)` — lazily
    imports `renquant_pipeline.bundle_contract` INSIDE the function
    (RFC §5: pipeline is a peer dependency at publish time ONLY; the
    artifacts package must stay importable — reader/GC/schema surfaces
    fully usable — without pipeline installed). Absent pipeline it raises
    the dedicated fail-closed `PairValidatorUnavailableError`.
    `accept_legacy_stamps` threads the M6 migration-window flag through
    to `validate_pair` (phase-2 recorded ambiguity 1: writer tools with a
    resolved strategy config pass its value; `None` = the contract's own
    runtime-window default, never looser than serve-time acceptance).
  - `create_default_store(root, *, accept_legacy_stamps=None, **kwargs)`
    — the DEFAULT publishing configuration: `BundleStore` with
    `pair_validator=validate_pair` wired at writer step 6. Refuses a
    caller-supplied `pair_validator` (a custom validator is not the
    default store; call `BundleStore` directly and say so). Fail-closed
    at construction, not first publish.
- Test plumbing for the peer dep: pytest `pythonpath` + Makefile gain
  `../renquant-pipeline/src`; CI checks out renquant-pipeline as a
  sibling (checkout only, NOT pip-installed — `bundle_contract` is
  import-light per its pinned phase-2 contract, so the sibling path +
  installed renquant-common suffice, and the heavy pipeline runtime deps
  stay out of this repo's CI).

## Fixture consumption choice (stated per the phase-3 task)

The integration tests consume the phase-2 vectors by READING
`tests/fixtures/bundle_contract/vectors.json` from the sibling
renquant-pipeline checkout (located via the imported module's src-layout
path), NOT by copying the file. Rationale: this repo's cross-repo test
convention is sibling checkouts (pytest `pythonpath` entries + CI sibling
checkout — same mechanism renquant-pipeline#206 already uses in the
reverse direction to import this repo's `BundleStore`), and the vectors'
canonical home is about to become renquant-common (phase-3 PR-B); an
interim copy here would create a third divergent copy of the contract.
If pipeline is importable but the fixture is missing, the tests FAIL
(broken checkout), never skip; only a wholly absent sibling skips, and CI
always provides it.

## Verification

11 new tests (`tests/test_bundle_contract_binding.py`); full suite 188
passed (177 before), `make doctor` clean:

- subprocess with `renquant_pipeline` blocked at the import machinery:
  `import renquant_artifacts` succeeds; `create_pair_validator()` and
  `create_default_store()` raise `PairValidatorUnavailableError`.
- factory verdicts are seam-compatible (`.ok` truthy/falsy per the
  phase-1 rule) on the matching + mismatched vectors.
- publish-ACCEPT through `create_default_store` on BOTH matching-pair
  vectors (legacy + v1 schema): generation 1, resolvable via
  `resolve_active`, member bytes intact.
- publish-REJECT on `mismatched_pair` / `missing_binding` /
  `cross_schema_comparison_refused`: `BundleValidationError` carries the
  vector's expected reason code; staged bundle dir deleted under lock; no
  ACTIVE pointer, no PREPARE record — store as if publish never ran.
- incident-shape test: good pair ACTIVE at gen 1 → orphaned-binding pair
  REFUSED → ACTIVE/archive untouched → next valid pair lands as gen 2
  (the 05-27/06-22/07-01/07-14→16 class is stopped BEFORE the flip).
- `accept_legacy_stamps=False` end-to-end: the legacy matching pair
  becomes a `version_gap` publication REJECT (publication is never looser
  than serve time); the v1 pair is unaffected.
- the factory-built store still enforces phase-1 authorization checks
  (`tool="hand-edit"` refused).

## Not in this PR

- Promoting the vectors to renquant-common (phase-3 PR-B) and switching
  this repo + pipeline to consume them from there (follow-up after PR-B).
- Orchestrator run-bundle binding fields (phase-3 PR-C, RFC §2.2).
- Production writer tools invoking `create_default_store` on the live
  store — migration is RFC §3, census-gated, explicitly not this phase.
- Pinning the manifest `bindings` block content against member content
  (validate_pair v1 scope note; needs an RFC-level decision on the
  binding digest basis).
