from __future__ import annotations

import importlib.util
import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
CHECK_PATH = REPO_ROOT / "scripts" / "check_external_data_inventory.py"
MANIFEST_PATH = REPO_ROOT / "docs" / "architecture" / "EXTERNAL_DATA_OUTBOUND_INVENTORY.json"


def _load_check_module():
    spec = importlib.util.spec_from_file_location("check_external_data_inventory", CHECK_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_repository_outbound_inventory_has_no_unclassified_imports():
    check = _load_check_module()
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    assert check.validate_manifest(manifest, require_zero_bypass=False) == []


def test_inventory_allowlist_is_limited_to_approved_non_data_boundaries():
    check = _load_check_module()
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    exclusions = {
        entry["allowlist_category"]
        for entry in manifest["entries"]
        if entry["scope"] == "out_of_scope"
    }

    assert exclusions <= check.ALLOWED_EXCLUSION_CATEGORIES
    assert not exclusions - {"trading", "llm", "notification", "payment", "oauth", "infrastructure"}


def test_zero_bypass_gate_passes_repository_and_rejects_a_reintroduced_legacy_path():
    check = _load_check_module()
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert check.validate_manifest(manifest, require_zero_bypass=True) == []

    entry = next(item for item in manifest["entries"] if item["scope"] == "in_scope")
    entry["migration_status"] = "legacy_unmigrated"
    entry["router_entries"] = []
    errors = check.validate_manifest(manifest, require_zero_bypass=True)
    assert f"{entry['path']}: cutover blocks legacy_unmigrated outbound data access" in errors
    assert f"{entry['path']}: router_entries must be a non-empty text list" in errors


def test_zero_bypass_gate_accepts_only_routed_calling_features(monkeypatch):
    check = _load_check_module()
    path = "backend_api_python/app/services/data_routing/adapters/example.py"
    manifest = {
        "schema_version": 1,
        "entries": [
            {
                "path": path,
                "scope": "in_scope",
                "owner_module": "app.features.example",
                "detected_clients": ["requests"],
                "calling_features": ["example.feature"],
                "adapters": ["example"],
                "capabilities": ["example_data"],
                "credential_sources": ["provider credential store"],
                "migration_status": "routed",
                "router_entries": ["app.features.example:load -> DataRouter.execute"],
            }
        ],
    }
    monkeypatch.setattr(check, "scan_source_files", lambda: {path: ["requests"]})

    assert check.validate_manifest(manifest, require_zero_bypass=True) == []
