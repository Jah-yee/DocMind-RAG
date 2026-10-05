"""issue #98：`try` 内直接 raise 的 4xx 控制流异常必须原样放行，不得被 `except Exception` 降级。

修复前：6 处端点/服务里，`try` 体内直接 `raise` 的控制流异常被同层宽
`except Exception` 捕获后「重新包装成 AppError(500)」或「吞成 200 错误字典」，
4xx 语义丢失。修复后：放行处理器在最前，异常类型与消息保持原样。

本文件按 `test_organization_tree_auth_guard.py` 的既有做法，直接调用端点/
服务函数（不经过 TestClient），逐个钉住修复点的行为。每个用例在修复前都应
失败（得到 AppError 或错误字典，而非原异常）。
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.api.v1.endpoints.chat import chat_parse_upload
from app.api.v1.endpoints.knowledge import calculate_text_similarity
from app.api.v1.endpoints.organizations import (
    get_organization_members,
    update_organization,
)
from app.api.v1.endpoints.users import get_user_profile
from app.core.config import settings
from app.exceptions import AppError, NotFoundError, ValidationError
from app.schemas.organization import OrganizationUpdate
from app.services.embedding_service import embedding_service
from app.services.file_service import file_service
from app.services.organization_service import organization_service


def _superuser() -> MagicMock:
    user = MagicMock()
    user.id = 1
    user.is_superuser = True
    user.organization_id = 1
    return user


# ---- chat.py: chat_parse_upload（try@608 直接 raise ValidationError）---------


@pytest.mark.asyncio
async def test_parse_upload_keeps_400_for_oversized_file():
    """文件过大时 ValidationError(400) 必须原样抛出，而不是被包装成 500。"""
    upload = MagicMock()
    upload.filename = "big.txt"
    upload.file = MagicMock()
    upload.file.tell = MagicMock(return_value=settings.MAX_FILE_SIZE + 1)
    upload.seek = AsyncMock()

    with pytest.raises(ValidationError) as exc_info:
        await chat_parse_upload(file=upload, current_user=MagicMock())

    assert exc_info.value.status_code == 400
    assert "文件过大" in exc_info.value.message


# ---- knowledge.py: calculate_text_similarity（try@607 直接 raise AppError）---


@pytest.mark.asyncio
async def test_similarity_keeps_original_app_error_message(monkeypatch):
    """嵌入数量不足时的 AppError 必须原样抛出，不能被再包一层前缀。"""
    monkeypatch.setattr(embedding_service, "get_embeddings", AsyncMock(return_value=[[]]))

    with pytest.raises(AppError) as exc_info:
        await calculate_text_similarity(
            text1="a", text2="b", current_user=MagicMock(), db=MagicMock()
        )

    assert exc_info.value.message == "创建向量嵌入失败"
    assert not exc_info.value.message.startswith("计算文本相似度失败")


# ---- organizations.py: update_organization（try@189 直接 raise NotFoundError）-


@pytest.mark.asyncio
async def test_update_organization_keeps_404(monkeypatch):
    """组织不存在时 NotFoundError(404) 必须原样抛出，而不是被包装成 500。"""
    monkeypatch.setattr(organization_service, "update_organization", AsyncMock(return_value=None))

    with pytest.raises(NotFoundError) as exc_info:
        await update_organization(
            org_id=99,
            org_in=OrganizationUpdate(),
            current_user=_superuser(),
            db=MagicMock(),
        )

    assert exc_info.value.status_code == 404


# ---- organizations.py: get_organization_members（try@266 直接 raise NotFoundError）


@pytest.mark.asyncio
async def test_get_organization_members_keeps_404():
    """组织不存在时 NotFoundError(404) 必须原样抛出，而不是被包装成 500。"""
    db = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=None)
    db.execute = AsyncMock(return_value=result)

    with pytest.raises(NotFoundError) as exc_info:
        await get_organization_members(org_id=99, current_user=_superuser(), db=db)

    assert exc_info.value.status_code == 404


# ---- users.py: get_user_profile（try@407 直接 raise NotFoundError）-----------


@pytest.mark.asyncio
async def test_user_profile_keeps_404():
    """用户查不到时 NotFoundError(404) 必须原样抛出，而不是被包装成 500。"""
    db = MagicMock()
    db.get = AsyncMock(return_value=None)

    with pytest.raises(NotFoundError) as exc_info:
        await get_user_profile(current_user=_superuser(), db=db)

    assert exc_info.value.status_code == 404


# ---- file_service.py: get_upload_status（try@606 直接 raise HTTPException）---


@pytest.mark.asyncio
async def test_upload_status_keeps_http_400():
    """非法哈希必须抛 400，而不是被吞成 status=error 的 200 字典。"""
    with pytest.raises(HTTPException) as exc_info:
        await file_service.get_upload_status("not-a-valid-hash")

    assert exc_info.value.status_code == 400
    assert "文件哈希不合法" in exc_info.value.detail
