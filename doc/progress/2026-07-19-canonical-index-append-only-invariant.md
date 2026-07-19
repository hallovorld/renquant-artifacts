# Append-only + content-addressed canonical INDEX invariant + producer allow-list

Date: 2026-07-19
Author: hallovorld
Issue: renquant-artifacts#31 (precondition of the merged F-7 protocol
RenQuant#516 / PR #517, design
`doc/design/2026-07-18-f7-516-canonical-provenance-state-machine.md`). This PR
delivers the registry-invariant portion; #31 stays OPEN (the pair-identity
VALUES + producer hook ride AC4 — see "sequencing").
Scope: `renquant-artifacts` registry only. No pin advance; code lands behind
review, does not deploy ([[artifacts-pin-gate]]).

## Problem — #29 landed the record shape, not the whole-index invariant

renquant-artifacts#29 (commit `0b67302f`) added the canonical publication
record SHAPE and a per-`artifact_digest` rebind refusal, but the F-7 state
machine's rollback-safety (design §2/§4) rests on the registry `INDEX.json`
being **append-only + content-addressed as a whole** — the explicit open item
in design §5. #29 enforced that only for the single `artifact_digest` being
written, so a prior entry that had been mutated / rebound / tampered / that
named a non-allow-listed producer could be **laundered forward** into a fresh
committed index by the next legitimate append.

## What is MACHINE-ENFORCED here (vs deployment-config)

Enforced by code + a required CI check (this PR):

1. **In-tree whole-index audit — `verify_canonical_index_integrity()` (new
   public API).** Every `INDEX.json` entry must be content-addressed (record
   exists, recomputed sha256 == indexed `run_intent_digest`, filename IS that
   digest's content-addressed name) and its record must pass intrinsic
   verification (schema + `CANONICAL_PRODUCERS` allow-list + evidence).
   `register_canonical_publication` runs it as a **fail-closed precondition**
   before it appends (never launders a corrupt/tampered prior entry forward),
   and asserts the persisted index is append-only vs what was read
   (`_index_append_only_errors`).

2. **Cross-COMMIT append-only — `verify_index_transition(base, candidate)`
   (new public API) + a required CI job.** The in-tree audit cannot catch a PR
   that *deletes* a valid historical entry or rewrites self-consistent
   non-content-addressed metadata (`artifact_uri` / `registered_at`) of an
   existing entry — within one checkout the "previous" is already the candidate.
   The transition verifier requires every BASE entry (and its referenced record
   file's bytes) to survive BYTE-IDENTICALLY in the candidate; only pure
   additions pass. `scripts/verify_canonical_index_transition.py` wires it into
   `.github/workflows/ci.yml` as a required step run against the PR base
   worktree. This is the machine enforcement behind "any commit is verifiable"
   — **not a human reading a diff.** (Armed and inert today: no
   `canonical_publications` store exists on main yet.)

3. **Producer allow-list, bound to record identity.** `CANONICAL_PRODUCERS` is
   enforced both in-tree (integrity) and at the promotion boundary. The
   producer field lives inside the content-addressed run-intent, so it cannot
   be swapped without changing the `run_intent_digest` the publication binds.

4. **Candidate → verified-publisher boundary — `CandidatePublication` +
   `verify_candidate_authorization()` + `promote_candidate_publication()` (new).**
   The three-principal flow (producer stages a candidate; verified publisher
   promotes; operator deploys) is now a code/CI-testable contract, not just a
   string check. A candidate is inert data written to a producer-owned STAGING
   path; `promote_candidate_publication` is the ONLY path that moves it into the
   live store, and it refuses an unauthorized/forged candidate (producer not in
   `CANONICAL_PRODUCERS`, or a tampered run-intent) **before the live store is
   touched** — the live INDEX is byte-unchanged and no record file leaks in.

5. **Same-host concurrent-append safety.** The `INDEX.json` read-modify-write
   is serialized by a POSIX advisory lock on the store dir's own fd
   (`_index_write_lock`; no lock file created, no-op fallback where `fcntl` is
   absent).

Left to **deployment config (NOT claimed as a code invariant):** that the live
`main` branch has no direct producer write path — that is GitHub branch
protection + CODEOWNERS (a producer cannot push to `main`; a publication lands
only as a reviewed commit). The code half of that boundary (the
candidate→promote gate above) is what this PR tests; the branch-protection half
is repo settings.

## Additive pair-identity schema (values arrive with the AC4 producer)

The F-7 record binds `run_intent_digest` → the exact canonical **pair
identity** (`bundle_id` + manifest digest + member digests, design §2). Those
values only exist once the AC4-seal producer hook lands (design §6), so this PR
adds the **schema field now** as an optional/nullable INDEX-entry field
(`CANONICAL_PAIR_IDENTITY_KEY = "pair_identity"`, `_verify_pair_identity_shape`,
a `pair_identity=` param on register/promote) — **present-but-null** by default,
shape-validated when populated, and covered by the append-only invariant once
written (it may never be mutated in place). The contract exists even though the
values arrive later.

## Sequencing (why #31 stays open)

- **This PR (independent, now):** the registry append-only + content-addressed
  invariant, cross-commit CI guard, producer allow-list, candidate→publisher
  boundary, and the additive pair-identity schema.
- **Deferred to the AC4-seal producer hook (design §4/§6):** POPULATING the
  pair-identity values (`bundle_id`/member digests only exist at seal). The
  admission adapter (orchestrator) that resolves the snapshot and calls validate
  is the separate independent piece.
- Therefore this PR does **not** claim `Closes #31`; #31 remains open until the
  producer populates the pair binding.
- No `renquant-artifacts` pin advances on this PR.

## Tests — `tests/test_canonical_index_invariant.py` (32, independent)

Whole-index: valid/idempotent append, second append preserves the first,
rebind rejected, append onto in-place-mutated record / rebound index entry
refused, non-content-addressed / filename-mismatch / non-allow-listed-producer
/ malformed / missing-record flagged, append-only relation helper, 8-thread
concurrent append loses nothing, candidate→append→pinned-checkout end-to-end.
Cross-commit (`TestIndexTransition`): pure append OK, empty base OK, deletion /
`artifact_uri` mutation / `registered_at` mutation / record byte-mod / record
deletion each REFUSED, and the CI guard script exit codes (0 append / 1
deletion). Verified-publisher (`TestVerifiedPublisherBoundary`): authorized
candidate promotes into live INDEX; unauthorized producer / forged run-intent
rejected with the live store byte-unchanged; the authorization envelope.
Pair-identity (`TestPairIdentitySchema`): entry carries a present-nullable
field by default, a valid binding is stored/resolved, a malformed one is
rejected, and rebinding it on an existing entry is refused.

## Evidence

Artifacts suite: **342 passed** (326 at the first revision + 16 new; 310 at the
`0b67302f` baseline), via `make test` (relative sibling checkouts) and direct
pytest; `make doctor` ok. Python 3.10, `renquant-common` + `renquant-pipeline`
sibling checkouts per CI. CI append-only guard verified locally (append→exit 0,
deletion→exit 1).
