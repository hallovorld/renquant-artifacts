"""Independent acceptance tests for the append-only + content-addressed
canonical-publication ``INDEX.json`` invariant and its producer allow-list
(renquant-artifacts#31, precondition of the F-7 protocol RenQuant#516 / #517).

The record SHAPE landed in #29 (``write_canonical_run_intent`` /
``register_canonical_publication`` / ``CanonicalPublicationSnapshot`` /
``verify_canonical_publication_snapshot``). This module adds the tests that
pin the WHOLE-index invariant the append-only store rests on:

* a valid append succeeds and is content-addressed;
* an idempotent replay is a no-op (defined behaviour);
* an in-place mutation / rebind / removal of an existing entry is refused;
* a non-content-addressed (mutated record) entry is refused;
* an entry whose persisted record names a non-allow-listed producer is refused;
* a malformed / digest-missing entry is refused;
* concurrent appends on one host do not lose an entry;
* the end-to-end candidate -> verified append -> pinned-checkout read verifies.

These exercise ``canonical_registry`` directly (the registry boundary performs
no environment/git I/O by design), plus one pinned-checkout acceptance test.
"""
from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path

import pytest

from renquant_artifacts import (
    CanonicalPublicationSnapshot,
    register_canonical_publication,
    verify_canonical_index_integrity,
    write_canonical_run_intent,
)
from renquant_artifacts.canonical_registry import (
    CANONICAL_PUBLICATIONS_INDEX_FILENAME,
    CANONICAL_CODE_PIN_SUBREPOS,
    _index_append_only_errors,
    _record_filename,
    resolve_canonical_publication,
)
from renquant_common.model_fingerprint import artifact_sha256


def _write_valid_intent(run_dir: Path, run_id: str) -> Path:
    """A run_intent.json that passes intrinsic verification (allow-listed
    producer, well-formed pins, non-empty evidence) without needing real git
    checkouts -- the registry boundary performs no environment I/O."""
    return write_canonical_run_intent(
        run_dir,
        run_id=run_id,
        run_type="daily_full",
        producer={
            "repo": "renquant-orchestrator",
            "entrypoint": "daily.TrainGbdtArtifactTask",
        },
        strategy_manifest_fingerprint="sha256:strategy",
        data_manifest_fingerprint="sha256:data",
        strategy_config_digest="sha256:strategyconfig",
        model_config_digest="sha256:modelconfig",
        calendar_universe_digest="sha256:universe",
        as_of="2026-07-18",
        code_pins={
            name: {"commit": "0" * 40, "remote": f"https://github.com/hallovorld/{name}"}
            for name in CANONICAL_CODE_PIN_SUBREPOS.values()
        },
    )


def _register(pubs: Path, run_dir: Path, run_id: str, artifact_digest: str) -> Path:
    intent = _write_valid_intent(run_dir, run_id)
    register_canonical_publication(
        pubs,
        run_intent_path=intent,
        artifact_digest=artifact_digest,
        artifact_uri=f"object://renquant-artifacts/{artifact_digest.replace(':', '_')}.bin",
    )
    return intent


def _read_index(pubs: Path) -> dict:
    return json.loads((pubs / CANONICAL_PUBLICATIONS_INDEX_FILENAME).read_text())


def _write_index(pubs: Path, index: dict) -> None:
    (pubs / CANONICAL_PUBLICATIONS_INDEX_FILENAME).write_text(json.dumps(index, indent=2))


# ── valid append + idempotent replay ───────────────────────────────────────


class TestValidAppend:
    def test_valid_append_succeeds_and_is_content_addressed(self, tmp_path):
        pubs = tmp_path / "pubs"
        intent = _register(pubs, tmp_path / "runs" / "a", "run-a", "sha256:artifact-a")
        index = _read_index(pubs)
        assert set(index) == {"sha256:artifact-a"}
        entry = index["sha256:artifact-a"]
        assert entry["run_intent_digest"] == artifact_sha256(intent)
        assert entry["record"] == _record_filename(entry["run_intent_digest"])
        assert verify_canonical_index_integrity(pubs) == []

    def test_second_distinct_append_preserves_the_first(self, tmp_path):
        pubs = tmp_path / "pubs"
        _register(pubs, tmp_path / "runs" / "a", "run-a", "sha256:artifact-a")
        _register(pubs, tmp_path / "runs" / "b", "run-b", "sha256:artifact-b")
        index = _read_index(pubs)
        assert set(index) == {"sha256:artifact-a", "sha256:artifact-b"}
        assert verify_canonical_index_integrity(pubs) == []

    def test_idempotent_replay_is_a_no_op(self, tmp_path):
        pubs = tmp_path / "pubs"
        intent = _write_valid_intent(tmp_path / "runs" / "a", "run-a")
        for _ in range(3):
            register_canonical_publication(
                pubs, run_intent_path=intent,
                artifact_digest="sha256:artifact-a",
                artifact_uri="object://renquant-artifacts/a.bin",
            )
        index = _read_index(pubs)
        assert list(index) == ["sha256:artifact-a"]
        assert verify_canonical_index_integrity(pubs) == []


