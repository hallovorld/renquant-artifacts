# AC4 migration P0: break-glass operational against the declared real store location

Date: 2026-07-18
Spec: census RenQuant `doc/design/2026-07-18-ac4-migration-census.md` §6
P0 ("build, no live change"; break-glass closes blocker B2's tool gap) +
RFC RenQuant#492 `doc/design/2026-07-17-artifact-bundle-transactionality.md`
§2.4 (mandatory incident-ref, ALWAYS alarms via the drift sentinel) and
§3 (every phase's commit reverts cleanly, no artifact surgery).
Companion PR: umbrella RenQuant (declares `deploy/bundle_store_location.json`
+ gitignores the store paths). Library layer previously merged:
artifacts#25/#26, pipeline#206, common#32, orch#547.

## What P0 still lacked (census P0 vs merged state)

The census P0 items (publisher + operation log + `bundle_breakglass`,
`validate_pair`, contract fixture, kill-injection CI) were all MERGED as
libraries — but `bundle_breakglass` demanded a hand-typed `--store-root`
(no declared real location existed anywhere) and its §2.4 always-alarm
stopped at stderr (phase-1 progress doc: "drift-sentinel binding is a
later phase"). A break-glass tool nobody can point at the real store
does not close B2. This PR is that operationalization; it adds NO new
protocol semantics.

## Delivered

- `bundle_store_location.py` — resolution of the DECLARED real store
  root: explicit `--store-root` > `RQ_BUNDLE_STORE_ROOT` env >
  the umbrella's reviewed `deploy/bundle_store_location.json` under
  `$RQ_ROOT` (default = the live umbrella checkout). Fail-closed
  (`StoreLocationError` naming the file and both overrides); NO silent
  built-in fallback path; every resolution carries a `source` string
  that tools echo.
- `bundle_store_init.py` — idempotent, additive-only stand-up of the
  store skeleton: creates at most `<root>`, `bundles/`, `bundles/.lock`.
  NEVER creates `ACTIVE` or `OPERATIONS.jsonl` (those are the first
  publication's job — the P1 seal) and never opens any sibling file, so
  the flat pair alongside is untouched by construction. Refuses missing
  parent dirs and non-local mounts (RFC §2.1 guard, injectable).
- `bundle_alarms.py` — `create_sentinel_alarm_hook()`: the §2.4 alarm
  bound to the REAL drift-sentinel channel. The orchestrator sentinel
  (`ops/run_surface_drift_check.py`) alarms via
  `renquant_common.notify.send` with the topic from `$RQ_ROOT/.env`
  (`ops/liveness_common.py:alert`); renquant-common is an install
  dependency of this package, so the binding is in-boundary — no
  orchestrator import. Hook contract: stderr `ALARM[...]` record ALWAYS
  first; then one ntfy send (never raises, honors `RENQUANT_NO_NOTIFY`);
  unimportable renquant-common degrades to a loud stderr warning.
- `bundle_breakglass.py` — `--store-root` now optional (declared-location
  resolution, provenance echoed in the result JSON), alarm hook =
  sentinel binding, tool_version 1.1.0. Incident-ref mandatory semantics
  unchanged.
- `tests/conftest.py` pins `RENQUANT_NO_NOTIFY=1` suite-wide (tests must
  never send live notifications; wiring tests monkeypatch `send`).

## Verification

37 new tests; full suite **225 passed** (188 before), `make doctor`
clean [VERIFIED, this branch, scratch clone]:

- resolution precedence + provenance strings; fail-closed on
  missing/malformed/unknown-field/wrong-version declarations.
- store-init idempotence (second run creates nothing; byte-identical
  tree), zero-serving-change alongside a fake flat pair + side files
  (only `bundles/.lock` added, every pre-existing byte identical),
  refusal on missing parent / non-dir collision / non-local mount, init
  over a published store changes nothing and serving state stays gen 1.
- break-glass CLI end-to-end against env- and declaration-resolved roots
  (no `--store-root`); fail-closed when nothing resolves; commit alarm
  reaches the sentinel channel (`notify.send` captured: title
  `[bundle-store] breakglass_commit`, incident-ref in body, env_file =
  `$RQ_ROOT/.env`) while the stderr record is retained.
- revert-cleanliness import-graph (RFC §3): the three new modules are
  imported ONLY by the break-glass CLI + package facade; the pre-P0
  serving/provenance modules (`contracts`, `registry`, `validation`)
  import no bundle code. Reverting this PR removes exactly the new
  modules + break-glass edits.

## Rollback invariant (RFC §3)

`git revert` of this PR restores: break-glass requiring an explicit
`--store-root`, stderr-only alarms, no location/init modules. No serving
surface reads any of this code (import-graph test above), so serving
behavior is bit-identical before/after/reverted. No artifact surgery:
nothing in this PR writes any production path; if the landing step had
already initialized the real store directory, the skeleton
(`bundles/.lock` only) is inert to every reader in the census and may be
removed with `rm -rf <prod>/bundles` (a landing-revert step, not
artifact surgery — the flat pair is never touched).

## Landing steps (NOT in this PR — ask-first per landing policy)

1. Sync the machine's renquant-artifacts checkout to this merge
   (merged-is-not-deployed).
2. After the umbrella declaration PR lands + syncs:
   `python -m renquant_artifacts.bundle_store_init` (idempotent; creates
   only `bundles/` + `.lock` alongside the flat pair; run under the
   operator's ask-first grant).
3. Verify: flat-pair sha256 unchanged; drift scan reports the new
   untracked store dir as info-only (it is gitignored by the umbrella
   companion PR).

## Orchestrator-side follow-up (documented, not reached across)

The sentinel binding here is the outbound alarm channel. Defense in
depth — the drift sentinel ALSO reading `OPERATIONS.jsonl` (surfacing
breakglass/RECOVERY records in its daily scan) — is orchestrator-owned
and belongs to P1+ alongside the run-surface manifest entry for the
store; tracked in the umbrella companion progress doc, not implemented
here.
