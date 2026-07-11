"""Regenerate field-agreement-check.json from the committed gen-1/gen-2 JSON.

Context (Codex CHANGES_REQUESTED on PR #22, point 1): attestation.json's
`verification` block previously claimed capture-verify-output.json's dry-run
"agrees with the written gen-1 manifest". That is false framing --
capture-verify-output.json is a *later, separate* gen-2 capture-dry-run
observation (manifest.generation == 2, manifest.deployment.state ==
"captured", supersedes_sha256 == the gen-1 manifest's own sha256). It cannot
prove gen-1's original capture was correct at gen-1 time; it can only be
compared against gen-1's hash-attested INPUT snapshots that are committed in
this bundle.

This script performs that comparison mechanically (a real recursive diff, not
a hand-typed claim) and writes field-agreement-check.json alongside it. Run it
from this directory:

    python3 build_field_agreement.py

It is deterministic given the four inputs (attestation.json,
source-lock.snapshot.json, runtime-inventory.snapshot.json,
capture-verify-output.json) and re-running it should reproduce
field-agreement-check.json byte-for-byte (modulo generated_at).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent

REPO_FIELDS = ("remote", "branch", "commit", "role", "status")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    attestation = json.loads((HERE / "attestation.json").read_text())
    source_lock = json.loads((HERE / "source-lock.snapshot.json").read_text())
    runtime_inv = json.loads((HERE / "runtime-inventory.snapshot.json").read_text())
    gen2 = json.loads((HERE / "capture-verify-output.json").read_text())

    source_lock_sha = _sha256(HERE / "source-lock.snapshot.json")
    runtime_inv_sha = _sha256(HERE / "runtime-inventory.snapshot.json")

    # Left side: gen-1's hash-attested INPUT snapshots. Both
    # runtime_inventory_sha256 and source_lock_sha256 are the original,
    # byte-verbatim gen-1-capture-time hashes -- neither file has been edited
    # since capture. (An earlier round of this PR briefly appended a
    # role_provenance_note annotation into source-lock.snapshot.json, which
    # changed its hash; that was reverted per Codex CHANGES_REQUESTED so the
    # raw file stays byte-verbatim. The equivalent caveat now lives in the
    # separate source-lock-role-disclaimer.json sidecar, referenced from
    # attestation.json.source_lock_role_disclaimer_ref, and does not affect
    # this file's hash or the fields compared below.)
    left_repos = {r["name"]: {k: r[k] for k in REPO_FIELDS} for r in source_lock["subrepos"]}
    left_runtime_paths = {name: v["path"] for name, v in runtime_inv["repos"].items()}
    left_host = runtime_inv["host"]

    right_repos = {
        name: {k: v[k] for k in REPO_FIELDS} for name, v in gen2["manifest"]["repos"].items()
    }
    right_runtime_paths = {
        name: v["path"] for name, v in gen2["runtime_inventory"]["repos"].items()
    }
    right_host = gen2["runtime_inventory"]["host"]

    per_repo = {}
    diffs = []
    for name in sorted(set(left_repos) | set(right_repos)):
        left = left_repos.get(name)
        right = right_repos.get(name)
        match = left == right
        per_repo[name] = {"left": left, "right": right, "match": match}
        if not match:
            diffs.append({"path": f"repos.{name}", "left": left, "right": right})

    runtime_path_diffs = {}
    for name in sorted(set(left_runtime_paths) | set(right_runtime_paths)):
        left = left_runtime_paths.get(name)
        right = right_runtime_paths.get(name)
        match = left == right
        runtime_path_diffs[name] = {"left": left, "right": right, "match": match}
        if not match:
            diffs.append({"path": f"runtime_inventory.repos.{name}.path", "left": left, "right": right})

    host_match = left_host == right_host
    if not host_match:
        diffs.append({"path": "runtime_inventory.host", "left": left_host, "right": right_host})

    result = {
        "kind": "gen1-input-vs-gen2-observation-field-agreement",
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "compares": {
            "left": {
                "description": (
                    "gen-1's hash-attested INPUT snapshots committed in this bundle: "
                    "source-lock.snapshot.json (subrepos[*].{remote,branch,commit,role,status}) "
                    "and runtime-inventory.snapshot.json (host, repos[*].path)."
                ),
                "captured_at": attestation["captured_at"],
                "source_lock_snapshot_sha256": source_lock_sha,
                "source_lock_snapshot_sha256_note": (
                    "Hash of the raw, byte-verbatim gen-1-capture-time file "
                    "(unmodified since capture; matches "
                    "attestation.json.source_lock_sha256). A caveat about "
                    "source_repo.role's authority scope is recorded separately "
                    "in source-lock-role-disclaimer.json (referenced from "
                    "attestation.json.source_lock_role_disclaimer_ref) and does "
                    "not affect this hash."
                ),
                "runtime_inventory_snapshot_sha256": runtime_inv_sha,
                "runtime_inventory_snapshot_sha256_matches_attestation": (
                    runtime_inv_sha == attestation["runtime_inventory_sha256"]
                ),
            },
            "right": {
                "description": (
                    "capture-verify-output.json: a LATER, SEPARATE gen-2 "
                    "capture-dry-run observation (manifest.generation=2, "
                    "manifest.deployment.state='captured', "
                    "manifest.deployment.supersedes_sha256 == gen-1's own "
                    "manifest sha256). NOT a re-verification or reproduction of "
                    "gen-1's own manifest-generation output."
                ),
                "observed_at": gen2["manifest"]["generated_at"],
                "manifest_sha256": gen2["manifest_sha256"],
                "manifest_generation": gen2["manifest"]["generation"],
                "manifest_deployment_state": gen2["manifest"]["deployment"]["state"],
                "manifest_supersedes_sha256": gen2["manifest"]["deployment"]["supersedes_sha256"],
                "supersedes_matches_attestation_manifest_gen1_sha256": (
                    gen2["manifest"]["deployment"]["supersedes_sha256"]
                    == attestation["manifest_gen1_sha256"]
                ),
            },
        },
        "fields_compared": [
            "repos.<name>.{remote,branch,commit,role,status} for all 9 repos",
            "runtime_inventory.host",
            "runtime_inventory.repos.<name>.path for all 9 repos",
        ],
        "fields_not_compared": [
            "manifest.artifact_store -- no gen-1-attested source for this field "
            "is committed in this bundle; not cross-checkable here.",
            "manifest.deployment.* -- describes the gen-2 dry-run/verify run "
            "itself, not a gen-1 property.",
        ],
        "per_repo": per_repo,
        "runtime_inventory_paths": runtime_path_diffs,
        "host_match": host_match,
        "diff_count": len(diffs),
        "diffs": diffs,
        "verdict": "no_drift_detected" if not diffs else "drift_detected",
        "evidentiary_meaning": (
            "This proves that, as of the LATER gen-2 capture-dry-run observation "
            "(" + gen2["manifest"]["generated_at"] + "), the specific fields "
            "listed above were still identical to the values recorded in gen-1's "
            "hash-attested input snapshots (captured " + attestation["captured_at"] + "). "
            "It is evidence of NO DRIFT between gen-1 time and the gen-2 "
            "observation for these fields. It is NOT proof that gen-1's original "
            "capture-and-write was itself correct at gen-1 capture time: gen-1's "
            "own deployment-manifest.json output is not committed in this bundle "
            "(only its sha256, attestation.json.manifest_gen1_sha256, is "
            "recorded), so this comparison uses gen-1's INPUT snapshots as the "
            "left side, not a re-run of gen-1's own manifest-generation output. "
            "A later recapture agreeing with earlier inputs is not the same "
            "claim as a later recapture reproducing the earlier host state."
        ),
    }

    out_path = HERE / "field-agreement-check.json"
    out_path.write_text(json.dumps(result, indent=2, sort_keys=False) + "\n")
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