# ── rebinding / mutation / removal of an existing entry are refused ─────────


class TestAppendOnlyRefusals:
    def test_rebinding_same_artifact_to_different_run_intent_is_rejected(self, tmp_path):
        pubs = tmp_path / "pubs"
        _register(pubs, tmp_path / "runs" / "a", "run-a", "sha256:artifact-x")
        intent_b = _write_valid_intent(tmp_path / "runs" / "b", "run-b")
        with pytest.raises(ValueError, match="may never be rebound"):
            register_canonical_publication(
                pubs, run_intent_path=intent_b,
                artifact_digest="sha256:artifact-x",
                artifact_uri="object://renquant-artifacts/x.bin",
            )

    def test_append_onto_in_place_mutated_record_is_refused(self, tmp_path):
        """An existing entry's persisted record is edited in place (its bytes
        no longer hash to its indexed digest). A subsequent legitimate append
        is refused fail-closed rather than laundering the tamper forward."""
        pubs = tmp_path / "pubs"
        _register(pubs, tmp_path / "runs" / "a", "run-a", "sha256:artifact-a")
        entry = _read_index(pubs)["sha256:artifact-a"]
        record_path = pubs / entry["record"]
        record = json.loads(record_path.read_text())
        record["producer"] = {"repo": "evil", "entrypoint": "evil.Task"}
        record_path.write_text(json.dumps(record))

        assert any(
            "not content-addressed" in e for e in verify_canonical_index_integrity(pubs)
        )
        intent_b = _write_valid_intent(tmp_path / "runs" / "b", "run-b")
        with pytest.raises(ValueError, match="violates its append-only"):
            register_canonical_publication(
                pubs, run_intent_path=intent_b,
                artifact_digest="sha256:artifact-b",
                artifact_uri="object://renquant-artifacts/b.bin",
            )

    def test_append_onto_in_place_rebound_index_entry_is_refused(self, tmp_path):
        """An existing INDEX entry is hand-edited to point at a DIFFERENT
        run_intent_digest (a mutating, non-append edit). The store no longer
        self-verifies, so the next append is refused."""
        pubs = tmp_path / "pubs"
        _register(pubs, tmp_path / "runs" / "a", "run-a", "sha256:artifact-a")
        index = _read_index(pubs)
        index["sha256:artifact-a"]["run_intent_digest"] = "sha256:" + "e" * 64
        _write_index(pubs, index)

        assert verify_canonical_index_integrity(pubs) != []
        intent_b = _write_valid_intent(tmp_path / "runs" / "b", "run-b")
        with pytest.raises(ValueError, match="violates its append-only"):
            register_canonical_publication(
                pubs, run_intent_path=intent_b,
                artifact_digest="sha256:artifact-b",
                artifact_uri="object://renquant-artifacts/b.bin",
            )

    def test_register_never_removes_or_mutates_a_prior_entry(self, tmp_path):
        pubs = tmp_path / "pubs"
        _register(pubs, tmp_path / "runs" / "a", "run-a", "sha256:artifact-a")
        before = _read_index(pubs)["sha256:artifact-a"]
        _register(pubs, tmp_path / "runs" / "b", "run-b", "sha256:artifact-b")
        after = _read_index(pubs)["sha256:artifact-a"]
        assert after == before  # the prior binding is byte-identical

    def test_append_only_relation_flags_removal_and_mutation(self):
        prev = {"a": {"run_intent_digest": "d1"}, "b": {"run_intent_digest": "d2"}}
        assert _index_append_only_errors(prev, prev) == []
        assert _index_append_only_errors(prev, {**prev, "c": {"x": 1}}) == []
        removed = _index_append_only_errors(prev, {"a": prev["a"]})
        assert any("would be removed" in e and "'b'" in e for e in removed)
        mutated = _index_append_only_errors(prev, {**prev, "a": {"run_intent_digest": "z"}})
        assert any("would be mutated in place" in e and "'a'" in e for e in mutated)


