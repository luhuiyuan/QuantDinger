from pathlib import Path

import pytest

from app.services import community_service
from app.services.community_service import CommunityService


MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "init.sql"


def test_existing_indicator_tables_receive_marketplace_columns_before_indexes():
    sql = MIGRATION.read_text(encoding="utf-8")
    asset_upgrade = "ALTER TABLE qd_indicator_codes ADD COLUMN IF NOT EXISTS asset_type"
    source_upgrade = "ALTER TABLE qd_indicator_codes ADD COLUMN IF NOT EXISTS source_script_source_id"
    source_index = "CREATE INDEX IF NOT EXISTS idx_indicator_codes_source_script"

    assert asset_upgrade in sql
    assert source_upgrade in sql
    assert sql.index(source_upgrade) < sql.index(source_index)


def test_existing_script_source_tables_receive_asset_type_before_index():
    sql = MIGRATION.read_text(encoding="utf-8")
    asset_upgrade = "ALTER TABLE qd_script_sources ADD COLUMN IF NOT EXISTS asset_type"
    asset_index = "CREATE INDEX IF NOT EXISTS idx_script_sources_asset_type"

    assert asset_upgrade in sql
    assert sql.index(asset_upgrade) < sql.index(asset_index)


def test_existing_backtest_runs_receive_v2_columns_before_indexes():
    sql = MIGRATION.read_text(encoding="utf-8")
    source_upgrade = "ALTER TABLE qd_backtest_runs ADD COLUMN IF NOT EXISTS source_id"
    market_type_upgrade = "ALTER TABLE qd_backtest_runs ADD COLUMN IF NOT EXISTS market_type"
    params_upgrade = "ALTER TABLE qd_backtest_runs ADD COLUMN IF NOT EXISTS params_json"
    manifest_upgrade = "ALTER TABLE qd_backtest_runs ADD COLUMN IF NOT EXISTS manifest_json"
    source_index = "CREATE INDEX IF NOT EXISTS idx_backtest_runs_source_id"

    for upgrade in (source_upgrade, market_type_upgrade, params_upgrade, manifest_upgrade):
        assert upgrade in sql
        assert sql.index(upgrade) < sql.index(source_index)


def test_author_published_surfaces_database_errors(monkeypatch):
    def fail_connection():
        raise RuntimeError("schema mismatch")

    monkeypatch.setattr(community_service, "get_db_connection", fail_connection)

    with pytest.raises(RuntimeError, match="schema mismatch"):
        CommunityService().get_author_published(user_id=7)
