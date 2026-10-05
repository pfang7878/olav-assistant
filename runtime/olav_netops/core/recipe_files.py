"""Where recipe YAML files live — builtin (shipped) and user (drafted).

This is what remains of ``recipe_seeds``, whose job — upserting those YAMLs into
the ``view_recipes`` table — stopped existing in R83.2. That round deleted the
recipe-driven view layer because its builtin recipes were the hardcoded
``(concept, command, vendor) → fields`` mapping R78 had removed from Python,
re-spelled in YAML. ``load_recipe_seeds`` survived the round with no caller and
its own test suite for two months; it is gone now, together with its validators
(2026-08-17).

The YAML files themselves are not dead, and are kept for a different reason than
they were written for: ``scripts/gen_collector_default_task.py`` derives the
collector's default command set from them — a recipe naming a command is the
strongest evidence that OLAV can turn that command into structured data. They
are a *command list* now, not a view definition. See
``topology/references/SUPPORTED_BUILTIN.md``.

Two consumers, two directories:

* ``builtin_recipes_dir()`` — the shipped files, for ``list_recipes``'s
  builtin-vs-user annotation.
* ``user_recipes_dir()`` — where ``save_recipe`` writes what a model drafted.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def builtin_recipes_dir() -> Path | None:
    """Shipped recipes directory: deployed workspace, dev tree, or the wheel.

    ``None``, not ``Path("")``: the empty path is ``PosixPath('.')``, which is
    truthy *and* exists, so a caller's ``if d and d.exists()`` passed and globbed
    the current working directory for ``*.yaml``. The same sentinel had already
    silently emptied the topology ETL from the two netops_init config readers.
    """
    try:
        from olav.core.config import get_paths_config
        base = (Path(get_paths_config().agent_dir_path) / "workspace" / "netops"
                / "topology" / "recipes" / "builtin")
        if base.is_dir():
            return base
    except Exception:  # noqa: BLE001 — config unavailable is not fatal here
        pass

    for candidate in _candidates():
        if candidate.is_dir():
            return candidate
    return None


def _package_root() -> Path:
    """The installed ``olav_netops`` directory.

    Derived from the package, not by counting `.parent`s off this module: `here`
    is already ``<pkg>/core``, so ``here.parent.parent`` is *site-packages* and
    every packaged-data path built from it missed by one level. In a source
    checkout the dev-tree candidate matched first and hid it; in the skill pack
    there is no dev tree, so `builtin_recipes_dir()` returned None, the learn
    queue lost its ranking, and nothing said so. ``config_files`` already
    resolved it this way — the two should not disagree.
    """
    import olav_netops

    return Path(olav_netops.__file__).resolve().parent


def _candidates() -> list[Path]:
    """Every place the builtin recipes may live, most specific first."""
    here = Path(__file__).resolve().parent
    pkg = _package_root()
    return [
        # Source checkout: <repo>/olav-netops/.olav/workspace/...
        here.parents[2] / ".olav" / "workspace" / "netops" / "topology" / "recipes" / "builtin",
        pkg / "data" / "recipes" / "builtin",
        # The copy inside the wheel — the only one a pip-only install has.
        pkg / "data" / "skillpack" / ".olav" / "workspace" / "netops"
        / "topology" / "recipes" / "builtin",
    ]


def user_recipes_dir() -> Path:
    """``<config>/recipes/user/`` — what ``save_recipe`` writes. Always a path.

    Unlike the builtin directory this one is a destination, so "does not exist
    yet" is normal and the caller creates it; there is nothing to report absent.
    """
    try:
        from olav.core.config import get_paths_config
        return Path(get_paths_config().config_dir) / "recipes" / "user"
    except Exception:  # noqa: BLE001
        return Path.home() / ".olav" / "config" / "recipes" / "user"