# ── content-addressing + producer allow-list on the standalone verifier ─────


class TestIndexIntegrityVerifier:
    def test_empty_or_absent_store_is_not_a_violation(self, tmp_path):
        assert verify_canonical_index_integrity(tmp_path / "missing") == []
        pubs = tmp_path / "pubs"
        pubs.mkdir()
        assert verify_canonical_index_integrity(pubs) == []

    def test_non_content_addressed_record_is_refused(self, tmp_path):
        """The record file's bytes do not hash to the digest the INDEX entry
        binds -- rejected by recomputation alone (no local build path)."""
        pubs = tmp_path / "pubs"
        intent = _write_valid_intent(tmp_path / "runs" / "a", "run-a")
        digest = artifact_sha256(intent)
        (pubs).mkdir(parents=True)
        # Persist DIFFERENT bytes under the content-addressed name.
        (pubs / _record_filename(digest)).write_text(json.dumps({"tampered": True}))
        _write_index(pubs, {
            "sha256:artifact-a": {
                "run_intent_digest": digest,
                "record": _record_filename(digest),
                "artifact_uri": "object://x.bin",
                "registered_at": "2026-07-18T00:00:00Z",
            }
        })
        errors = verify_canonical_index_integrity(pubs)
        assert any("not content-addressed" in e for e in errors)

    def test_record_filename_not_matching_digest_is_refused(self, tmp_path):
        pubs = tmp_path / "pubs"
        pubs.mkdir(parents=True)
        intent = _write_valid_intent(tmp_path / "runs" / "a", "run-a")
        digest = artifact_sha256(intent)
        (pubs / "wrong-name.json").write_bytes(intent.read_bytes())
        _write_index(pubs, {
            "sha256:artifact-a": {
                "run_intent_digest": digest,
                "record": "wrong-name.json",
                "artifact_uri": "object://x.bin",
                "registered_at": "2026-07-18T00:00:00Z",
            }
        })
        errors = verify_canonical_index_integrity(pubs)
        assert any("is not the content-addressed name" in e for e in errors)

    def test_non_allowlisted_producer_entry_is_refused(self, tmp_path):
        """A wholesale-forged entry: record bytes, content-addressed filename
        and INDEX entry are all internally consistent, but the record names a
        producer outside CANONICAL_PRODUCERS -- refused by intrinsic
        verification of the persisted record."""
        pubs = tmp_path / "pubs"
        pubs.mkdir(parents=True)
        forged = {
            "schema_version": 1,
            "kind": "canonical-run-intent",
            "run_id": "run-forged",
            "run_type": "daily_full",
            "created_at": "2026-07-18T00:00:00Z",
            "producer": {"repo": "somewhere-else", "entrypoint": "not.Allowlisted"},
            "workflow_class": "canonical",
            "strategy_manifest_fingerprint": "sha256:s",
            "data_manifest_fingerprint": "sha256:d",
            "strategy_config_digest": "sha256:sc",
            "model_config_digest": "sha256:mc",
            "calendar_universe_digest": "sha256:u",
            "as_of": "2026-07-18",
            "code_pins": {
                name: {"commit": "0" * 40, "remote": "https://example.com/r"}
                for name in CANONICAL_CODE_PIN_SUBREPOS.values()
            },
        }
        forged_path = tmp_path / "forged.json"
        forged_path.write_text(json.dumps(forged))
        digest = artifact_sha256(forged_path)
        (pubs / _record_filename(digest)).write_bytes(forged_path.read_bytes())
        _write_index(pubs, {
            "sha256:forged-artifact": {
                "run_intent_digest": digest,
                "record": _record_filename(digest),
                "artifact_uri": "object://forged.bin",
                "registered_at": "2026-07-18T00:00:00Z",
            }
        })
        errors = verify_canonical_index_integrity(pubs)
        assert any("CANONICAL_PRODUCERS allowlist" in e for e in errors)
        # And a subsequent legitimate append is refused on top of it.
        intent_b = _write_valid_intent(tmp_path / "runs" / "b", "run-b")
        with pytest.raises(ValueError, match="violates its append-only"):
            register_canonical_publication(
                pubs, run_intent_path=intent_b,
                artifact_digest="sha256:artifact-b",
                artifact_uri="object://b.bin",
            )

    def test_malformed_entry_missing_binding_is_refused(self, tmp_path):
        pubs = tmp_path / "pubs"
        pubs.mkdir(parents=True)
        _write_index(pubs, {"sha256:artifact-a": {"artifact_uri": "object://x.bin"}})
        errors = verify_canonical_index_integrity(pubs)
        assert any("no usable run_intent_digest" in e for e in errors)

    def test_missing_record_file_is_refused(self, tmp_path):
        pubs = tmp_path / "pubs"
        pubs.mkdir(parents=True)
        digest = "sha256:" + "a" * 64
        _write_index(pubs, {
            "sha256:artifact-a": {
                "run_intent_digest": digest,
                "record": _record_filename(digest),
                "artifact_uri": "object://x.bin",
                "registered_at": "2026-07-18T00:00:00Z",
            }
        })
        errors = verify_canonical_index_integrity(pubs)
        assert any("record file" in e and "is missing" in e for e in errors)


