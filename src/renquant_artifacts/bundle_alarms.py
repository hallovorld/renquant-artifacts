"""Emit structured bundle-store alarm events without routing them.

Artifacts owns the event emitted by a containment action. Orchestrator owns
the operational policy that routes that event to a sentinel channel, including
runtime configuration, retries, and run evidence. Keeping this seam local
prevents the artifact library from reading an umbrella ``.env`` or duplicating
an orchestrator notification path.
"""
from __future__ import annotations

import json
import sys
from typing import Any, Callable, IO

AlarmHook = Callable[[str, dict[str, Any]], None]


def format_alarm_record(kind: str, payload: dict[str, Any]) -> str:
    """Return the stable event record consumed by an operational adapter."""
    return f"ALARM[{kind}] {json.dumps(payload, sort_keys=True)}"


def create_stderr_alarm_hook(
    *,
    stream: IO[str] | None = None,
) -> AlarmHook:
    """Return an ``alarm_hook`` that emits a durable terminal event."""

    def alarm_hook(kind: str, payload: dict[str, Any]) -> None:
        out = stream if stream is not None else sys.stderr
        print(format_alarm_record(kind, payload), file=out)

    return alarm_hook


__all__ = [
    "AlarmHook",
    "create_stderr_alarm_hook",
    "format_alarm_record",
]
