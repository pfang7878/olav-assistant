"""olav-netops — OLAV Network Operations domain package.

Provides:
- Network-specific DuckDB table declarations (BaseIngestTable subclasses)
- ``olav-netops`` CLI binary (init, snapshot, etc.)
- NETOPS-ONLY config extensions (NORNIR_CONFIG_PATH, TextFSM paths, etc.)

Installation::

    pip install olav-netops
    olav-netops init   # injects netops workspace + bootstraps TextFSM templates
"""

__version__ = "0.28.0"


def setup() -> None:
    """Explicit entry-point initializer — call once during plugin registration.

    Registers all olav-netops tables into the platform TableRegistry so that
    DuckDB schema migrations discover them.  Kept separate from module import
    to avoid side effects during testing or partial imports.
    """
    from olav_netops.core.tables import _register_all

    _register_all()
