#!/usr/bin/env python3
"""Validate the repository-wide outbound-call inventory.

The baseline check proves that every source file which imports a supported
outbound transport or known data SDK has an explicit inventory classification.
The stricter ``--require-zero-bypass`` mode is the cutover gate: every in-scope
entry must be migrated and name at least one Data Router entry point.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = REPO_ROOT / "backend_api_python" / "app"
DEFAULT_MANIFEST = REPO_ROOT / "docs" / "architecture" / "EXTERNAL_DATA_OUTBOUND_INVENTORY.json"

OUTBOUND_MODULES = {
    "aiohttp",
    "akshare",
    "ccxt",
    "finnhub",
    "fredapi",
    "httpx",
    "requests",
    "twelvedata",
    "urllib.request",
    "yfinance",
}
ALLOWED_EXCLUSION_CATEGORIES = {
    "trading",
    "llm",
    "notification",
    "payment",
    "oauth",
    "infrastructure",
}
ALLOWED_SCOPES = {"in_scope", "out_of_scope", "adapter_transport"}
ALLOWED_MIGRATION_STATES = {"legacy_unmigrated", "routed", "excluded", "registered_adapter_transport"}


def _matches_outbound_module(module: str) -> bool:
    return any(module == candidate or module.startswith(f"{candidate}.") for candidate in OUTBOUND_MODULES)


def scan_source_files() -> dict[str, list[str]]:
    """Return repo-relative Python paths and the outbound modules they import."""

    discovered: dict[str, set[str]] = {}
    for path in sorted(APP_ROOT.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError) as exc:
            raise ValueError(f"cannot scan {path.relative_to(REPO_ROOT)}: {exc}") from exc
        modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(name.name for name in node.names if _matches_outbound_module(name.name))
            elif isinstance(node, ast.ImportFrom) and node.module and _matches_outbound_module(node.module):
                modules.add(node.module)
        if modules:
            discovered[path.relative_to(REPO_ROOT).as_posix()] = modules
    return {path: sorted(modules) for path, modules in discovered.items()}


def _required_text(entry: dict[str, Any], field: str, errors: list[str]) -> None:
    if not isinstance(entry.get(field), str) or not entry[field].strip():
        errors.append(f"{entry.get('path', '<unknown>')}: {field} must be non-empty text")


def _required_text_list(entry: dict[str, Any], field: str, errors: list[str]) -> None:
    value = entry.get(field)
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item.strip() for item in value):
        errors.append(f"{entry.get('path', '<unknown>')}: {field} must be a non-empty text list")


def validate_manifest(manifest: dict[str, Any], *, require_zero_bypass: bool) -> list[str]:
    errors: list[str] = []
    entries = manifest.get("entries")
    if manifest.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    if not isinstance(entries, list):
        return errors + ["entries must be a list"]

    by_path: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            errors.append("each entry must be an object")
            continue
        _required_text(entry, "path", errors)
        path = entry.get("path")
        if not isinstance(path, str) or not path:
            continue
        if path in by_path:
            errors.append(f"duplicate inventory entry: {path}")
        by_path[path] = entry
        scope = entry.get("scope")
        migration_status = entry.get("migration_status")
        if scope not in ALLOWED_SCOPES:
            errors.append(f"{path}: invalid scope {scope!r}")
        if migration_status not in ALLOWED_MIGRATION_STATES:
            errors.append(f"{path}: invalid migration_status {migration_status!r}")
        _required_text(entry, "owner_module", errors)
        _required_text_list(entry, "detected_clients", errors)

        if scope == "in_scope":
            for field in ("calling_features", "adapters", "capabilities", "credential_sources"):
                _required_text_list(entry, field, errors)
            if migration_status not in {"legacy_unmigrated", "routed"}:
                errors.append(f"{path}: in-scope entry has incompatible migration_status")
            if require_zero_bypass:
                if migration_status != "routed":
                    errors.append(f"{path}: cutover blocks legacy_unmigrated outbound data access")
                _required_text_list(entry, "router_entries", errors)
        elif scope == "out_of_scope":
            if entry.get("allowlist_category") not in ALLOWED_EXCLUSION_CATEGORIES:
                errors.append(f"{path}: invalid or missing allowlist_category")
            _required_text(entry, "exclusion_reason", errors)
            if migration_status != "excluded":
                errors.append(f"{path}: out-of-scope entry must be excluded")
        elif scope == "adapter_transport":
            expected_prefix = "backend_api_python/app/services/data_routing/adapters/"
            if not path.startswith(expected_prefix):
                errors.append(f"{path}: adapter transport must live under {expected_prefix}")
            if migration_status != "registered_adapter_transport":
                errors.append(f"{path}: adapter transport has incompatible migration_status")
            _required_text_list(entry, "adapters", errors)

    discovered = scan_source_files()
    for path, clients in discovered.items():
        entry = by_path.get(path)
        if entry is None:
            errors.append(f"unmapped outbound client import: {path} ({', '.join(clients)})")
            continue
        declared = set(entry.get("detected_clients") or [])
        missing = set(clients) - declared
        if missing:
            errors.append(f"{path}: detected_clients missing {', '.join(sorted(missing))}")
    for path in sorted(set(by_path) - set(discovered)):
        if by_path[path].get("migration_status") != "routed":
            errors.append(f"stale inventory entry has no detected outbound client import: {path}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--require-zero-bypass", action="store_true")
    args = parser.parse_args()
    manifest_path = args.manifest if args.manifest.is_absolute() else REPO_ROOT / args.manifest
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        errors = validate_manifest(manifest, require_zero_bypass=args.require_zero_bypass)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"external data inventory check failed: {exc}", file=sys.stderr)
        return 1
    if errors:
        print("external data inventory check failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    mode = "zero-bypass" if args.require_zero_bypass else "baseline"
    print(f"external data inventory ({mode}) OK: {len(manifest['entries'])} mapped source files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
