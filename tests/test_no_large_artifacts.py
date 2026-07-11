from __future__ import annotations

import hashlib
import json
from pathlib import Path

FORBIDDEN = {".pt", ".pkl", ".db", ".parquet", ".zip"}


def _store_manifest(root: Path) -> dict[str, str]:
    manifest_path = root / "store" / "STORE-MANIFEST.json"
    if not manifest_path.exists():
        return {}
    return json.loads(manifest_path.read_text())


def test_repo_does_not_track_unmanifested_large_artifacts() -> None:
    """Large binaries are forbidden EXCEPT inside store/ — the pinned
    artifact store this repo exists to own — and there only when listed in
    store/STORE-MANIFEST.json with a matching content sha256 (verified by
    test_store_manifest.py). A blob outside store/, or inside store/ but
    unlisted, is still a policy violation."""
    root = Path(__file__).parents[1]
    manifest = _store_manifest(root)
    offenders = []
    for path in root.rglob("*"):
        if ".git" in path.parts or not path.is_file():
            continue
        if path.suffix not in FORBIDDEN:
            continue
        rel = path.relative_to(root)
        if rel.parts[0] != "store" or str(rel.relative_to("store")) not in manifest:
            offenders.append(str(rel))
    assert offenders == []


def test_store_blobs_match_their_manifest_sha() -> None:
    root = Path(__file__).parents[1]
    manifest = _store_manifest(root)
    for rel, expected in manifest.items():
        blob = root / "store" / rel
        assert blob.exists(), f"manifest lists {rel} but the blob is absent"
        actual = hashlib.sha256(blob.read_bytes()).hexdigest()
        assert actual == expected, f"{rel}: content sha drifted"
