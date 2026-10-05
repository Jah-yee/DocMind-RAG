"""回归（issue #103 第 1 条）：登录不得再写无人读取的 `user:{id}` Redis 快照。

背景：两条登录路径（form / JSON）曾是 `user:{id}` 唯一的生产写方，而这份快照
**没有任何读方** —— PR B（issue #82）之后身份判定只认 DB，`auth_service`
刻意不再读它。保留写入只会制造"陈旧快照"，还要在删除/禁用用户时维护
"删缓存"的时序（漏删一处、删早一步、并发回填都会让旧身份复活）。
所以登录侧彻底停写，本用例固定这条行为。

拦截点选在 Redis 客户端这一层（而不是断言源码里没有 `setex` 字样，后者改个
函数名就失效）：`app.core.redis.redis_client` 与 `app.core.redis.get_redis`
都被换成替身，于是无论实现用哪条路把快照写回去都会被记录 ——
直接 `redis_client.setex(...)`，或经 `RedisTools.set_cache` → `get_redis()`。
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.models.user import User
from app.services.auth_service import auth_service

_FORM_LOGIN = "/api/v1/auth/login"
_JSON_LOGIN = "/api/v1/auth/login/json"

_TEST_PASSWORD = "correct-horse-battery-staple"
# bcrypt 较慢，模块加载时算一次，两条用例共用
_HASHED_PASSWORD = auth_service.hash_password(_TEST_PASSWORD)


def _fake_user() -> User:
    """登录成功会返回的用户（不落库，仅作为查询结果）。"""
    return User(
        id=7,
        username="cache_probe",
        email="cache_probe@example.com",
        hashed_password=_HASHED_PASSWORD,
        full_name="Cache Probe",
        organization_id=None,
        role="user",
        is_active=True,
        is_superuser=False,
    )


def _redis_stand_in() -> MagicMock:
    """redis_client 替身：异步方法全是 AsyncMock，`await mock()` 不会炸。

    注意 `setex` 必须显式设为 AsyncMock —— 若留着默认的 MagicMock，
    `await_args_list` 只会自动生出一个空的 MagicMock，断言会**静默空转**；
    而 `MetricsCollector._persist_live` 会调 `setex` 写 `metrics:live:*`
    （与本条无关的键），所以要看得到真实调用记录。
    """
    client = MagicMock()
    client.setex = AsyncMock()
    client.get = AsyncMock(return_value=None)
    client.delete = AsyncMock()
    client.incr = AsyncMock(return_value=1)
    client.incrby = AsyncMock(return_value=1)
    client.expire = AsyncMock()
    return client


def _login_once(route: str):
    """走一次真实登录成功流程：只替换 DB session、Redis 客户端与审计落库。"""
    from app.core.database import get_db
    from app.main import app
    from app.services.audit_service import audit_service

    fake_user = _fake_user()
    result = MagicMock()
    result.scalar_one_or_none.return_value = fake_user
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.add = MagicMock()  # 同步方法——用 MagicMock 避免"未被 await"告警

    async def _override_db():
        yield db

    app.dependency_overrides[get_db] = _override_db
    try:
        with patch.object(audit_service, "log_activity", AsyncMock()):
            client = TestClient(app)
            if route == _FORM_LOGIN:
                return client.post(
                    route, data={"username": fake_user.username, "password": _TEST_PASSWORD}
                )
            return client.post(
                route, json={"username": fake_user.username, "password": _TEST_PASSWORD}
            )
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.parametrize("route", [_FORM_LOGIN, _JSON_LOGIN])
def test_login_success_does_not_write_user_cache(route: str):
    redis_mock = _redis_stand_in()

    with (
        patch("app.core.redis.redis_client", redis_mock),
        patch("app.core.redis.get_redis", AsyncMock(return_value=redis_mock)),
    ):
        response = _login_once(route)

    assert response.status_code == 200, f"前提不成立：登录没有成功 —— {response.text}"

    user_cache_writes = [
        call.args[0]
        for call in redis_mock.setex.await_args_list
        if call.args and str(call.args[0]).startswith("user:")
    ]
    assert user_cache_writes == [], (
        f"{route} 又写回了无人读取的 `user:{{id}}` 快照：{user_cache_writes}。"
        "该快照没有任何读方（身份判定只认 DB），只会制造下一份陈旧快照。"
    )
