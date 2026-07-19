"""Artifact-layer alarm event tests.

The library emits a stable terminal event. Notification delivery belongs to
the orchestrator adapter that consumes the event.
"""
from __future__ import annotations

import io

from renquant_artifacts.bundle_alarms import (
    create_stderr_alarm_hook,
    format_alarm_record,
)


def test_hook_emits_stable_stderr_record() -> None:
    stream = io.StringIO()
    hook = create_stderr_alarm_hook(stream=stream)

    hook("breakglass_commit", {"incident_ref": "TASK-62", "generation": 2})

    text = stream.getvalue()
    assert "ALARM[breakglass_commit]" in text
    assert "TASK-62" in text


def test_alarm_record_is_deterministic() -> None:
    assert format_alarm_record("recovery_committed", {"z": 1, "a": 2}) == (
        'ALARM[recovery_committed] {"a": 2, "z": 1}'
    )
