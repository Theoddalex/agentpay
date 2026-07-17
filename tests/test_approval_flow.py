"""Tests for the approval-completion flow — the resume path for needs_approval.

Pins the invariants that make it safe:
  - only an admin identity can list or resolve pending approvals (an agent
    cannot clear its own)
  - approving executes and converts the row to a budget-consuming allow
  - rejecting never moves funds and never consumes budget
  - a human ok overrides ONLY the approval threshold — hard limits are re-checked
    against the current ledger at approval time
  - a pending row can be resolved once (no double-execute)
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from agentpay.api.payments import register_payment_tools
from agentpay.services.audit import AuditLog
from agentpay.services.auth import current_agent_id, current_is_admin
from agentpay.services.policy import PolicyStore

DEFAULT = dict(
    per_transaction_max="0.05", daily_max="0.20", hourly_max="0.10",
    rate_limit_per_minute=100, approval_threshold="0.02",
)
RECIPIENT = "0xAAAA000000000000000000000000000000000001"


class FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


class FakeChain:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def send_eth(self, to, amount):
        self.calls.append((to, amount))
        if self.fail:
            raise RuntimeError("rpc boom")
        return "0xEXEC"


def build(tmp_path, chain=None, enable_sends=True):
    audit = AuditLog(str(tmp_path / "audit.db"))
    store = PolicyStore(DEFAULT, {})
    mcp = FakeMCP()
    register_payment_tools(
        mcp, store, audit,
        get_chain=(lambda: chain) if chain else None,
        enable_sends=enable_sends,
    )
    return mcp.tools, audit


@pytest.fixture(autouse=True)
def _identity():
    a = current_agent_id.set("agent-1")
    b = current_is_admin.set(False)
    yield
    current_agent_id.reset(a)
    current_is_admin.reset(b)


def as_admin():
    current_is_admin.set(True)


def as_agent():
    current_is_admin.set(False)


# ---- admin gate ---------------------------------------------------------------

def test_agent_cannot_list_or_resolve(tmp_path):
    chain = FakeChain()
    tools, _ = build(tmp_path, chain=chain)
    as_agent()
    tools["request_payment"](RECIPIENT, 0.03)  # needs_approval
    assert "error" in tools["list_pending_approvals"]()
    r = tools["resolve_approval"](1)
    assert "error" in r and chain.calls == []


# ---- happy path: approve executes ---------------------------------------------

def test_needs_approval_shows_up_then_approve_executes(tmp_path):
    chain = FakeChain()
    tools, audit = build(tmp_path, chain=chain)
    as_admin()

    resp = tools["request_payment"](RECIPIENT, 0.03, "big one")
    assert resp["decision"] == "needs_approval" and resp["executed"] is False

    pending = tools["list_pending_approvals"]()["pending"]
    assert len(pending) == 1 and pending[0]["amount"] == "0.03"
    pid = pending[0]["id"]

    r = tools["resolve_approval"](pid, approve=True)
    assert r["resolved"] and r["executed"] and r["tx_hash"] == "0xEXEC"
    assert chain.calls == [(RECIPIENT, Decimal("0.03"))]

    # row is now an executed allow, attributed to the approver, and in the budget
    row = audit.history()[0]
    assert row["decision"] == "allow" and row["status"] == "executed"
    assert row["approver"] == "agent-1"
    assert sum(a for _, a, _, _ in audit.approved_spends("agent-1")) == Decimal("0.03")
    # and it's no longer pending
    assert tools["list_pending_approvals"]()["pending"] == []


# ---- reject never moves funds -------------------------------------------------

def test_reject_does_not_execute_or_consume_budget(tmp_path):
    chain = FakeChain()
    tools, audit = build(tmp_path, chain=chain)
    as_admin()
    tools["request_payment"](RECIPIENT, 0.03)
    r = tools["resolve_approval"](1, approve=False, note="not now")
    assert r["resolved"] and r["executed"] is False and r["decision"] == "rejected"
    assert chain.calls == []
    assert audit.history()[0]["status"] == "rejected"
    assert audit.approved_spends("agent-1") == []


# ---- hard limits are re-checked at approval time ------------------------------

def test_approval_refused_if_a_hard_limit_now_blocks_it(tmp_path):
    chain = FakeChain()
    tools, audit = build(tmp_path, chain=chain)
    as_admin()

    tools["request_payment"](RECIPIENT, 0.03)   # id 1 -> needs_approval (pending)
    # meanwhile the agent spends up to near the hourly cap with allowed payments
    for _ in range(5):                           # 5 * 0.015 = 0.075 allowed
        assert tools["request_payment"](RECIPIENT, 0.015)["decision"] == "allow"

    # now approving the 0.03 would push hourly to 0.105 > 0.10 — refuse it
    r = tools["resolve_approval"](1, approve=True)
    assert r["resolved"] and r["executed"] is False
    assert r["decision"] == "deny" and r["rule"] == "hourly_max"
    assert chain.calls == [(RECIPIENT, Decimal("0.015"))] * 5  # the pending one never sent
    assert audit.history()[0]["status"] == "rejected"


# ---- resolve-once -------------------------------------------------------------

def test_cannot_resolve_the_same_approval_twice(tmp_path):
    chain = FakeChain()
    tools, _ = build(tmp_path, chain=chain)
    as_admin()
    tools["request_payment"](RECIPIENT, 0.03)
    assert tools["resolve_approval"](1, approve=True)["executed"] is True
    again = tools["resolve_approval"](1, approve=True)
    assert "error" in again
    assert len(chain.calls) == 1  # executed exactly once


# ---- failed send during approval ----------------------------------------------

def test_failed_send_during_approval_is_recorded_not_counted(tmp_path):
    chain = FakeChain(fail=True)
    tools, audit = build(tmp_path, chain=chain)
    as_admin()
    tools["request_payment"](RECIPIENT, 0.03)
    r = tools["resolve_approval"](1, approve=True)
    assert r["executed"] is False and r["error"] == "rpc boom"
    row = audit.history()[0]
    assert row["decision"] == "allow" and row["status"] == "failed"
    assert audit.approved_spends("agent-1") == []  # failed send never counts


def test_unknown_payment_id_is_an_error(tmp_path):
    tools, _ = build(tmp_path, chain=FakeChain())
    as_admin()
    assert "error" in tools["resolve_approval"](999)
