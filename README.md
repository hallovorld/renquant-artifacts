# renquant-artifacts

Artifact-registry repository for RenQuant.

This repo tracks artifact manifests, fingerprints, metrics, promotion status,
and object locations. It does not store large model checkpoints, random
experiment dumps, live WAL files, or raw databases in normal Git.

## Pipeline Rule

Artifact validation and promotion workflows are `renquant-common`
Task/Job/Pipeline chains.

## Initial Split Source

`hallovorld/RenQuant` commit
`8f3e08d8d1ae1e402a78f4815efb59e3c7c66aa8`.

## Local Test

```bash
PYTHONPATH=../renquant-common/src:src python -m pytest -q
```
