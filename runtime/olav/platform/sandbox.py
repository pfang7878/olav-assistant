"""sandbox.py — Python sandbox with per-workspace venv support (GAP-10).

Security hardening (§20 SECURITY_MODEL):
  - DuckDB monkey-patch: all duckdb.connect() calls inside sandbox are forced
    read_only=True regardless of the argument passed by user code (6.1)
  - Network namespace isolation: opt-in via OLAV_SANDBOX_NETNS=1 env var,
    prepends `unshare --net` to subprocess command when available (6.2)
  - Pre-execution sandbox_guard scan: HTTP/DB external writes blocked (3.4)

Provides:
  resolve_sandbox_python()  — workspace venv python or sys.executable
  _build_wrapper(code)      — build the script string written to subprocess
  _build_sandbox_cmd(...)   — build subprocess argv (with optional unshare)
  execute_in_sandbox()      — full sandbox entry point
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from typing import Any


# ── DuckDB safety prologue ────────────────────────────────────────────────────
# Injected at the TOP of every sandbox wrapper script.
# Forces read_only=True on all duckdb.connect() calls regardless of user code.
# Unbypassable within the subprocess — any duckdb import will see the patched
# connect() function.

_DUCKDB_READONLY_PROLOGUE = textwrap.dedent("""\
    # ── olav sandbox: DuckDB safety patch ──────────────────────────────────
    try:
        import duckdb as _olav_ddb
        _olav_ddb_orig_connect = _olav_ddb.connect
        def _olav_ddb_safe_connect(database=':memory:', read_only=False, **_kw):
            return _olav_ddb_orig_connect(database, read_only=True, **_kw)
        _olav_ddb.connect = _olav_ddb_safe_connect
    except ImportError:
        pass
    # ── end DuckDB safety patch ─────────────────────────────────────────────

