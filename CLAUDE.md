# CLAUDE.md

Canonical operating model:
https://github.com/hallovorld/RenQuant/blob/main/doc/arch/subrepo-operating-model.md

Local repo map: `RENQUANT_REPOS.md`.

Branch policy: `main` is the stable interface consumed by other repos and
automation. Experiments, optimizations, and large upgrades happen on feature
branches, then merge back only after tests and integration checks pass.

## Repo Role

`renquant-artifacts` owns artifact manifests, metrics summaries, fingerprints,
promotion status, and object locations.

## Hard Boundaries

- Git stores searchable manifests, not large checkpoints or random experiment
  dumps.
- Accepted, shadow, candidate, diagnostic, and rejected artifacts must be
  distinguishable by manifest.
- Future agents must be able to find historical runs by artifact id, strategy,
  model family, date, promotion status, and metric keys.
- Do not train models, own broker execution, or hide prod artifacts in a model
  repo working directory.
- Large registry/schema changes use a feature branch.
- Do not delete or empty the source umbrella repo at
  `/Users/renhao/git/github/RenQuant`.

## Required Evidence

Every manifest needs artifact id, URI, SHA/fingerprint, model family,
strategy, data/config/code fingerprints, metric summary, promotion status,
owner, and retention class.

## Workflow

```bash
make test
make doctor
```
