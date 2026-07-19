# Append-only + content-addressed canonical INDEX invariant + producer allow-list

Date: 2026-07-19
Author: hallovorld
Issue: renquant-artifacts#31 (precondition of the merged F-7 protocol
RenQuant#516 / PR #517, design
`doc/design/2026-07-18-f7-516-canonical-provenance-state-machine.md`).
Scope: `renquant-artifacts` registry only. No pin advance; code lands behind
review, does not deploy ([[artifacts-pin-gate]]).

## Problem — #29 landed the record shape, not the whole-index invariant

renquant-artifacts#29 (commit `0b67302f`) added the canonical publication
record SHAPE: `write_canonical_run_intent`, `register_canonical_publication`,
`CanonicalPublicationSnapshot`, `verify_canonical_publication_snapshot`, and a
per-record content address (`<run_intent_digest hex>.json`). But the F-7 state
machine's rollback-safety (design §2, §4) rests on the registry `INDEX.json`
being **append-only + content-addressed as a whole**, and #29 enforced that
only for the single `artifact_digest` being written:

- The per-artifact rebind check (`register_canonical_publication`) refused
  rebinding one artifact to a different run-intent, but did **not** verify the
  rest of the index before appending. A prior entry that had been mutated,
  rebound in place, had its record tampered, or named a non-allow-listed
  producer would be **laundered forward** into a fresh committed index by the
  next otherwise-legitimate append (register re-serializes the whole index).
- There was no independent, machine-verifiable checker for the committed
  registry (design §4/§5 promise: "entries are content-addressed so any commit
  is machine-verifiable").

Design §5 flagged exactly this open item: "#29 provides the record shape but
the append-only INDEX invariant must be verified/added."

## What this adds (`canonical_registry.py`)

1. **`verify_canonical_index_integrity(publications_dir)` (new public API)** —
   a whole-index audit: every `INDEX.json` entry must be content-addressed (its
   persisted record exists, its recomputed sha256 equals the indexed
   `run_intent_digest`, and its filename IS that digest's content-addressed
   name) and its record must pass intrinsic verification (schema +
   `CANONICAL_PRODUCERS` allow-list + evidence fields). Returns one error per
   offending entry; an absent/empty index is not a violation. This is the
   checker CI / a pre-commit hook runs over the committed registry, and the
   fail-closed precondition register applies. Exported from the package
   (`__init__.py` + `__all__`).

2. **`register_canonical_publication` now enforces the invariant before it
   appends** (fail-closed):
   - runs `verify_canonical_index_integrity` over the existing store first — a
     store that already violates the invariant is refused, so a legitimate
     append never launders a corrupt/tampered prior entry forward;
   - asserts the persisted index is append-only relative to what was read
     (`_index_append_only_errors`: may only ADD the new digest, never drop or
     rewrite an existing entry);
   - keeps the existing per-artifact semantics: identical re-registration is an
     idempotent no-op, a rebind to a different run-intent raises.

3. **Same-host concurrent-append safety** — the `INDEX.json`
   read-modify-write is serialized by a POSIX advisory lock on the store
   directory's own fd (`_index_write_lock`). No lock file is created, so
   nothing pollutes the committed registry; degrades to a no-op where `fcntl`
   is unavailable. The registry's authoritative cross-machine concurrency
   control remains git's append-only, branch-protected history — `main` has no
   direct producer write path; a publication lands only as a reviewed commit.

Content-addressing bounds what the in-process check can prove: it binds
`run_intent_digest <-> record bytes` and refuses any record tamper by
recomputation alone. An out-of-band *removal* of an entry, or a mutation of
non-addressed metadata (`artifact_uri`/`registered_at`), is caught by git diff
at review time, not in-process — this is by design (the registry is a
git-committed, branch-protected, append-only store; §2 rollback re-activates a
prior pinned generation and never mutates in place). `register` additionally
never itself drops or rewrites a prior entry (asserted + tested).

## Tests — `tests/test_canonical_index_invariant.py` (16 new, independent)

Valid append is content-addressed; a second distinct append preserves the
first; idempotent replay is a no-op; rebinding an artifact is rejected; an
append onto an in-place-mutated record / rebound index entry is refused; a
non-content-addressed record, a record-filename/digest mismatch, a
non-allow-listed producer entry, a malformed digest-missing entry, and a
missing record file are each flagged by the verifier (and block a subsequent
append); the append-only relation helper flags removal and mutation;
concurrent appends from 8 threads lose no entry; and the end-to-end
candidate -> verified append -> committed pinned checkout both resolves the
binding and passes the whole-index audit (with no lingering runtime lock file
in the pinned tree).

## Evidence

Artifacts suite: **326 passed** (310 baseline at `0b67302f` + 16 new), via
`make test` (relative sibling checkouts) and direct pytest; `make doctor` ok.
Python 3.10, `renquant-common` + `renquant-pipeline` sibling checkouts per CI.

## Scope boundary / follow-ups (out of scope here, per design §4/§6)

- The producer hook that writes the run-intent at seal time and binds the full
  canonical **pair identity** (`bundle_id` + manifest digest + member digests,
  design §2) rides AC4 bundle-seal and sequences after AC4 P1 cutover
  (design §6) — it is not this registry-invariant PR. The index currently keys
  by `artifact_digest` (the manifest's own fingerprint); extending the entry to
  carry the bundle pair identity is additive and lands with that producer, when
  its values exist to bind.
- The admission adapter (orchestrator) that resolves the snapshot and calls
  validate is the separate independent piece (design §4).
- No `renquant-artifacts` pin advances on this PR.
