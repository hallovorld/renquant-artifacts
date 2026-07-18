"""AC4 P0 revert-cleanliness: the new store-operationalization code is
imported by NOTHING on the pre-existing serving/provenance surface.

The RFC §3 rollback invariant requires that reverting the P0 commit
restores previous behavior with no artifact surgery. Proof shape (per the
phase task): an import-graph assertion — the modules this phase adds
(``bundle_store_location``, ``bundle_store_init``, ``bundle_alarms``)
are reachable ONLY from the break-glass CLI and the package facade, and
the pre-P0 registry/contract modules that today's run-bundle and registry
consumers use (``contracts``, ``registry``, ``validation``) import no
bundle-store code at all. Reverting the commit therefore removes exactly
the new modules + the break-glass edits and cannot change any serving
surface.
"""
from __future__ import annotations

import ast
from pathlib import Path

import renquant_artifacts

PKG_DIR = Path(renquant_artifacts.__file__).resolve().parent

#: Modules ADDED by the P0 operationalization commit.
P0_MODULES = {"bundle_store_location", "bundle_store_init", "bundle_alarms"}

#: The only sanctioned importers of the P0 modules (facade + the manual
#: break-glass tool + the P0 modules themselves).
ALLOWED_P0_IMPORTERS = {"__init__", "bundle_breakglass"} | P0_MODULES

#: Pre-P0 modules on today's serving/provenance surface (orchestrator
#: run-bundle build, registry resolution, manifest validation).
PRE_P0_SERVING_MODULES = {"contracts", "registry", "validation"}


def _imported_local_modules(path: Path) -> set[str]:
    """Names of same-package modules imported by ``path`` (absolute or
    relative import forms)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level > 0 and node.module:
                out.add(node.module.split(".")[0])
            elif node.level > 0 and node.module is None:
                out.update(alias.name for alias in node.names)
            elif node.module and node.module.startswith("renquant_artifacts."):
                out.add(node.module.split(".")[1])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("renquant_artifacts."):
                    out.add(alias.name.split(".")[1])
    return out


def test_p0_modules_only_imported_by_breakglass_and_facade() -> None:
    offenders: dict[str, set[str]] = {}
    for path in sorted(PKG_DIR.glob("*.py")):
        module = path.stem
        hits = _imported_local_modules(path) & P0_MODULES
        if hits and module not in ALLOWED_P0_IMPORTERS:
            offenders[module] = hits
    assert not offenders, (
        f"P0 store-operationalization modules leaked into {offenders}; "
        "the revert-cleanliness invariant (RFC §3) requires them to stay "
        "reachable only via the break-glass tool and the package facade"
    )


def test_pre_p0_serving_modules_import_no_bundle_code() -> None:
    bundle_modules = {p.stem for p in PKG_DIR.glob("bundle_*.py")}
    for module in sorted(PRE_P0_SERVING_MODULES):
        hits = _imported_local_modules(PKG_DIR / f"{module}.py") & bundle_modules
        assert not hits, (
            f"serving-surface module {module}.py imports bundle code {hits}; "
            "P0 must leave the flat-path serving/provenance surface untouched"
        )
