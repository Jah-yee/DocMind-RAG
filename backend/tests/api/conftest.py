"""`tests/api/` 的目录级配置。

本目录下的测试自动获得 `api` marker，不必逐个文件加装饰器。
marker 定义见 `pyproject.toml` 的 `[tool.pytest.ini_options].markers`。

这里的用例都是 API 层测试：TestClient + `app.dependency_overrides`（外加对
`minio/kafka/ES` 等外部依赖的 mock），**不连接真实服务** —— 因此可以进
`ci-fast` 里无 services 的 PR 门禁（backend-tests job）。
"""

from pathlib import Path

import pytest

_API_DIR = Path(__file__).resolve().parent


def pytest_collection_modifyitems(config, items):
    """给 `tests/api/` 下收集到的每个用例补上 `api` marker。

    注意：`pytest_collection_modifyitems` 是 **session 级** hook —— 无论它定义在哪个
    `conftest.py`，收到的都是整个 session 收集到的**完整 items 列表**。运行
    `pytest tests/` 时其它目录的用例同样会传进来，所以必须按路径过滤，
    不能无条件 `add_marker`，否则全仓用例都会被标成 api。

    与 `tests/integration/conftest.py` 的同名 hook 互不干扰：各自按自己的目录过滤，
    因此本目录的用例只带 `api` marker，不会被误打 `integration`。
    """
    for item in items:
        if not item.path.is_relative_to(_API_DIR):
            continue
        if item.get_closest_marker("api") is None:
            item.add_marker(pytest.mark.api)
