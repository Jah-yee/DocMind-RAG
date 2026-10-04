"""Tenant-filter edge cases for execute_sql's org injection.

The first round of fixes only required that `organization_id = <caller's org>`
appear somewhere in the query before skipping injection. That still let an OR
widen the result set, and let a SELECT-list projection masquerade as a filter.
"""

from app.agent.tools.code_execution import _inject_org_filter


def _injects(query: str, org: int) -> bool:
    """True when a caller-org constraint was added or strengthened."""
    out = _inject_org_filter(query, org)
    if out == query:
        return False
    return f"organization_id = {org}" in out or f"{org} = organization_id" in out


class TestOrgFilterEdges:
    def test_other_org_predicate_does_not_disable_injection(self):
        q = "SELECT id FROM documents WHERE organization_id = 99"
        assert _injects(q, 3), "a predicate for another org must not switch the filter off"

    def test_or_after_pinned_org_still_gets_scoped(self):
        """`organization_id = 3 OR 1=1` matches the org literally but reads everything."""
        q = "SELECT id FROM documents WHERE organization_id = 3 OR 1=1"
        assert _injects(q, 3), "an OR in the WHERE clause can widen past the pinned org"

    def test_or_before_pinned_org_still_gets_scoped(self):
        q = "SELECT id FROM documents WHERE 1=1 OR organization_id = 3"
        assert _injects(q, 3)

    def test_select_list_projection_is_not_a_filter(self):
        """`SELECT organization_id = 3, title FROM documents WHERE ...` is a projection."""
        q = "SELECT organization_id = 3, title FROM documents WHERE is_active = 1"
        assert _injects(q, 3), "the match must be looked for after WHERE, not in the SELECT list"

    def test_plain_pinned_org_is_left_alone(self):
        q = "SELECT id FROM documents WHERE organization_id = 3"
        assert not _injects(q, 3), "an already-correct filter should not be duplicated"

    def test_missing_filter_is_injected(self):
        q = "SELECT id FROM documents WHERE title LIKE 'x'"
        assert _injects(q, 3)

    def test_query_without_where_gets_one(self):
        q = "SELECT id FROM documents"
        assert _injects(q, 3)
