"""Suite-wide env guard.

The break-glass CLI binds the REAL drift-sentinel alarm channel
(``renquant_artifacts.bundle_alarms`` -> ``renquant_common.notify.send``).
Tests must never send live notifications; ``notify.send`` honors
``RENQUANT_NO_NOTIFY`` unconditionally (its contract), so pin it for the
whole suite. Wiring-specific tests monkeypatch ``send`` itself and are
unaffected.
"""
import os

os.environ["RENQUANT_NO_NOTIFY"] = "1"
