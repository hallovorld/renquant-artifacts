"""Bind bundle-store alarm hooks to the REAL drift-sentinel alarm channel.

RFC RenQuant#492 §2.4: a break-glass commit ALWAYS alarms via the drift
sentinel (it is by definition a containment event under the AC3
protocol). The orchestrator's sentinel (renquant-orchestrator
``ops/run_surface_drift_check.py``) delivers its alarms through
``renquant_common.notify.send`` — the fleet's single canonical ntfy
sender (compliance campaign B6) — with the topic resolved from
``$RQ_ROOT/.env`` (see renquant-orchestrator ``ops/liveness_common.py``,
``alert()``). renquant-common is an install dependency of this package,
so binding the SAME channel here is in-boundary: no orchestrator code is
imported, and the alarm lands on exactly the topic the sentinel's alarms
land on.

Contract of the returned hook (store seam ``alarm_hook(kind, payload)``):

* the stderr ``ALARM[<kind>] <payload-json>`` line is ALWAYS emitted
  first — the durable in-terminal record survives even a dead network;
* then one ntfy notification is sent via ``renquant_common.notify.send``
  (which by its own contract NEVER raises and honors
  ``RENQUANT_NO_NOTIFY``);
* the hook itself never raises: an unimportable renquant-common degrades
  to a loud stderr warning. An alarm-channel failure must not block a
  containment action — but it is never silent.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, IO, Mapping

from .bundle_store_location import DEFAULT_RQ_ROOT, RQ_ROOT_ENV

#: Title prefix on every bundle-store alarm notification.
ALARM_TITLE_PREFIX = "[bundle-store]"

AlarmHook = Callable[[str, dict[str, Any]], None]


def sentinel_env_file(environ: Mapping[str, str] | None = None) -> Path:
    """The env file the sentinel channel resolves its ntfy topic from."""
    env = os.environ if environ is None else environ
    return Path(env.get(RQ_ROOT_ENV) or DEFAULT_RQ_ROOT) / ".env"


def create_sentinel_alarm_hook(
    *,
    rq_root: str | os.PathLike[str] | None = None,
    stream: IO[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> AlarmHook:
    """Return an ``alarm_hook`` bound to the drift-sentinel ntfy channel.

    ``rq_root`` overrides the ``RQ_ROOT`` env / default umbrella checkout
    used to locate ``.env`` (topic resolution only). ``stream`` defaults
    to ``sys.stderr``.
    """
    env_file = (
        Path(rq_root) / ".env" if rq_root is not None else sentinel_env_file(environ)
    )

    def alarm_hook(kind: str, payload: dict[str, Any]) -> None:
        out = stream if stream is not None else sys.stderr
        body = json.dumps(payload, sort_keys=True)
        print(f"ALARM[{kind}] {body}", file=out)
        try:
            from renquant_common.notify import send
        except ImportError as exc:
            print(
                f"WARNING: renquant_common.notify unavailable ({exc}); "
                f"bundle-store alarm NOT sent to the sentinel channel: "
                f"{kind}: {body}",
                file=out,
            )
            return
        # send() never raises and honors RENQUANT_NO_NOTIFY (its contract).
        send(f"{ALARM_TITLE_PREFIX} {kind}", body, env_file=env_file)

    return alarm_hook


__all__ = [
    "ALARM_TITLE_PREFIX",
    "AlarmHook",
    "create_sentinel_alarm_hook",
    "sentinel_env_file",
]
