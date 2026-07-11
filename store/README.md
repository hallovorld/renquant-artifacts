# store/ — pinned artifact store (D6-§2a shadow-ab experiment)

Byte-copies of the artifacts the two-arm shadow experiment consumes, made
2026-07-10 from the production store (verified: model.pt content fingerprint
matches the live artifact, sha256 prefix 0704696399…). This directory is the
target of the orchestrator run-manifest `artifact_store: {repo, path}` binding
— consumers resolve store-addressed config refs (`…/artifacts/<rest>`) against
`<this repo>/store/<rest>` AFTER the runner verifies this checkout's commit
and clean tree, so blob identity is bound by the repo pin plus per-artifact
fingerprint stamping.

Layout mirrors the ref remainders exactly:
- `patchtst_shadow/pt07_strict_trainfit_embargo60_20260522/seed_44/` — panel
  model + metadata + summary sidecars
- `shadow/panel-rank-calibration.….json` — global calibrator (source of
  truth was the umbrella strategy dir; NOT present in the strategy-104
  subrepo — dual-kernel divergence, audit T1)

The optional context refs (spy-gmm-regime / watchlist-correlation /
earnings-calendar) exist nowhere on the production host and are None-tolerated
arm-symmetric — deliberately not included.

Adding/refreshing an artifact = a PR to this repo + a run-manifest pin bump;
never a live-tree copy.

## Integrity mechanism

`STORE-MANIFEST.json` lists every blob with its full content sha256; CI
(`tests/test_no_large_artifacts.py`, `tests/test_store_manifest.py`) fails on
any unlisted large binary, missing blob, or content drift. Registry entries
(`registry/shadow-ab-pt07-seed44-*.json`) bind the same fingerprints to
`store://` URIs. The r1 lesson is pinned by test: the repo-global `*.pt`
gitignore silently dropped the model blob from the first commit — large-ext
files under `store/` are re-included and must be manifest-listed.
