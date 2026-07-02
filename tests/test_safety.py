"""Safety-gate tests."""

from __future__ import annotations

from agent_harness.safety import SafetyGate


def test_destructive_action_blocked_by_default():
    d = SafetyGate().check("flaky_api", {"resource": "prod-db", "action": "delete"})
    assert not d.allowed
    assert d.rule == "destructive_not_allowlisted"


def test_destructive_action_allowed_when_allowlisted():
    gate = SafetyGate(delete_allowlist={"scratch"})
    d = gate.check("flaky_api", {"resource": "scratch", "action": "delete"})
    assert d.allowed


def test_read_action_allowed():
    d = SafetyGate().check("flaky_api", {"resource": "order-1", "action": "read"})
    assert d.allowed


def test_path_traversal_blocked():
    d = SafetyGate().check("doc_search", {"query": "../../etc/passwd"})
    assert not d.allowed
    assert d.rule == "dangerous_pattern"


def test_code_exec_probe_blocked():
    d = SafetyGate().check("doc_search", {"query": "__import__('os').system('x')"})
    assert not d.allowed


def test_sql_drop_blocked_nested_in_list():
    d = SafetyGate().check("calc_units", {"note": ["hi", "DROP TABLE users"]})
    assert not d.allowed


def test_clean_query_allowed():
    d = SafetyGate().check("doc_search", {"query": "retry backoff policy"})
    assert d.allowed
