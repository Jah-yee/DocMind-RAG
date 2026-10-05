"""issue #95 回归：非超管的组织树必须包含非私有组织（真执行 SQL）。

既有测试（`test_organization_tree_auth_guard.py`）全部用 `MagicMock` 顶替
`db.execute`，SQL 从不真正执行，因此任何 SQL 层面的缺陷都测不出来 —— 它甚至
在 docstring 里自己承认没覆盖非超管分支。本文件的价值就是：用内存 SQLite
真建表、真插入、真执行查询。

缺陷本身：`Organization.is_private` 是 SQLAlchemy 的 `InstrumentedAttribute`
（列描述符），`bool()` 它恒为 `True`。所以 `not Organization.is_private` 会先在
Python 层坍缩成 `False`，编译出字面量 `WHERE false = 1` —— 非超管恒拿到空树，
且不抛错。修法是 `Organization.is_private.is_(False)`。
"""

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base, Organization, User
from app.services.organization_service import organization_service


async def _make_seeded_session():
    """建内存库 + 建全部表 + 插入一公开一私有两条顶级组织，返回 (engine, session, public, private)。

    真跑 SQL 的关键：engine 用 aiosqlite 内存库，`create_all` 建真实 schema，
    组织行通过真实 INSERT 落库（id 由 SQLite 生成）。
    `Base.metadata.create_all` 已验证在 SQLite 上可整包建表（26 张），无需只挑单表。
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session = async_sessionmaker(engine, expire_on_commit=False)()
    public = Organization(name="PublicOrg", is_private=False, owner_id=1)
    private = Organization(name="PrivateOrg", is_private=True, owner_id=1)
    session.add_all([public, private])
    await session.commit()
    return engine, session, public, private


@pytest.mark.asyncio
async def test_non_superuser_sees_public_org_but_not_private_org():
    """非超管：树里含公开组织、不含私有组织。

    变异验证：把 service 里的 `Organization.is_private.is_(False)` 临时改回
    `not Organization.is_private`，本用例必须失败（拿到的是空树）。
    """
    engine, session, public, _private = await _make_seeded_session()
    try:
        tree = await organization_service.get_organization_tree(session, User(is_superuser=False))

        names = [node["name"] for node in tree]
        # 含公开组织 —— 改坏时此处失败（WHERE false 把两条都过滤掉）
        assert "PublicOrg" in names
        # 不含私有组织 —— 防止"干脆不过滤"式的过度修复
        assert "PrivateOrg" not in names
        # id 由 SQLite 真实生成，证明返回的是落库记录而非 mock 痕迹
        assert [node["id"] for node in tree] == [public.id]
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_superuser_sees_both_orgs():
    """对照组：超管分支不做 `is_private` 过滤，两条都能看到（真跑 SQL）。"""
    engine, session, public, private = await _make_seeded_session()
    try:
        tree = await organization_service.get_organization_tree(session, User(is_superuser=True))

        assert {node["name"] for node in tree} == {"PublicOrg", "PrivateOrg"}
        assert {node["id"] for node in tree} == {public.id, private.id}
    finally:
        await session.close()
        await engine.dispose()