""")


# ── Domain-registered sandbox prologues (ADR-0002 boundary) ──────────────
# The platform core has no knowledge of which domain extensions are
# installed. Extensions inject pre-loaded locals into the sandbox by
# registering a prologue via the ``olav.sandbox_prologues`` entry-point
# group. Each registered entry is either:
#
#   * a callable returning the Python source to inject (runtime-computed), or
#   * a string constant carrying the injected source verbatim.
#
# Each prologue must be self-guarded (try/except) so a missing dependency
# inside the prologue never breaks the sandbox.
#
# olav-netops registers an ARCH-14 ``model = load_network_model()``
# prologue in its pyproject — that lives in the olav-netops tree, not
# here.

def _collect_domain_prologues() -> str:
    """Iterate ``olav.sandbox_prologues`` entry-points and concatenate results.

    Returns an empty string when no extension registers a prologue (pure
    platform install or CI smoke). Any individual prologue that raises is
    swallowed so the rest of the sandbox still starts.
    """
    try:
        from importlib.metadata import entry_points
    except Exception:
        return ""
    collected: list[str] = []
    try:
        eps = entry_points(group="olav.sandbox_prologues")
    except Exception:
        return ""
    for ep in eps:
        try:
            obj = ep.load()
            source = obj() if callable(obj) else str(obj)
            if source:
                collected.append(source)
        except Exception:
            # A broken prologue from one extension must never prevent
            # the sandbox from starting for another.
            continue
    return "\n".join(collected)


def _build_wrapper(code: str) -> str:
    """Build the wrapper script string for sandbox execution.

    Injects:
    1. DuckDB read_only=True monkey-patch (hardening §6.1)
    2. Domain-registered prologues (``olav.sandbox_prologues`` entry-points)
    3. Standard _result serialisation boilerplate
    4. User code
    """
    domain_prologues = _collect_domain_prologues()
    return (
        _DUCKDB_READONLY_PROLOGUE
        + (domain_prologues + "\n" if domain_prologues else "")
        + textwrap.dedent("""\
            import json as _json
            import sys as _sys

            _result = None

            try:
        """)
        + textwrap.indent(code, "    ")
        + textwrap.dedent("""
            except Exception as _exc:
                import traceback
                print("__OLAV_ERROR__:" + str(_exc), file=_sys.stderr)
                print(traceback.format_exc(), file=_sys.stderr)
                _sys.exit(1)

            try:
                print("__OLAV_RESULT__:" + _json.dumps(_result))
            except (TypeError, ValueError):
                print("__OLAV_RESULT__:" + _json.dumps(str(_result)))
        """)
    )


def _build_sandbox_cmd(
    python_exe: str, script_path: str, network_isolation: bool = False
) -> list[str]:
    """Build the subprocess argv for running the sandbox script.

    When network_isolation=True and `unshare` is on PATH, prepends
    `unshare --net` to isolate the subprocess in a network namespace
    (hardening §6.2). Falls back silently if unshare is not available.

    The global fallback OLAV_SANDBOX_NETNS=1 env var is handled by
    execute_in_sandbox() before calling this function.
    """
    cmd = [python_exe, script_path]
    if network_isolation:
        unshare_bin = shutil.which("unshare")
        if unshare_bin:
            cmd = [unshare_bin, "--net"] + cmd
    return cmd


def resolve_sandbox_python() -> str:
    """Return the Python executable for sandbox execution.

    If the active workspace has a .venv, return its python binary.
    Otherwise fall back to sys.executable.
    """
    from olav.core.workspace import get_active_workspace

    active = get_active_workspace()
    if active:
        venv_python = Path(".olav") / "workspace" / active / ".venv" / "bin" / "python"
        if venv_python.exists():
            return str(venv_python)
    return sys.executable


def execute_in_sandbox(
    code: str,
    timeout: int = 60,
    cwd: str | None = None,
    network_isolation: bool | None = None,
) -> dict[str, Any]:
    """Execute Python code in a sandboxed subprocess.

    Security layers applied:
    1. sandbox_guard pre-scan: rejects HTTP mutations and DB write patterns
    2. DuckDB monkey-patch: all duckdb.connect() forced read_only=True
    3. Network namespace (opt-in): blocks all outbound network when enabled

    Local filesystem operations are always allowed.

    Args:
        code:              Python source code. Set `_result` to return structured data.
        timeout:           Max execution time in seconds.
        cwd:               Working directory (default: current directory).
        network_isolation: Whether to run in an isolated network namespace (unshare --net).
                           True  — no external network; use for pure-compute tools
                                   (simulation, diff analysis, topology calculations).
                           False — external network allowed; use when tool code calls
                                   external APIs (e.g. lab agent httpx → clab REST API).
                           None  — inherit from OLAV_SANDBOX_NETNS env var (default).
                           When in doubt: prefer True for new tools that don't need network.

    Returns:
        {"status": "success"|"error"|"requires_approval", "result": ..., ...}
    """
    from olav.platform.safety.sandbox_guard import scan_sandbox_code

    # Resolve network isolation: explicit param overrides env var
    if network_isolation is None:
        network_isolation = os.environ.get("OLAV_SANDBOX_NETNS") == "1"

    guard = scan_sandbox_code(code, network_isolation=bool(network_isolation))
    if guard.hard_block:
        return {
            "status": "hard_block",
            "reason": guard.reason,
            "matched_pattern": guard.matched_pattern,
            "suggested_action": guard.suggested_action,
            "result": None,
        }
    if guard.requires_approval:
        return {
            "status": "requires_approval",
            "reason": guard.reason,
            "matched_pattern": guard.matched_pattern,
            "suggested_action": guard.suggested_action,
            "result": None,
        }

    python_exe = resolve_sandbox_python()
    wrapper = _build_wrapper(code)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(wrapper)
        script_path = f.name

    cmd = _build_sandbox_cmd(python_exe, script_path, network_isolation=network_isolation)

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd or str(Path.cwd()),
        )
    except subprocess.TimeoutExpired:
        Path(script_path).unlink(missing_ok=True)
        return {
            "status": "error",
            "error": f"code execution timed out after {timeout}s",
            "result": None,
            "stdout": "",
            "stderr": "",
        }
    finally:
        Path(script_path).unlink(missing_ok=True)

    stdout = proc.stdout or ""
    stderr = proc.stderr or ""

    result_value = None
    clean_lines = []
    for line in stdout.splitlines():
        if line.startswith("__OLAV_RESULT__:"):
            try:
                result_value = json.loads(line[len("__OLAV_RESULT__:"):])
            except json.JSONDecodeError:
                result_value = line[len("__OLAV_RESULT__:"):]
        else:
            clean_lines.append(line)

    if proc.returncode != 0:
        error_msg = ""
        for line in stderr.splitlines():
            if line.startswith("__OLAV_ERROR__:"):
                error_msg = line[len("__OLAV_ERROR__:"):]
                break
        if not error_msg:
            error_msg = stderr.strip().split("\n")[-1] if stderr.strip() else "non-zero exit"
        return {
            "status": "error",
            "error": error_msg,
            "result": None,
            "stdout": "\n".join(clean_lines),
            "stderr": stderr,
        }

    return {
        "status": "success",
        "error": None,
        "result": result_value,
        "stdout": "\n".join(clean_lines),
        "stderr": stderr,
    }
