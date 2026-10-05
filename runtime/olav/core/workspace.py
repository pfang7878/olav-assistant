"""workspace.py — WorkspaceDeclaration (workspace.yaml parser) and binary checks.

WorkspaceDeclaration is the parsed form of a workspace.yaml file bundled in a
skill git repo. It drives `olav skill install` to create the workspace directory
structure under .olav/workspace/<name>/.

Reference: dev_docs/06. ECOSYSTEM_SPLIT_PLAN.md §1.2
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


@dataclass
class RequiresDeclaration:
    packages: list[str] = field(default_factory=list)
    binaries: list[str] = field(default_factory=list)
    env_hint: list[str] = field(default_factory=list)


@dataclass
class AgentDeclaration:
    name: str
    kind: str = "Agent"
    description: str = ""
    route_keywords: list[str] = field(default_factory=list)
    agents: list["AgentDeclaration"] = field(default_factory=list)


@dataclass
class InjectIntoCoreDeclaration:
    """Declares tools/references to be injected into the core workspace on install."""
    tools: list[str] = field(default_factory=list)
    references: list[str] = field(default_factory=list)


@dataclass
class SubWorkspaceDeclaration:
    """One entry in the ``workspaces`` list (multi-workspace install)."""
    name: str
    source: str


@dataclass
class WorkspaceDeclaration:
    name: str
    version: str = "0.1.0"
    description: str = ""
    source: str | None = None
    set_active: bool = False
    requires: RequiresDeclaration = field(default_factory=RequiresDeclaration)
    db_schema: str | None = None
    init_command: str | None = None
    agents: list[AgentDeclaration] = field(default_factory=list)
    inject_into_core: InjectIntoCoreDeclaration | None = None
    workspaces: list[SubWorkspaceDeclaration] = field(default_factory=list)

    @classmethod
    def from_yaml(cls, path: Path) -> "WorkspaceDeclaration":
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        requires_raw = data.get("requires") or {}
        requires = RequiresDeclaration(
            packages=list(requires_raw.get("packages") or []),
            binaries=list(requires_raw.get("binaries") or []),
            env_hint=list(requires_raw.get("env_hint") or []),
        )
        agents = [_parse_agent(a) for a in (data.get("agents") or [])]
        inject_raw = data.get("inject_into_core")
        inject: InjectIntoCoreDeclaration | None = None
        if inject_raw:
            inject = InjectIntoCoreDeclaration(
                tools=list(inject_raw.get("tools") or []),
                references=list(inject_raw.get("references") or []),
            )
        workspaces = [
            SubWorkspaceDeclaration(name=w["name"], source=w["source"])
            for w in (data.get("workspaces") or [])
        ]
        return cls(
            name=data["name"],  # required — raises KeyError if absent
            version=str(data.get("version", "0.1.0")),
            description=str(data.get("description", "")),
            source=data.get("source"),
            set_active=bool(data.get("set_active", False)),
            requires=requires,
            db_schema=data.get("db_schema"),
            init_command=data.get("init_command"),
            agents=agents,
            inject_into_core=inject,
            workspaces=workspaces,
        )


def _parse_agent(data: dict) -> AgentDeclaration:
    nested = [_parse_agent(a) for a in (data.get("agents") or [])]
    return AgentDeclaration(
        name=data["name"],
        kind=str(data.get("kind", "Agent")),
        description=str(data.get("description", "")),
        route_keywords=list(data.get("route_keywords") or []),
        agents=nested,
    )


def check_binary_requirements(requires: RequiresDeclaration) -> list[str]:
    """Return list of binary names that are not on PATH."""
    return [b for b in requires.binaries if shutil.which(b) is None]


# ── Active workspace resolution ───────────────────────────────────────────────

def get_active_workspace() -> str:
    """Return the active workspace name.

    Checks ``.olav/config/api.json`` (new location) first, then falls back
    to the legacy ``.olav/config/settings.json`` so existing installations
    continue to work until they run ``olav workspace use <name>`` once.

    Returns whatever name is stored in the config; defaults to "core" only
    when no valid name is found. Directory existence is NOT validated here —
    the caller is responsible for handling missing workspace directories.
    """
    import json as _json

    # Primary: api.json (stores active_workspace alongside LLM / auth config)
    api_path = Path(".olav") / "config" / "api.json"
    if api_path.exists():
        try:
            data = _json.loads(api_path.read_text(encoding="utf-8"))
            ws = data.get("active_workspace")
            if ws and str(ws).strip():
                return str(ws)
        except Exception as e:
            logger.debug("api.json active_workspace read failed: %s", e)

    # LEGACY-KEEP: pre-M2 settings.json fallback — removed as the canonical
    # source but users upgrading from <0.12 may still have the file.
    settings_path = Path(".olav") / "config" / "settings.json"
    if settings_path.exists():
        try:
            data = _json.loads(settings_path.read_text(encoding="utf-8"))
            ws = data.get("active_workspace")
            if ws and str(ws).strip():
                return str(ws)
        except Exception as e:
            logger.debug("settings.json active_workspace read failed: %s", e)
    return "core"


def resolve_workspace_path(
    *parts: str,
    workspace: str | None = None,
    workspace_root: Path | None = None,
) -> Path:
    """Resolve a path under the workspace directory with flat→nested fallback.

    Strategy:
    1. No parts → return workspace_root (or workspace_root/<workspace>)
    2. Flat structure exists (.olav/workspace/<parts>/…) → return it (backward compat)
    3. Nested structure → .olav/workspace/<workspace>/<parts>/…
       where <workspace> = explicit kwarg or active workspace from settings.json

    Args:
        *parts: Path components relative to the workspace root.
        workspace: Explicit workspace name; if None, uses get_active_workspace().
        workspace_root: Override for the base workspace directory.
                        Defaults to Path(".olav/workspace").

    Examples:
        resolve_workspace_path()                         → .olav/workspace/
        resolve_workspace_path(workspace="netops")       → .olav/workspace/netops/
        resolve_workspace_path("audit", "profiles")      → .olav/workspace/audit/profiles  (flat)
                                                        or .olav/workspace/core/audit/profiles
        resolve_workspace_path("ops", workspace="itsm")  → .olav/workspace/itsm/ops/
    """
    if workspace_root is None:
        workspace_root = (Path(".olav") / "workspace").resolve()
    else:
        workspace_root = workspace_root.resolve()

    if not parts:
        if workspace is not None:
            return workspace_root / workspace
        return workspace_root

    # Flat path check (backward compat — wins if it exists on disk)
    flat_path = workspace_root / Path(*parts)
    if flat_path.exists():
        return flat_path

    # Nested path: workspace_root / <workspace> / <parts>
    ws = workspace if workspace is not None else get_active_workspace()
    return workspace_root / ws / Path(*parts)


def resolve_workspace_root(
    workspace: str | None = None,
    workspace_root: Path | None = None,
) -> Path:
    """Return the root directory of the workspace tree or a specific workspace.

    Args:
        workspace: Workspace name; if None, returns the workspace tree root.
        workspace_root: Override for the base workspace directory.
    """
    if workspace_root is None:
        workspace_root = (Path(".olav") / "workspace").resolve()
    else:
        workspace_root = workspace_root.resolve()
    if workspace is not None:
        return workspace_root / workspace
    return workspace_root
