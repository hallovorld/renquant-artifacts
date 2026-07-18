"""Sentinel alarm-hook tests (AC4 P0, RFC §2.4 always-alarm binding).

The hook must (1) always emit the stderr ``ALARM[...]`` record first,
(2) send one notification through ``renquant_common.notify.send`` — the
exact channel the orchestrator drift sentinel alarms on — with the topic
env file at ``$RQ_ROOT/.env``, and (3) never raise, even with
renquant-common missing.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import renquant_common.notify

from renquant_artifacts.bundle_alarms import (
    ALARM_TITLE_PREFIX,
    create_sentinel_alarm_hook,
    sentinel_env_file,
)
from renquant_artifacts.bundle_store_location import DEFAULT_RQ_ROOT, RQ_ROOT_ENV


def test_hook_emits_stderr_record_and_sends_on_sentinel_channel(
    tmp_path: Path, monkeypatch
) -> None:
    sent: list[dict] = []

    def fake_send(title, body, topic=None, **kwargs):
        sent.append({"title": title, "body": body, **kwargs})
        return True

    monkeypatch.setattr(renquant_common.notify, "send", fake_send)
    stream = io.StringIO()
    hook = create_sentinel_alarm_hook(rq_root=tmp_path, stream=stream)

    hook("breakglass_commit", {"incident_ref": "TASK-62", "generation": 2})

    text = stream.getvalue()
    assert "ALARM[breakglass_commit]" in text
    assert "TASK-62" in text
    assert len(sent) == 1
    assert sent[0]["title"] == f"{ALARM_TITLE_PREFIX} breakglass_commit"
    assert '"incident_ref": "TASK-62"' in sent[0]["body"]
    assert sent[0]["env_file"] == tmp_path / ".env"


def test_hook_env_file_defaults_to_rq_root_env(tmp_path: Path, monkeypatch) -> None:
    sent: list[dict] = []
    monkeypatch.setattr(
        renquant_common.notify,
        "send",
        lambda title, body, topic=None, **kw: sent.append(kw) or True,
    )
    hook = create_sentinel_alarm_hook(
        stream=io.StringIO(), environ={RQ_ROOT_ENV: str(tmp_path)}
    )
    hook("recovery_committed", {"generations": [3]})
    assert sent[0]["env_file"] == tmp_path / ".env"


def test_sentinel_env_file_resolution(tmp_path: Path) -> None:
    assert sentinel_env_file({RQ_ROOT_ENV: str(tmp_path)}) == tmp_path / ".env"
    assert sentinel_env_file({}) == Path(DEFAULT_RQ_ROOT) / ".env"


def test_hook_send_failure_does_not_raise(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        renquant_common.notify, "send", lambda *a, **k: False
    )  # notify.send returns False on failure/suppression; never raises
    stream = io.StringIO()
    hook = create_sentinel_alarm_hook(rq_root=tmp_path, stream=stream)
    hook("crash_interval_serve", {"generation": 5})
    assert "ALARM[crash_interval_serve]" in stream.getvalue()


def test_hook_survives_missing_renquant_common(tmp_path: Path, monkeypatch) -> None:
    # None in sys.modules makes `from renquant_common.notify import send`
    # raise ImportError — the unavailable-dependency shape.
    monkeypatch.setitem(sys.modules, "renquant_common.notify", None)
    stream = io.StringIO()
    hook = create_sentinel_alarm_hook(rq_root=tmp_path, stream=stream)
    hook("breakglass_commit", {"incident_ref": "INC-1"})
    text = stream.getvalue()
    assert "ALARM[breakglass_commit]" in text  # stderr record survives
    assert "WARNING" in text and "NOT sent" in text  # loudly degraded
