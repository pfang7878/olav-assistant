"""permissions.py — Platform-wide permission bypass control.

OLAV_DANGEROUSLY_SKIP_PERMISSIONS=1 disables all approval gates for testing:
  - check_approval()    in approval.py
  - scan_sandbox_code() in sandbox_guard.py
  - service_call()      write method gate in client.py

execute_sql read_only=True is NOT bypassed — that is a data integrity
constraint at the DuckDB driver level, not an approval gate.

Usage:
    # From CLI flag --dangerously-skip-permissions
    from olav.platform.safety.permissions import set_bypass
    set_bypass(True)

    # Or via env var directly
    OLAV_DANGEROUSLY_SKIP_PERMISSIONS=1 olav "deploy lab"
"""
from __future__ import annotations

import os

_ENV_VAR = "OLAV_DANGEROUSLY_SKIP_PERMISSIONS"


def is_bypass_active() -> bool:
    """Return True if the permission bypass is active."""
    return os.environ.get(_ENV_VAR) == "1"


def set_bypass(enabled: bool) -> None:
    """Enable or disable the permission bypass programmatically.

    Sets or clears the OLAV_DANGEROUSLY_SKIP_PERMISSIONS environment variable
    for the current process. Takes effect immediately for all subsequent calls
    to is_bypass_active().
    """
    if enabled:
        os.environ[_ENV_VAR] = "1"
    else:
        os.environ.pop(_ENV_VAR, None)
