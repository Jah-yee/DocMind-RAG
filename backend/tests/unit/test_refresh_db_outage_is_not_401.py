"""回归（issue #103 第 2 条）：/auth/refresh 不得把 DB 故障伪装成 401。

`get_user_by_id` 原先 `except Exception: return None`，于是「数据库查询失败」
与「用户不存在」在响应层不可区分 —— /auth/refresh 把 None 统一映射成 401，
前端收到 401 就清 token 跳登录页：用户看到「登录已过期」，真实原因是数据库
挂了，而且每个请求都会中招。

本用例向 /auth/refresh 注入 DB 故障（`execute` 抛 `OperationalError`），断言
响应不是 401 而是 5xx。两条触发面分开覆盖（issue #103 报告里的边界）：
  - 不带 session_id 的 refresh token → 直接命中 `get_user_by_id` 查询；
  - 带 session_id 的 token → 先命中 `auth.py` 的会话校验查询（这条本来就不
    吞异常），证明「整库故障」与「查询级故障」都不会变成 401。

与 `tests/integration/test_auth_db_authority.py::test_db_outage_is_not_disguised_as_401`
的关系：那条护栏只覆盖 `/auth/me`，且需要真 MySQL；这条覆盖 `/auth/refresh`
的伪装面，纯 mock，跑在 ci-fast 里。
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.services.auth_service import auth_service as real_auth_service

_REFRESH = "/api/v1/auth/refresh"


def _db_that_always_fails() -> MagicMock:
    """会话替身：任何 execute 都抛 OperationalError（模拟连接中断/查询超时）。"""
    session = MagicMock()
    session.execute = AsyncMock(
        side_effect=OperationalError("SELECT ...", {}, Exception("connection lost"))
    )
    session.rollback = AsyncMock()
    session.close = AsyncMock()
    return session


def _refresh_with_dead_db(token: str):
    from app.core.database import get_db
    from app.main import app

    session = _db_that_always_fails()

    async def _override_db():
        yield session

    app.dependency_overrides[get_db] = _override_db
    try:
        # 黑名单检查依赖 Redis，与 DB 故障无关，直接放行
        with patch.object(real_auth_service, "is_token_blacklisted", AsyncMock(return_value=False)):
            return TestClient(app).post(_REFRESH, json={"refresh_token": token})
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.parametrize(
    "session_id",
    [None, 1],
    ids=["legacy-token-without-session", "session-bound-token"],
)
def test_db_outage_during_refresh_is_not_disguised_as_401(session_id):
    payload: dict = {"sub": "someone", "user_id": 1}
    if session_id is not None:
        payload["session_id"] = session_id
    token = real_auth_service.create_refresh_token(payload)

    response = _refresh_with_dead_db(token)

    assert response.status_code != 401, (
        "DB 故障被伪装成了认证失败 —— 前端会把「数据库挂了」显示成「登录已过期」。"
        f"实际响应: {response.status_code} {response.text}"
    )
    assert response.status_code >= 500, (
        f"DB 故障应当冒泡成 5xx，实际响应: {response.status_code} {response.text}"
    )