# ── concurrent-append safety ────────────────────────────────────────────────


class TestConcurrentAppend:
    def test_concurrent_appends_do_not_lose_an_entry(self, tmp_path):
        """Two producer threads race the read-modify-write of the same store.
        The POSIX advisory lock serializes the critical section, so BOTH
        appends survive (a lost update would drop one)."""
        pubs = tmp_path / "pubs"
        n = 8
        intents = [
            _write_valid_intent(tmp_path / "runs" / f"r{i}", f"run-{i}")
            for i in range(n)
        ]
        barrier = threading.Barrier(n)
        errors: list[Exception] = []

        def worker(i: int) -> None:
            barrier.wait()
            try:
                register_canonical_publication(
                    pubs, run_intent_path=intents[i],
                    artifact_digest=f"sha256:artifact-{i}",
                    artifact_uri=f"object://renquant-artifacts/{i}.bin",
                )
            except Exception as exc:  # noqa: BLE001 - surface for assertion
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        index = _read_index(pubs)
        assert set(index) == {f"sha256:artifact-{i}" for i in range(n)}
        assert verify_canonical_index_integrity(pubs) == []


# ── end-to-end: candidate -> verified append -> pinned-checkout read ────────


class TestPinnedCheckoutAcceptance:
    def test_candidate_verified_append_then_pinned_checkout_verifies(self, tmp_path):
        """The full F-7 registry flow: a producer candidate run_intent is
        verified-appended into the store, the store is committed as a clean
        pinned snapshot, and reading it back from that pinned checkout both
        resolves the binding and passes the whole-index integrity audit."""
        repo_root = tmp_path / "registry_repo"
        pubs = repo_root / "registry" / "canonical_publications"
        intent = _register(pubs, tmp_path / "runs" / "a", "run-a", "sha256:artifact-a")

        subprocess.run(["git", "init", "-q"], cwd=repo_root, check=True)
        subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo_root, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=repo_root, check=True)
        subprocess.run(
            ["git", "remote", "add", "origin", "https://github.com/hallovorld/renquant-artifacts"],
            cwd=repo_root, check=True,
        )
        subprocess.run(["git", "add", "registry"], cwd=repo_root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "publish canonical"], cwd=repo_root, check=True)
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo_root, text=True,
        ).strip()

        # A dedicated pinned checkout the validating caller would trust.
        pinned = tmp_path / "pinned"
        subprocess.run(["git", "clone", "-q", str(repo_root), str(pinned)], check=True)
        pinned_pubs = pinned / "registry" / "canonical_publications"

        # The pinned tree has NO lingering runtime lock file and self-verifies.
        assert not (pinned_pubs / (CANONICAL_PUBLICATIONS_INDEX_FILENAME + ".lock")).exists()
        assert verify_canonical_index_integrity(pinned_pubs) == []
        entry, record, resolve_errors = resolve_canonical_publication(
            "sha256:artifact-a", pinned_pubs,
        )
        assert resolve_errors == []
        assert entry["run_intent_digest"] == artifact_sha256(intent)
        assert record["run_id"] == "run-a"
        assert commit  # a real pinned commit exists
