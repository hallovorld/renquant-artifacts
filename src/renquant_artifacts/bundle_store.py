"""Transactional bundle store for the 104 serving pair (AUTHORITATIVE).

Normative spec: RFC "transactional artifact bundles for the 104 serving
pair" (RenQuant#492, doc/design/2026-07-17-artifact-bundle-transactionality.md)
— §2.1 (layout + host model), §2.3 (writer protocol), §2.4 (authorization,
break-glass), §2.6 (reader protocol + reference-rooted GC).

Store layout (under a caller-chosen ``root``, the RFC's ``…/prod/``)::

    <root>/bundles/<bundle_id>/manifest.json + members
    <root>/bundles/.lock              # flock EX — writers AND GC serialize
    <root>/bundles/OPERATIONS.jsonl   # append-only, fsync'd per record
    <root>/ACTIVE                     # pointer: "<generation> <bundle_id>"

Host model (RFC §2.1): single-host POSIX filesystem. ``flock`` is the store
lock under this model ONLY; the store refuses to open on a path that cannot
be confirmed to be a local mount (fail-closed; injectable guard for tests
and exotic hosts).

Phase-1 seams (bound in later phases, per the RFC ownership map §5):

* ``pair_validator`` — the renquant-pipeline PUBLIC pair-validation API
  (``renquant_pipeline.bundle_contract.validate_pair``) invoked at writer
  step 6. Signature accepted here::

      pair_validator(manifest_payload: Mapping[str, Any],
                     member_paths: Mapping[str, pathlib.Path]) -> Any

  Rejection = raising any exception, returning ``False``, or returning an
  object whose ``ok`` attribute is falsy. ``None`` (and any other return)
  = pass. When no validator is supplied, step 6 still performs full
  schema + digest re-verification.

* ``is_referenced`` — the orchestrator-owned run-bundle reference query
  used by GC (RFC §2.6)::

      is_referenced(bundle_id: str) -> bool

  ``True`` means at least one RETAINED run bundle references the bundle;
  GC then must not delete it. There is deliberately NO default: GC
  without a reference query must not run.

Kill-injection support: every numbered writer step and every fsync site
invokes ``crash_hook(label)`` with a label from :data:`WRITER_CHECKPOINTS`;
a hook that raises simulates a crash at exactly that point (the flock is
released on the way out, as it would be on process death).
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .bundle_schema import (
    BREAKGLASS_TOOL,
    BUNDLE_ID_RE,
    BUNDLE_MEMBER_NAMES,
    MANIFEST_FILENAME,
    BundleManifest,
    BundleSchemaError,
    MemberDigest,
    build_bundle_manifest,
    format_created_at,
    sha256_hex,
    validate_bundle_authorization,
)

ACTIVE_FILENAME = "ACTIVE"
ACTIVE_TMP_FILENAME = "ACTIVE.tmp"
BUNDLES_DIRNAME = "bundles"
LOCK_FILENAME = ".lock"
OPERATIONS_FILENAME = "OPERATIONS.jsonl"

RECORD_PREPARE = "PREPARE"
RECORD_ACTIVATE = "ACTIVATE"
RECORD_RECOVERY = "RECOVERY"
RECORD_GC_DELETE = "GC_DELETE"
RECORD_GC_SWEEP_TMP = "GC_SWEEP_TMP"

#: Every crash-injection checkpoint of the §2.3 writer protocol, in
#: execution order: each numbered step boundary plus a point after every
#: fsync. The kill-injection suite iterates over this exact tuple.
WRITER_CHECKPOINTS: tuple[str, ...] = (
    "step1:locked",
    f"step2:member-written:{BUNDLE_MEMBER_NAMES[0]}",
    f"step2:member-fsynced:{BUNDLE_MEMBER_NAMES[0]}",
    f"step2:member-written:{BUNDLE_MEMBER_NAMES[1]}",
    f"step2:member-fsynced:{BUNDLE_MEMBER_NAMES[1]}",
    "step3:manifest-written",
    "step3:manifest-fsynced",
    "step4:tmpdir-fsynced",
    "step5:bundle-dir-renamed",
    "step5:bundles-dir-fsynced",
    "step6:validated",
    "step7:prepare-written",
    "step7:prepare-fsynced",
    "step8:active-tmp-written",
    "step8:active-tmp-fsynced",
    "step9:active-renamed",
    "step9:store-root-fsynced",
    "step10:activate-written",
    "step10:activate-fsynced",
)

#: Checkpoints shared by the rollback path (which skips steps 2-5: the
#: target bundle already exists in the archive).
ROLLBACK_CHECKPOINTS: tuple[str, ...] = (
    "step1:locked",
    "step6:validated",
    "step7:prepare-written",
    "step7:prepare-fsynced",
    "step8:active-tmp-written",
    "step8:active-tmp-fsynced",
    "step9:active-renamed",
    "step9:store-root-fsynced",
    "step10:activate-written",
    "step10:activate-fsynced",
)

_NETWORK_FS_TYPES = frozenset(
    {
        "nfs",
        "nfs3",
        "nfs4",
        "cifs",
        "smb",
        "smb2",
        "smbfs",
        "afpfs",
        "webdav",
        "davfs",
        "fuse.sshfs",
        "sshfs",
        "9p",
        "afs",
        "glusterfs",
        "lustre",
        "ceph",
        "fuse.ceph",
        "fuse.cephfs",
    }
)


class BundleStoreError(RuntimeError):
    """Base error for the transactional bundle store."""


class NonLocalStoreError(BundleStoreError):
    """The store path is not (confirmably) on a local mount (RFC §2.1)."""


class BundleValidationError(BundleStoreError):
    """Writer step-6 / bundle-content validation failed."""


class BundleCollisionError(BundleStoreError):
    """A bundle_id collision was detected (RFC §2.2: collision => abort)."""


class RecoveryRequiredError(BundleStoreError):
    """A PREPARE-without-ACTIVATE crash interval exists and no RECOVERY
    record has been committed; mutations are refused (RFC §2.3)."""


class BundleReadRefusedError(BundleStoreError):
    """The reader refuses to serve (no PREPARE record, pointer regression,
    digest mismatch, or schema violation) — fail-closed (RFC §2.6)."""


class RollbackTargetError(BundleStoreError):
    """--rollback-to target is not a parent_bundle ancestor (RFC §2.4)."""


@dataclass(frozen=True)
class PublishResult:
    bundle_id: str
    generation: int
    manifest: BundleManifest


@dataclass(frozen=True)
class GCReport:
    deleted: tuple[str, ...]
    retained_active: tuple[str, ...]
    retained_ancestry: tuple[str, ...]
    retained_referenced: tuple[str, ...]
    swept_tmp: tuple[str, ...]


@dataclass
class _LogState:
    records: list[dict[str, Any]] = field(default_factory=list)
    torn_tail: bool = False

    def prepares(self) -> dict[int, dict[str, Any]]:
        out: dict[int, dict[str, Any]] = {}
        for rec in self.records:
            if rec.get("record") == RECORD_PREPARE:
                out[int(rec["generation"])] = rec
        return out

    def activates(self) -> dict[int, dict[str, Any]]:
        out: dict[int, dict[str, Any]] = {}
        for rec in self.records:
            if rec.get("record") == RECORD_ACTIVATE:
                out[int(rec["generation"])] = rec
        return out

    def recovered_generations(self) -> set[int]:
        out: set[int] = set()
        for rec in self.records:
            if rec.get("record") == RECORD_RECOVERY:
                out.update(int(g) for g in rec.get("generations", []))
        return out

    def dangling_prepares(self) -> dict[int, dict[str, Any]]:
        """PREPARE records with neither an ACTIVATE nor a RECOVERY —
        detected crash intervals blocking further mutation (RFC §2.3)."""
        activated = set(self.activates())
        recovered = self.recovered_generations()
        return {
            gen: rec
            for gen, rec in self.prepares().items()
            if gen not in activated and gen not in recovered
        }

    def max_generation(self) -> int:
        gens = [
            int(rec["generation"])
            for rec in self.records
            if rec.get("record") in (RECORD_PREPARE, RECORD_ACTIVATE)
            and "generation" in rec
        ]
        for rec in self.records:
            if rec.get("record") == RECORD_RECOVERY:
                gens.extend(int(g) for g in rec.get("generations", []))
        return max(gens, default=0)

    def max_activated_generation(self) -> int:
        return max(self.activates(), default=0)


class ResolvedBundle:
    """A resolved, digest-verified bundle held open by dirfd.

    Member reads go through file descriptors opened via the bundle's
    directory fd at resolve time, so a concurrent GC pass or pointer flip
    cannot invalidate an in-progress read (POSIX unlink-after-open).
    """

    def __init__(
        self,
        *,
        bundle_id: str,
        generation: int | None,
        manifest: BundleManifest,
        dir_fd: int,
        member_fds: dict[str, int],
        crash_interval: bool = False,
    ) -> None:
        self.bundle_id = bundle_id
        self.generation = generation
        self.manifest = manifest
        self.crash_interval = crash_interval
        self._dir_fd: int | None = dir_fd
        self._member_fds = member_fds

    @property
    def recovery_required(self) -> bool:
        return self.crash_interval

    def read_member(self, name: str) -> bytes:
        if self._dir_fd is None:
            raise BundleStoreError("resolved bundle is closed")
        fd = self._member_fds[name]
        size = os.fstat(fd).st_size
        return os.pread(fd, size, 0)

    def close(self) -> None:
        for fd in self._member_fds.values():
            _close_quietly(fd)
        self._member_fds = {}
        if self._dir_fd is not None:
            _close_quietly(self._dir_fd)
            self._dir_fd = None

    def __enter__(self) -> "ResolvedBundle":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


class BundleStore:
    """AUTHORITATIVE bundle store: publication, resolution, recovery, GC.

    ``root`` is the store root (the RFC's ``…/artifacts/prod``). See the
    module docstring for the two phase-1 seams (``pair_validator``,
    ``is_referenced``) and the crash/alarm hooks.

    ``alarm_hook(kind, payload)`` fires on: serving a detected crash
    interval (``"crash_interval_serve"``), committing a recovery
    (``"recovery_committed"``), and every break-glass commit
    (``"breakglass_commit"`` — the drift-sentinel integration point).
    """

    def __init__(
        self,
        root: str | Path,
        *,
        pair_validator: Callable[[Mapping[str, Any], Mapping[str, Path]], Any] | None = None,
        alarm_hook: Callable[[str, dict[str, Any]], None] | None = None,
        crash_hook: Callable[[str], None] | None = None,
        local_mount_guard: Callable[[Path], tuple[bool, str]] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.root = Path(root)
        self._pair_validator = pair_validator
        self._alarm_hook = alarm_hook
        self._crash_hook = crash_hook
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        guard = local_mount_guard or default_local_mount_guard
        ok, reason = guard(self.root)
        if not ok:
            raise NonLocalStoreError(
                f"refusing to open bundle store at {self.root}: {reason} "
                "(RFC §2.1 host model: single-host local POSIX filesystem only)"
            )

    # -- paths -----------------------------------------------------------

    @property
    def bundles_dir(self) -> Path:
        return self.root / BUNDLES_DIRNAME

    @property
    def active_path(self) -> Path:
        return self.root / ACTIVE_FILENAME

    @property
    def lock_path(self) -> Path:
        return self.bundles_dir / LOCK_FILENAME

    @property
    def operations_path(self) -> Path:
        return self.bundles_dir / OPERATIONS_FILENAME

    def bundle_dir(self, bundle_id: str) -> Path:
        return self.bundles_dir / bundle_id

    # -- writer protocol (RFC §2.3) --------------------------------------

    def publish(
        self,
        members: Mapping[str, str | Path | bytes],
        *,
        bindings: Mapping[str, Any],
        authorization: Mapping[str, Any],
    ) -> PublishResult:
        """Commit a new bundle and flip ACTIVE to it — the 10-step
        durability-ordered protocol of RFC §2.3."""
        auth = dict(authorization)
        validate_bundle_authorization(auth)
        member_bytes = self._load_member_contents(members)

        lock_fd = self._acquire_lock()  # step 1
        try:
            self._checkpoint("step1:locked")
            log = self._read_log(strict=True)
            self._refuse_if_recovery_required(log)

            active = self._read_active_pointer()
            parent = active[1] if active else None
            created_at = format_created_at(self._clock())
            digests = {
                name: MemberDigest(sha256=sha256_hex(data), bytes=len(data))
                for name, data in member_bytes.items()
            }
            manifest = build_bundle_manifest(
                member_digests=digests,
                bindings=bindings,
                authorization=auth,
                parent_bundle=parent,
                created_at=created_at,
            )
            bundle_id = manifest.bundle_id
            final_dir = self.bundle_dir(bundle_id)
            tmp_dir = self.bundles_dir / f"{bundle_id}.tmp"
            if final_dir.exists() or tmp_dir.exists():
                raise BundleCollisionError(
                    f"bundle_id collision for {bundle_id}; aborting (RFC §2.2)"
                )

            # step 2: members into the tmp dir, fsync each file
            os.mkdir(tmp_dir)
            for name in sorted(member_bytes):
                _write_file_fsync(
                    tmp_dir / name,
                    member_bytes[name],
                    written_cb=lambda n=name: self._checkpoint(f"step2:member-written:{n}"),
                    fsynced_cb=lambda n=name: self._checkpoint(f"step2:member-fsynced:{n}"),
                )
            # step 3: manifest, fsync
            _write_file_fsync(
                tmp_dir / MANIFEST_FILENAME,
                manifest.canonical_bytes(),
                written_cb=lambda: self._checkpoint("step3:manifest-written"),
                fsynced_cb=lambda: self._checkpoint("step3:manifest-fsynced"),
            )
            # step 4: fsync tmp dirfd
            _fsync_dir(tmp_dir)
            self._checkpoint("step4:tmpdir-fsynced")
            # step 5: rename into place, fsync bundles/ dirfd
            os.rename(tmp_dir, final_dir)
            self._checkpoint("step5:bundle-dir-renamed")
            _fsync_dir(self.bundles_dir)
            self._checkpoint("step5:bundles-dir-fsynced")
            # step 6: validate — re-read manifest, verify digests, run the
            # (pluggable) public pair-validation API; failure => delete
            # the bundle dir and abort, still holding the lock.
            try:
                self._validate_bundle_dir(final_dir, expected_bundle_id=bundle_id)
            except BundleValidationError:
                shutil.rmtree(final_dir, ignore_errors=True)
                raise
            self._checkpoint("step6:validated")

            generation = self._next_generation(active, log)
            return self._flip_pointer(generation, bundle_id, auth, manifest)
        finally:
            _release_lock(lock_fd)

    def rollback_to(
        self,
        bundle_id: str,
        *,
        authorization: Mapping[str, Any],
    ) -> PublishResult:
        """Flip ACTIVE back to a parent_bundle ancestor via the same
        protocol (steps 1, 6-10; the target already exists immutably).

        Restricted to the break-glass tool (RFC §2.4): rollback is a
        manual containment action by definition.
        """
        auth = dict(authorization)
        validate_bundle_authorization(auth)
        if auth["tool"] != BREAKGLASS_TOOL:
            raise BundleStoreError(
                "rollback_to is restricted to the break-glass tool "
                f"(authorization.tool={auth['tool']!r} != {BREAKGLASS_TOOL!r})"
            )
        lock_fd = self._acquire_lock()
        try:
            self._checkpoint("step1:locked")
            log = self._read_log(strict=True)
            self._refuse_if_recovery_required(log)
            active = self._read_active_pointer()
            if active is None:
                raise BundleStoreError("cannot rollback: store has no ACTIVE pointer")
            ancestry = self._ancestor_chain(active[1])
            if bundle_id not in ancestry[1:]:
                raise RollbackTargetError(
                    f"--rollback-to target {bundle_id} is not a parent_bundle "
                    f"ancestor of the ACTIVE bundle {active[1]} (RFC §2.4)"
                )
            manifest = self._validate_bundle_dir(
                self.bundle_dir(bundle_id), expected_bundle_id=bundle_id
            )
            self._checkpoint("step6:validated")
            generation = self._next_generation(active, log)
            return self._flip_pointer(generation, bundle_id, auth, manifest)
        finally:
            _release_lock(lock_fd)

    def commit_recovery(self, *, actor: Mapping[str, str], note: str) -> list[int]:
        """Commit a RECOVERY record naming every dangling PREPARE interval
        (RFC §2.3), unblocking mutations. Returns the recovered
        generations (empty list = nothing dangling; idempotent)."""
        if not note:
            raise BundleStoreError("recovery requires a non-empty note naming the interval")
        lock_fd = self._acquire_lock()
        try:
            log = self._read_log(strict=True)
            dangling = log.dangling_prepares()
            if not dangling:
                return []
            generations = sorted(dangling)
            record = {
                "record": RECORD_RECOVERY,
                "generations": generations,
                "bundle_ids": [dangling[g].get("bundle_id") for g in generations],
                "actor": dict(actor),
                "note": note,
                "ts": format_created_at(self._clock()),
            }
            self._append_operation(record)
            self._alarm(
                "recovery_committed",
                {"generations": generations, "note": note},
            )
            return generations
        finally:
            _release_lock(lock_fd)

    # -- reader protocol (RFC §2.6) --------------------------------------

    def resolve_active(self) -> ResolvedBundle:
        """Resolve ACTIVE -> audit against OPERATIONS.jsonl -> dirfd-open
        -> digest-verify -> serve. Fail-closed on every violation."""
        for attempt in (0, 1):
            pointer = self._read_active_pointer()
            if pointer is None:
                raise BundleReadRefusedError("no ACTIVE pointer; nothing to serve")
            generation, bundle_id = pointer
            log = self._read_log(strict=True)

            prepare = log.prepares().get(generation)
            if prepare is None:
                raise BundleReadRefusedError(
                    f"ACTIVE generation {generation} has NO PREPARE record in "
                    "OPERATIONS.jsonl — refusing to serve unaudited state (RFC §2.3)"
                )
            if prepare.get("bundle_id") != bundle_id:
                raise BundleReadRefusedError(
                    f"ACTIVE generation {generation} PREPARE record names bundle "
                    f"{prepare.get('bundle_id')!r} but the pointer names {bundle_id!r}"
                )
            max_activated = log.max_activated_generation()
            if max_activated > generation:
                raise BundleReadRefusedError(
                    f"stale ACTIVE pointer: generation {generation} < last "
                    f"activated generation {max_activated} — pointer regression "
                    "outside --rollback-to is refused (RFC §2.3/§4)"
                )
            activate = log.activates().get(generation)
            if activate is not None and activate.get("bundle_id") != bundle_id:
                raise BundleReadRefusedError(
                    f"ACTIVATE record for generation {generation} names bundle "
                    f"{activate.get('bundle_id')!r} but the pointer names {bundle_id!r}"
                )
            crash_interval = activate is None and generation not in log.recovered_generations()

            try:
                resolved = self._open_and_verify(
                    bundle_id, generation=generation, crash_interval=crash_interval
                )
            except FileNotFoundError:
                if attempt == 0:
                    continue  # ACTIVE may have flipped mid-resolve; re-read once
                raise BundleReadRefusedError(
                    f"ACTIVE bundle directory {bundle_id} is missing"
                )
            if crash_interval:
                self._alarm(
                    "crash_interval_serve",
                    {
                        "generation": generation,
                        "bundle_id": bundle_id,
                        "detail": "PREPARE without ACTIVATE: serving fully-prepared "
                        "state; a RECOVERY record is required before the next mutation",
                    },
                )
            return resolved
        raise BundleReadRefusedError("unreachable")  # pragma: no cover

    def resolve_bundle(self, bundle_id: str) -> ResolvedBundle:
        """Resolve an ARCHIVED bundle by id (run-bundle replay support):
        dirfd-open + full digest verification, no pointer semantics."""
        if not BUNDLE_ID_RE.match(bundle_id):
            raise BundleReadRefusedError(f"malformed bundle_id {bundle_id!r}")
        try:
            return self._open_and_verify(bundle_id, generation=None, crash_interval=False)
        except FileNotFoundError:
            raise BundleReadRefusedError(f"bundle {bundle_id} is not in the archive")

    # -- GC (RFC §2.6, reference-rooted; r4 hierarchy) -------------------

    def collect_garbage(self, *, is_referenced: Callable[[str], bool]) -> GCReport:
        """Delete bundles that are (a) not the ACTIVE target, (b) outside
        the ACTIVE bundle's parent_bundle rollback ancestry, and (c) not
        referenced by any retained run bundle per ``is_referenced``.

        There is NO time or count cutoff anywhere (RFC §2.6 r4): run-bundle
        retention (owned by the orchestrator, queried through the seam) is
        the single knob. Serializes on the store flock; deletions are
        operation-log records; readers holding dirfds are safe
        (unlink-after-open).
        """
        if not callable(is_referenced):
            raise TypeError("collect_garbage requires an injectable is_referenced callable")
        lock_fd = self._acquire_lock()
        try:
            log = self._read_log(strict=True)
            self._refuse_if_recovery_required(log)
            active = self._read_active_pointer()

            deleted: list[str] = []
            retained_active: list[str] = []
            retained_ancestry: list[str] = []
            retained_referenced: list[str] = []
            swept_tmp: list[str] = []

            if not self.bundles_dir.is_dir():
                return GCReport((), (), (), (), ())
            entries = sorted(os.listdir(self.bundles_dir))
            bundle_entries = [e for e in entries if BUNDLE_ID_RE.match(e)]
            if active is None:
                if bundle_entries:
                    raise BundleStoreError(
                        "refusing to GC a store with bundles but no ACTIVE pointer"
                    )
                return GCReport((), (), (), (), ())
            active_id = active[1]
            ancestry = set(self._ancestor_chain(active_id))

            for entry in entries:
                path = self.bundles_dir / entry
                if entry in (LOCK_FILENAME, OPERATIONS_FILENAME):
                    continue
                if entry.endswith(".tmp") and path.is_dir():
                    # A .tmp dir visible while we hold the store flock is
                    # dead (writers hold the lock across steps 2-10).
                    self._append_operation(
                        {
                            "record": RECORD_GC_SWEEP_TMP,
                            "entry": entry,
                            "ts": format_created_at(self._clock()),
                        }
                    )
                    shutil.rmtree(path, ignore_errors=True)
                    swept_tmp.append(entry)
                    continue
                if not BUNDLE_ID_RE.match(entry) or not path.is_dir():
                    continue
                if entry == active_id:
                    retained_active.append(entry)
                    continue
                if entry in ancestry:
                    retained_ancestry.append(entry)
                    continue
                if is_referenced(entry):
                    retained_referenced.append(entry)
                    continue
                # Log-ahead: the deletion record lands (fsync'd) before the
                # unlink; a crash in between re-deletes harmlessly next pass.
                self._append_operation(
                    {
                        "record": RECORD_GC_DELETE,
                        "bundle_id": entry,
                        "ts": format_created_at(self._clock()),
                    }
                )
                shutil.rmtree(path, ignore_errors=True)
                deleted.append(entry)

            return GCReport(
                deleted=tuple(deleted),
                retained_active=tuple(retained_active),
                retained_ancestry=tuple(retained_ancestry),
                retained_referenced=tuple(retained_referenced),
                swept_tmp=tuple(swept_tmp),
            )
        finally:
            _release_lock(lock_fd)

    # -- introspection ---------------------------------------------------

    def read_operations(self) -> list[dict[str, Any]]:
        """All well-formed operation-log records (torn tail tolerated)."""
        return self._read_log(strict=False).records

    def recovery_required(self) -> bool:
        return bool(self._read_log(strict=False).dangling_prepares())

    # -- internals -------------------------------------------------------

    def _flip_pointer(
        self,
        generation: int,
        bundle_id: str,
        auth: dict[str, Any],
        manifest: BundleManifest,
    ) -> PublishResult:
        """Steps 7-10: PREPARE -> pointer flip -> ACTIVATE (lock held)."""
        # step 7: PREPARE before the flip — the activation-audit invariant.
        prepare_line = self._append_operation(
            {
                "record": RECORD_PREPARE,
                "generation": generation,
                "bundle_id": bundle_id,
                "authorization": auth,
                "ts": format_created_at(self._clock()),
            },
            written_checkpoint="step7:prepare-written",
            fsynced_checkpoint="step7:prepare-fsynced",
        )
        # step 8: ACTIVE.tmp, fsync
        active_tmp = self.root / ACTIVE_TMP_FILENAME
        _write_file_fsync(
            active_tmp,
            f"{generation} {bundle_id}\n".encode("ascii"),
            written_cb=lambda: self._checkpoint("step8:active-tmp-written"),
            fsynced_cb=lambda: self._checkpoint("step8:active-tmp-fsynced"),
        )
        # step 9: rename over ACTIVE, fsync store-root dirfd
        os.rename(active_tmp, self.active_path)
        self._checkpoint("step9:active-renamed")
        _fsync_dir(self.root)
        self._checkpoint("step9:store-root-fsynced")
        # step 10: ACTIVATE bound to the PREPARE record
        self._append_operation(
            {
                "record": RECORD_ACTIVATE,
                "generation": generation,
                "bundle_id": bundle_id,
                "prepare_sha256": sha256_hex(prepare_line),
                "ts": format_created_at(self._clock()),
            },
            written_checkpoint="step10:activate-written",
            fsynced_checkpoint="step10:activate-fsynced",
        )
        if auth.get("tool") == BREAKGLASS_TOOL:
            self._alarm(
                "breakglass_commit",
                {
                    "generation": generation,
                    "bundle_id": bundle_id,
                    "incident_ref": auth.get("source", {}).get("incident_ref"),
                },
            )
        return PublishResult(bundle_id=bundle_id, generation=generation, manifest=manifest)

    def _validate_bundle_dir(
        self, bundle_dir: Path, *, expected_bundle_id: str
    ) -> BundleManifest:
        """Step-6 validation: re-read manifest from disk, schema-check,
        recompute digests, verify the exact member set, then run the
        pluggable pair validator (§2.5 seam)."""
        manifest_path = bundle_dir / MANIFEST_FILENAME
        try:
            payload = json.loads(manifest_path.read_bytes().decode("utf-8"))
        except FileNotFoundError:
            raise BundleValidationError(f"{bundle_dir.name}: manifest.json is missing")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BundleValidationError(f"{bundle_dir.name}: manifest.json unreadable: {exc}")
        try:
            manifest = BundleManifest.from_payload(payload)
        except BundleSchemaError as exc:
            raise BundleValidationError(f"{bundle_dir.name}: {exc}") from exc
        if manifest.bundle_id != expected_bundle_id:
            raise BundleValidationError(
                f"manifest derives bundle_id {manifest.bundle_id} but the bundle "
                f"directory is {expected_bundle_id}"
            )
        entries = set(os.listdir(bundle_dir))
        expected_entries = {MANIFEST_FILENAME, *BUNDLE_MEMBER_NAMES}
        if entries != expected_entries:
            raise BundleValidationError(
                f"{expected_bundle_id}: bundle directory member set mismatch; "
                f"missing={sorted(expected_entries - entries)} "
                f"extra={sorted(entries - expected_entries)} (RFC §2.2)"
            )
        for name, digest in manifest.members.items():
            data = (bundle_dir / name).read_bytes()
            if len(data) != digest.bytes or sha256_hex(data) != digest.sha256:
                raise BundleValidationError(
                    f"{expected_bundle_id}: member {name} content does not match "
                    "its manifest digest"
                )
        self._run_pair_validator(manifest, bundle_dir)
        return manifest

    def _run_pair_validator(self, manifest: BundleManifest, bundle_dir: Path) -> None:
        if self._pair_validator is None:
            return
        member_paths = {name: bundle_dir / name for name in manifest.members}
        try:
            verdict = self._pair_validator(manifest.to_payload(), member_paths)
        except Exception as exc:
            raise BundleValidationError(
                f"pair validator rejected bundle {manifest.bundle_id}: {exc}"
            ) from exc
        rejected = False
        if verdict is False:
            rejected = True
        elif verdict is not None and hasattr(verdict, "ok"):
            rejected = not bool(verdict.ok)
        if rejected:
            raise BundleValidationError(
                f"pair validator returned a failing verdict for bundle "
                f"{manifest.bundle_id}: {verdict!r}"
            )

    def _open_and_verify(
        self, bundle_id: str, *, generation: int | None, crash_interval: bool
    ) -> ResolvedBundle:
        """dirfd-open the bundle, verify manifest + member digests through
        that dirfd, and return the held-open resolution."""
        bundle_dir = self.bundle_dir(bundle_id)
        dir_fd = os.open(bundle_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        member_fds: dict[str, int] = {}
        try:
            entries = set(os.listdir(dir_fd))
            expected_entries = {MANIFEST_FILENAME, *BUNDLE_MEMBER_NAMES}
            if entries != expected_entries:
                raise BundleReadRefusedError(
                    f"{bundle_id}: bundle member set mismatch; "
                    f"missing={sorted(expected_entries - entries)} "
                    f"extra={sorted(entries - expected_entries)} (RFC §2.2)"
                )
            manifest_fd = os.open(MANIFEST_FILENAME, os.O_RDONLY, dir_fd=dir_fd)
            try:
                raw = _read_fd(manifest_fd)
            finally:
                _close_quietly(manifest_fd)
            try:
                payload = json.loads(raw.decode("utf-8"))
                manifest = BundleManifest.from_payload(payload)
            except (UnicodeDecodeError, json.JSONDecodeError, BundleSchemaError) as exc:
                raise BundleReadRefusedError(f"{bundle_id}: invalid manifest: {exc}") from exc
            if manifest.bundle_id != bundle_id:
                raise BundleReadRefusedError(
                    f"manifest derives bundle_id {manifest.bundle_id} but was "
                    f"served from directory {bundle_id}"
                )
            for name, digest in manifest.members.items():
                fd = os.open(name, os.O_RDONLY, dir_fd=dir_fd)
                member_fds[name] = fd
                data = _read_fd(fd)
                if len(data) != digest.bytes or sha256_hex(data) != digest.sha256:
                    raise BundleReadRefusedError(
                        f"{bundle_id}: member {name} failed digest verification"
                    )
            return ResolvedBundle(
                bundle_id=bundle_id,
                generation=generation,
                manifest=manifest,
                dir_fd=dir_fd,
                member_fds=member_fds,
                crash_interval=crash_interval,
            )
        except BaseException:
            for fd in member_fds.values():
                _close_quietly(fd)
            _close_quietly(dir_fd)
            raise

    def _ancestor_chain(self, bundle_id: str) -> list[str]:
        """[bundle_id, parent, grandparent, …] via parent_bundle links,
        stopping at genesis, a missing archive dir, or a cycle."""
        chain: list[str] = []
        current: str | None = bundle_id
        while current and current not in chain:
            chain.append(current)
            manifest_path = self.bundle_dir(current) / MANIFEST_FILENAME
            if not manifest_path.is_file():
                break
            try:
                payload = json.loads(manifest_path.read_bytes().decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise BundleStoreError(
                    f"cannot walk parent_bundle ancestry: manifest of {current} "
                    f"is unreadable ({exc})"
                ) from exc
            parent = payload.get("parent_bundle")
            current = parent if isinstance(parent, str) else None
        return chain

    def _next_generation(
        self, active: tuple[int, str] | None, log: _LogState
    ) -> int:
        """Monotonic generation: strictly above both the pointer and every
        generation ever named in the operation log (so a recovered, never-
        activated PREPARE's number is not reused)."""
        base = active[0] if active else 0
        return max(base, log.max_generation()) + 1

    def _read_active_pointer(self) -> tuple[int, str] | None:
        try:
            text = self.active_path.read_text(encoding="ascii")
        except FileNotFoundError:
            return None
        except UnicodeDecodeError as exc:
            raise BundleReadRefusedError(
                f"malformed ACTIVE pointer (non-ASCII bytes): {exc}"
            ) from exc
        parts = text.strip().split(" ")
        if len(parts) != 2 or not parts[0].isdigit() or not BUNDLE_ID_RE.match(parts[1]):
            raise BundleReadRefusedError(
                f"malformed ACTIVE pointer {text!r}; expected '<generation> <bundle_id>'"
            )
        return int(parts[0]), parts[1]

    def _read_log(self, *, strict: bool) -> _LogState:
        state = _LogState()
        try:
            raw = self.operations_path.read_bytes()
        except FileNotFoundError:
            return state
        lines = raw.split(b"\n")
        # A trailing empty chunk after the final LF is normal.
        if lines and lines[-1] == b"":
            lines.pop()
        for i, line in enumerate(lines):
            try:
                record = json.loads(line.decode("utf-8"))
                if not isinstance(record, dict):
                    raise ValueError("record is not an object")
            except (UnicodeDecodeError, ValueError) as exc:
                if i == len(lines) - 1:
                    state.torn_tail = True  # torn final append: tolerated
                    break
                raise BundleReadRefusedError(
                    f"OPERATIONS.jsonl line {i + 1} is corrupt: {exc}"
                ) from exc
            state.records.append(record)
        if strict and state.torn_tail:
            # A torn tail means the last append never completed its fsync
            # cycle; the record it would have created does not exist. That
            # is tolerated for reads — the audit invariant only requires
            # records that DO exist to be well-formed.
            pass
        return state

    def _refuse_if_recovery_required(self, log: _LogState) -> None:
        dangling = log.dangling_prepares()
        if dangling:
            gens = sorted(dangling)
            raise RecoveryRequiredError(
                f"PREPARE record(s) for generation(s) {gens} have no ACTIVATE "
                "and no RECOVERY — a crash interval is open; commit_recovery() "
                "must record it before any further mutation (RFC §2.3)"
            )

    def _append_operation(
        self,
        record: dict[str, Any],
        *,
        written_checkpoint: str | None = None,
        fsynced_checkpoint: str | None = None,
    ) -> bytes:
        """Append one fsync'd JSONL record; returns the exact line bytes."""
        self.bundles_dir.mkdir(parents=True, exist_ok=True)
        line = (
            json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            + "\n"
        ).encode("utf-8")
        existed = self.operations_path.exists()
        fd = os.open(
            self.operations_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644
        )
        try:
            os.write(fd, line)
            if written_checkpoint:
                self._checkpoint(written_checkpoint)
            os.fsync(fd)
        finally:
            _close_quietly(fd)
        if not existed:
            _fsync_dir(self.bundles_dir)
        if fsynced_checkpoint:
            self._checkpoint(fsynced_checkpoint)
        return line

    def _load_member_contents(
        self, members: Mapping[str, str | Path | bytes]
    ) -> dict[str, bytes]:
        provided = set(members)
        expected = set(BUNDLE_MEMBER_NAMES)
        if provided != expected:
            raise BundleValidationError(
                f"publish requires exactly the schema-v1 member set "
                f"{sorted(expected)}; missing={sorted(expected - provided)} "
                f"extra={sorted(provided - expected)}"
            )
        out: dict[str, bytes] = {}
        for name, source in members.items():
            if isinstance(source, bytes):
                data = source
            else:
                data = Path(source).read_bytes()
            if not data:
                raise BundleValidationError(f"member {name} is empty")
            out[name] = data
        return out

    def _acquire_lock(self) -> int:
        self.bundles_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except BaseException:
            _close_quietly(fd)
            raise
        return fd

    def _checkpoint(self, label: str) -> None:
        if self._crash_hook is not None:
            self._crash_hook(label)

    def _alarm(self, kind: str, payload: dict[str, Any]) -> None:
        if self._alarm_hook is not None:
            self._alarm_hook(kind, payload)


# -- host-model guard (RFC §2.1) ----------------------------------------


def default_local_mount_guard(path: Path) -> tuple[bool, str]:
    """Best-effort check that ``path`` is on a LOCAL mount. Fail-closed:
    if the platform or filesystem cannot be identified, refuse (callers
    may inject their own guard)."""
    probe = Path(path).resolve()
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if sys.platform.startswith("linux"):
        return _linux_mount_guard(probe)
    if sys.platform == "darwin":
        return _darwin_mount_guard(probe)
    return (
        False,
        f"cannot verify local mount on platform {sys.platform!r}; "
        "inject local_mount_guard explicitly",
    )


def _linux_mount_guard(probe: Path) -> tuple[bool, str]:
    try:
        text = Path("/proc/mounts").read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - /proc always present on linux
        return False, f"cannot read /proc/mounts: {exc}"
    best: tuple[int, str, str] | None = None
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        mount_point = parts[1].replace("\\040", " ").replace("\\011", "\t")
        fstype = parts[2]
        mp = Path(mount_point)
        if probe == mp or mp in probe.parents:
            depth = len(mp.parts)
            if best is None or depth > best[0]:
                best = (depth, mount_point, fstype)
    if best is None:  # pragma: no cover - "/" always matches
        return False, "no matching mount point found"
    _, mount_point, fstype = best
    if fstype.lower() in _NETWORK_FS_TYPES:
        return False, f"{mount_point} is a network filesystem ({fstype})"
    return True, f"{mount_point} ({fstype})"


_DARWIN_MOUNT_RE = re.compile(r"^.+? on (.+) \(([^)]*)\)$")


def _darwin_mount_guard(probe: Path) -> tuple[bool, str]:
    try:
        out = subprocess.run(
            ["/sbin/mount"], capture_output=True, text=True, check=True, timeout=10
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
        return False, f"cannot run /sbin/mount: {exc}"
    best: tuple[int, str, list[str]] | None = None
    for line in out.splitlines():
        match = _DARWIN_MOUNT_RE.match(line.strip())
        if not match:
            continue
        mount_point, opts_text = match.groups()
        opts = [o.strip() for o in opts_text.split(",")]
        mp = Path(mount_point)
        if probe == mp or mp in probe.parents:
            depth = len(mp.parts)
            if best is None or depth > best[0]:
                best = (depth, mount_point, opts)
    if best is None:  # pragma: no cover - "/" always matches
        return False, "no matching mount point found"
    _, mount_point, opts = best
    fstype = opts[0].lower() if opts else ""
    if fstype in _NETWORK_FS_TYPES:
        return False, f"{mount_point} is a network filesystem ({fstype})"
    if "local" in opts or fstype in {"apfs", "hfs", "msdos", "exfat"}:
        return True, f"{mount_point} ({fstype})"
    return False, f"{mount_point} ({fstype}) is not flagged as a local filesystem"


# -- small file helpers --------------------------------------------------


def _write_file_fsync(
    path: Path,
    data: bytes,
    *,
    written_cb: Callable[[], None] | None = None,
    fsynced_cb: Callable[[], None] | None = None,
) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, data)
        if written_cb:
            written_cb()
        os.fsync(fd)
    finally:
        _close_quietly(fd)
    if fsynced_cb:
        fsynced_cb()


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        _close_quietly(fd)


def _read_fd(fd: int) -> bytes:
    size = os.fstat(fd).st_size
    return os.pread(fd, size, 0)


def _close_quietly(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


def _release_lock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    _close_quietly(fd)
