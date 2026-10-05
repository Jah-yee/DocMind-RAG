"""init_db() 的 create_all 只在空库上跑的回归（issue #150 §1.5）。

create_all 不会给已有表补列；在非空库上运行只会在缺表时静默造出与迁移路径
口径不同的表——这是 issue #150 schema 漂移的「另一半」来源。init_db 现在
把 create_all 限制在空库引导场景。

issue #154：判断「是否迁移建库」的判据是 alembic_version 里**有没有 revision 行**，
不是表是否存在——「表在但 0 行」（create_all 建库的常见残留）此前被误判为
迁移建库、静默走 INFO，漂移告警不响。下面 4 个状态用例覆盖该判据。
"""
import logging

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


async def _sqlite_has_table(engine, name: str) -> bool:
    async with engine.connect() as conn:
        return bool((await conn.execute(text(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = :name"
        ), {"name": name})).scalar())


def _db_log_records(caplog, min_level: int) -> list[logging.LogRecord]:
    return [
        r for r in caplog.records
        if r.name == "app.core.database" and r.levelno >= min_level
    ]


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


# ── issue #154：判据 = alembic_version 里有没有 revision 行 ─────────────────

ALEMBIC_VERSION_DDL = (
    "CREATE TABLE alembic_version "
    "(version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
)


async def test_warns_when_alembic_version_table_exists_but_has_zero_rows(
    sqlite_engine, monkeypatch, caplog
):
    """核心回归（issue #154）：表存在但 0 行 ≠ 迁移建库 → 跳过 + 漂移告警。

    改前行为：空表被误判为「schema 由迁移管理」，只打一条 INFO，告警不响——
    正是用户本地 create_all 建库 + 空 alembic_version 表的实际形态。
    """
    monkeypatch.setattr(database_module, "engine", sqlite_engine)
    await database_module.init_db()  # 模拟 create_all 建的库

    async with sqlite_engine.begin() as conn:
        await conn.execute(text(ALEMBIC_VERSION_DDL))
        await conn.execute(text("DROP TABLE chat_messages"))  # create_all 若再跑会重建它

    caplog.clear()
    await database_module.init_db()

    warnings = _db_log_records(caplog, logging.WARNING)
    assert warnings, "alembic_version 表在但 0 行：必须打漂移告警（issue #154）"
    msg = "".join(r.getMessage() for r in warnings)
    assert "alembic_version" in msg and "没有 revision 记录" in msg
    # 告警必须给出可照做的修复步骤
    assert "check_schema.py" in msg and "alembic stamp" in msg and "upgrade head" in msg
    assert not await _sqlite_has_table(sqlite_engine, "chat_messages"), (
        "告警路径同样必须跳过 create_all（缺失的表不被静默重建）"
    )


async def test_info_without_warning_when_alembic_version_has_revision(
    sqlite_engine, monkeypatch, caplog
):
    """非空 + 有 revision 行 = 确属迁移管理 → 跳过 + INFO，且无任何告警。"""
    monkeypatch.setattr(database_module, "engine", sqlite_engine)
    await database_module.init_db()

    async with sqlite_engine.begin() as conn:
        await conn.execute(text(ALEMBIC_VERSION_DDL))
        await conn.execute(text(
            "INSERT INTO alembic_version (version_num) VALUES ('beefbeef0000')"
        ))
        await conn.execute(text("DROP TABLE chat_messages"))

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="app.core.database"):
        await database_module.init_db()

    assert not _db_log_records(caplog, logging.WARNING), "有 revision 行时不应打告警"
    infos = _db_log_records(caplog, logging.INFO)
    assert any("由迁移管理" in r.getMessage() for r in infos), "应打一条迁移管理的 INFO"
    assert not await _sqlite_has_table(sqlite_engine, "chat_messages"), (
        "迁移建库同样跳过 create_all"
    )


async def test_warns_when_alembic_version_table_is_missing(
    sqlite_engine, monkeypatch, caplog
):
    """非空 + 完全没有 alembic_version 表 → 跳过 + 漂移告警（判据的另一半）。"""
    monkeypatch.setattr(database_module, "engine", sqlite_engine)
    await database_module.init_db()

    async with sqlite_engine.begin() as conn:
        await conn.execute(text("DROP TABLE chat_messages"))

    caplog.clear()
    await database_module.init_db()

    warnings = _db_log_records(caplog, logging.WARNING)
    assert warnings, "没有 alembic_version 表：必须打漂移告警"
    msg = "".join(r.getMessage() for r in warnings)
    assert "没有 alembic_version 表" in msg
    assert "check_schema.py" in msg and "alembic stamp" in msg and "upgrade head" in msg
    assert not await _sqlite_has_table(sqlite_engine, "chat_messages")


async def test_no_exception_and_warns_when_alembic_version_structure_changed(
    sqlite_engine, monkeypatch, caplog
):
    """表结构被改成非标准形态且 0 行：判据只做 COUNT(*)，不抛异常、仍打告警。

    这防的是「判据去 SELECT version_num，被改过的表结构一查就炸」的实现——
    只要表还在但无行，就必须安全地走到告警路径。
    """
    monkeypatch.setattr(database_module, "engine", sqlite_engine)
    await database_module.init_db()

    async with sqlite_engine.begin() as conn:
        # 非标准结构：没有 version_num 列
        await conn.execute(text("CREATE TABLE alembic_version (whatever TEXT)"))

    caplog.clear()
    await database_module.init_db()  # 不应抛异常

    warnings = _db_log_records(caplog, logging.WARNING)
    assert warnings, "结构被改过但 0 行：仍必须打漂移告警"
    assert "没有 revision 记录" in "".join(r.getMessage() for r in warnings)

