"""Core Utilities for OLAV.

Provides shared logic for text processing and other common tasks.
"""

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# `RecursiveCharacterTextSplitter` is imported inside `TextProcessor.__init__`,
# not here. This module's most-used export is `utc_now()` — a two-line datetime
# helper that the ingest path, the topology ETL and the audit writer all import —
# and a top-level splitter import made every one of them require
# langchain-text-splitters. Measured 2026-08-18: it is the single thing that made
# `pip install --no-deps olav` unusable for the skill pack, which needs the ingest
# path and does no chunking at all.


def utc_now() -> datetime:
    """Return the current UTC time as a timezone-aware ``datetime``.

    Canonical replacement for ``datetime.utcnow()`` (deprecated in Python 3.12)
    and ``datetime.now()`` (timezone-naive — unsafe across locales). Using this
    helper keeps snapshot_id, audit timestamps, and cross-snapshot comparisons
    consistent across dev/prod, regardless of the host's local timezone.
    """
    return datetime.now(timezone.utc)


_BACKUP_COMMANDS_PATH_ENV = "OLAV_BACKUP_COMMANDS_PATH"
_BACKUP_COMMANDS_FILENAME = "backup_only_commands.yaml"


def find_backup_commands_yaml():
    """Resolve the canonical ``backup_only_commands.yaml`` path.

    Priority (ARCH-22 C — shared by ingest_manager and every domain's
    command_registry). ADR-0002 compliance: no domain name is hardcoded;
    the lookup is generic across any extension that ships a
    ``backup_only_commands.yaml`` in its workspace.

    0. Env override: ``OLAV_BACKUP_COMMANDS_PATH`` — when set to an
       existing file, wins unconditionally. Operators can redirect the
       lookup without editing code for deployments that keep config
       outside the default ``.olav/`` tree.
    1. Per-domain workspace: any ``.olav/workspace/*/*_init/config/backup_only_commands.yaml``
       (matches ``ops/netops_init/`` today, ``k8sops/k8sops_init/`` tomorrow).
    2. Domain config: any ``.olav/config/domains/*/backup_only_commands.yaml``.
    3. Legacy flat path: ``.olav/config/backup_only_commands.yaml``.

    Returns the first existing ``Path`` or ``None`` if no file is found.
    Callers treat ``None`` as "no backup_only_commands configured".
    """
    import os
    raw = os.environ.get(_BACKUP_COMMANDS_PATH_ENV, "").strip()
    if raw:
        candidate = Path(raw)
        if candidate.is_file():
            return candidate

    from olav.core.config import _AGENT_DIR_PATH, _CONFIG_DIR

    # 1. Glob every workspace's *_init/config — covers any domain that
    #    follows the platform init convention.
    workspace_root = _AGENT_DIR_PATH / "workspace"
    if workspace_root.is_dir():
        for candidate in sorted(
            workspace_root.glob(f"*/*_init/config/{_BACKUP_COMMANDS_FILENAME}")
        ):
            if candidate.is_file():
                return candidate

    # 2. Glob every domain under config/domains/.
    domains_root = _CONFIG_DIR / "domains"
    if domains_root.is_dir():
        for candidate in sorted(
            domains_root.glob(f"*/{_BACKUP_COMMANDS_FILENAME}")
        ):
            if candidate.is_file():
                return candidate

    # 3. Legacy flat path.
    legacy = _CONFIG_DIR / _BACKUP_COMMANDS_FILENAME
    if legacy.is_file():
        return legacy
    return None


class TextProcessor:
    """Consolidated text processing utility.

    Provides semantic chunking and text cleaning for knowledge base tasks.
    """

    def __init__(self, chunk_size: int = 1024, chunk_overlap: int = 128) -> None:
        """Initialize text processor.

        Args:
            chunk_size: Characters per chunk
            chunk_overlap: Overlap between chunks
        """
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap

        from langchain_text_splitters import RecursiveCharacterTextSplitter

        self.splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size, chunk_overlap=chunk_overlap, separators=["\n\n", "\n", " ", ""]
        )

    def split_into_chunks(self, text: str) -> list[str]:
        """Split text into semantic chunks."""
        return self.splitter.split_text(text)

    def clean_text(self, text: str) -> str:
        """Remove common noise from extracted text."""
        # Remove page markers: === PAGE 1 ===
        text = re.sub(r"=== PAGE \d+ ===", "", text)
        # Remove excessive whitespace
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def build_chunk_metadata(
        self,
        chunk_content: str,
        chunk_index: int,
        total_chunks: int,
        source_file: str,
        extra_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build standard metadata for a search chunk."""
        metadata = {
            "chunk_index": chunk_index,
            "total_chunks": total_chunks,
            "source_file": source_file,
            "char_count": len(chunk_content),
        }
        if extra_metadata:
            metadata.update(extra_metadata)
        return metadata


def create_olav_directories(base_path: Any) -> dict:
    """Create the .olav/ directory structure.

    Args:
        base_path: The base path where .olav/ should be created

    Returns:
        Dict with creation status for each directory
    """
    from pathlib import Path

    base_path = Path(base_path)
    results = {}
    olav_path = base_path / ".olav"

    # Create main .olav directory
    try:
        olav_path.mkdir(parents=True, exist_ok=True)
        results["olav_root"] = {"status": "created", "path": str(olav_path)}
        logger.info(f"Created .olav/ directory at {olav_path}")
    except Exception as e:
        results["olav_root"] = {"status": "error", "error": str(e)}
        logger.error(f"Failed to create .olav/: {e}")
        return results

    subdirs = [
        "config",
        "databases",
        "logs",
        "logs/users",
        "cache",
        "workspace",
        "exports",
        "exports/snapshots",
        "exports/snapshots/latest",
        "exports/snapshots/latest/raw",
        "exports/backup",
        "exports/audit_reports",
        "tmp/snapshots",
        "tmp/staging",
        "knowledge",
    ]

    for subdir in subdirs:
        dir_path = olav_path / subdir
        try:
            dir_path.mkdir(parents=True, exist_ok=True)
            results[subdir] = {"status": "created", "path": str(dir_path)}
        except Exception as e:
            results[subdir] = {"status": "error", "error": str(e)}
            logger.error(f"Failed to create {subdir}: {e}")

    return results
