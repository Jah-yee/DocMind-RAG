"""R0 回归测试：工作流知识库检索的租户范围必须由服务端推导。

修复前：KnowledgeSearchNodeExecutor 从 state["input"]["organization_id"] 读取检索组织，
而 input 是未校验的请求体，任意认证用户可跨租户读取知识库。
修复后：组织由 API 层从认证用户推导、经 WorkflowEngine 显式注入，
请求体中的同名字段被忽略。
"""
import pytest

from app.schemas.workflow import WorkflowConfig, WorkflowNode
from app.services.workflow_engine import (
    KnowledgeSearchNodeExecutor,
    WorkflowEngine,
    WorkflowState,
)


def _make_node(node_id: str = "n1", node_type: str = "tool_search", data: dict | None = None) -> WorkflowNode:
    return WorkflowNode(id=node_id, type=node_type, data=data or {}, position={"x": 0, "y": 0})


def _make_state(**overrides) -> WorkflowState:
    base: WorkflowState = {
        "input": {"text": "报销流程"},
        "messages": [],
        "current_node": None,
        "node_outputs": {},
        "context": {},
        "errors": [],
        "memory": {},
        "iteration_count": 0,
        "tool_results": [],
    }
    base.update(overrides)
    return base


class TestWorkflowRetrievalOrgScope:
    @pytest.mark.asyncio
    async def test_client_supplied_organization_id_is_ignored(self, monkeypatch):
        """请求体里的 organization_id=42 不得影响实际检索组织。"""
        from app.services.rag_service import rag_service

        calls: list[int] = []

        async def fake_search(query, organization_id, top_k=5, **kwargs):
            calls.append(organization_id)
            return [{"content": "命中内容", "score": 0.9, "title": "资料"}], None

        monkeypatch.setattr(rag_service, "search_knowledge_base", fake_search)

        executor = KnowledgeSearchNodeExecutor(_make_node())
        executor.organization_id = 7  # 服务端注入（引擎从认证用户推导）
        state = _make_state(input={"text": "报销流程", "organization_id": 42})

        result = await executor.execute(state)

        assert calls == [7], "检索必须使用服务端注入的组织，而不是请求体中的 organization_id"
        assert result["context"]["knowledge_context"] == "命中内容"

    @pytest.mark.asyncio
    async def test_executor_without_injected_org_falls_back_to_default(self, monkeypatch):
        """引擎未注入组织时退回默认组织 1，且依旧忽略请求体字段。"""
        from app.services.rag_service import rag_service

        calls: list[int] = []

        async def fake_search(query, organization_id, top_k=5, **kwargs):
            calls.append(organization_id)
            return [], None

        monkeypatch.setattr(rag_service, "search_knowledge_base", fake_search)

        executor = KnowledgeSearchNodeExecutor(_make_node())
        await executor.execute(_make_state(input={"text": "x", "organization_id": 42}))

        assert calls == [1]

    def test_engine_injects_organization_id_into_search_executor(self):
        """WorkflowEngine(organization_id=...) 必须把组织注入 tool_search 执行器。"""
        config = WorkflowConfig(nodes=[_make_node("search1", "tool_search")], edges=[])
        engine = WorkflowEngine(config, organization_id=7)
        executor = engine.get_executor(config.nodes[0])

        assert isinstance(executor, KnowledgeSearchNodeExecutor)
        assert executor.organization_id == 7

    def test_engine_does_not_leak_org_into_other_executors(self):
        """注入只作用于知识库检索节点，不影响其他执行器构造。"""
        config = WorkflowConfig(nodes=[_make_node("in1", "input")], edges=[])
        engine = WorkflowEngine(config, organization_id=7)
        executor = engine.get_executor(config.nodes[0])
        assert not hasattr(executor, "organization_id")
