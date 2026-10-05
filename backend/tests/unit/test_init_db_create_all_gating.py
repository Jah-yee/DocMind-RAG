"""init_db() 的 create_all 只在空库上跑的回归（issue #150 §1.5）。

create_all 不会给已有表补列；在非空库上运行只会在缺表时静默造出与迁移路径
口径不同的表——这是 issue #150 schema 漂移的「另一半」来源。init_db 现在
把 create_all 限制在空库引导场景。
"""
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core import database as database_module


@pytest.fixture
async def sqlite_engine(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'init_db_gating.db'}")
    yield engine
    await engine.dispose()


async def _sqlite_table_count(engine) -> int:
    async with engine.connect() as conn:
        return (await conn.execute(text(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'"
        ))).scalar()


async def test_create_all_runs_on_empty_database(sqlite_engine, monkeypatch):
    """空库：init_db 正常引导出全量 schema（开发首启路径不受影响）。"""
    monkeypatch.setattr(database_module, "engine", sqlite_engine)

    await database_module.init_db()

    assert await _sqlite_table_count(sqlite_engine) > 20


async def test_create_all_skipped_on_non_empty_database(sqlite_engine, monkeypatch):
    """非空库：init_db 不再跑 create_all —— 缺的表不会被静默重建出来。"""
    monkeypatch.setattr(database_module, "engine", sqlite_engine)

    await database_module.init_db()  # 空库引导
    async with sqlite_engine.begin() as conn:
        await conn.execute(text("DROP TABLE chat_messages"))

    await database_module.init_db()  # 非空 → 应跳过

    async with sqlite_engine.connect() as conn:
        recreated = (await conn.execute(text(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type = 'table' AND name = 'chat_messages'"
        ))).scalar()
    assert recreated == 0, "非空库上 init_db 不应运行 create_all（缺失的表不被静默重建）"
