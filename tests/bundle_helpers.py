"""Shared helpers for the transactional bundle-store test suite."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from renquant_artifacts.bundle_schema import BUNDLE_MEMBER_NAMES
from renquant_artifacts.bundle_store import BundleStore

PANEL = BUNDLE_MEMBER_NAMES[0]
CAL = BUNDLE_MEMBER_NAMES[1]

_CLOCK_INSTANCES = 0


class TickingClock:
    """Deterministic clock: every call advances one second; each instance
    starts in its own disjoint minute so bundles from different store
    instances never collide on created_at."""

    def __init__(self, start: datetime | None = None) -> None:
        global _CLOCK_INSTANCES
        _CLOCK_INSTANCES += 1
        self.now = start or (
            datetime(2026, 7, 18, 3, 0, 0, tzinfo=timezone.utc)
            + timedelta(hours=_CLOCK_INSTANCES)
        )

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


def make_members(tag: str = "a") -> dict[str, bytes]:
    return {
        PANEL: f'{{"panel": "{tag}", "wf_gate_verdict": "PASS"}}\n'.encode(),
        CAL: f'{{"calibration": "{tag}"}}\n'.encode(),
    }


def make_authorization(tool: str = "wf_promote", **overrides: Any) -> dict[str, Any]:
    auth: dict[str, Any] = {
        "tool": tool,
        "tool_version": "1.0.0",
        "actor": {"os_user": "testuser", "operator": "renhao"},
        "source": {"wf_run_id": "wf-2026-07-18", "verdict_id": "PASS-001"},
        "inputs": {"panel": "sha256:" + "0" * 64},
    }
    auth.update(overrides)
    return auth


def make_breakglass_authorization(incident_ref: str = "TASK-62") -> dict[str, Any]:
    return make_authorization(
        tool="bundle_breakglass", source={"incident_ref": incident_ref}
    )


def make_bindings() -> dict[str, Any]:
    return {
        "scorer_fingerprint": "f" * 16,
        "calibrator_scorer_binding": "f" * 16,
        "wf_gate_verdict": "PASS",
    }


def make_store(root: Path, **kwargs: Any) -> BundleStore:
    kwargs.setdefault("local_mount_guard", lambda p: (True, "test-injected"))
    kwargs.setdefault("clock", TickingClock())
    return BundleStore(root, **kwargs)


def publish_simple(store: BundleStore, tag: str = "a", **kwargs: Any):
    kwargs.setdefault("bindings", make_bindings())
    kwargs.setdefault("authorization", make_authorization())
    return store.publish(make_members(tag), **kwargs)


class AlarmRecorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, kind: str, payload: dict[str, Any]) -> None:
        self.events.append((kind, payload))

    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.events]
