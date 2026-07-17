"""MCP tools — the server's public surface (the transport layer).

These are what an agent (or Claude Desktop, Cursor, anyone) sees. Each tool is
thin: it gathers inputs, delegates to the services (policy / audit / chain), and
returns a plain dict. All the money-guarding logic lives in the policy engine,
NOT here.

The star is `request_payment`. Its critical section — read history, evaluate,
record, send — runs under a per-agent lock so two concurrent requests can't both
pass the same budget check (check-then-act must be atomic). The attempt is
recorded BEFORE the send, then stamped executed/failed, so money can never move
without a corresponding audit row.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from agentpay.schemas.schemas import Decision, PaymentRequest, SpendRecord
from agentpay.services.audit import AuditLog
from agentpay.services.auth import current_agent_id
from agentpay.services.policy import PolicyEngine, PolicyStore

# widest policy window is daily; only the last 24h can affect a decision.
_BUDGET_WINDOW = timedelta(hours=24)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def register_payment_tools(
    mcp,
    store: PolicyStore,
    audit: AuditLog,
    get_chain=None,
    enable_sends: bool = False,
) -> None:
    """Attach the payment tools to an MCP server.

    `get_chain` is a zero-arg callable returning a Chain, invoked lazily so the
    server can run for policy demos without web3/an RPC configured.
    """

    # One lock per agent: serialises each agent's read-check-record-send cycle
    # without blocking unrelated agents. (Single-process assumption — see README;
    # multi-worker deployments need a DB-level lock, not yet supported.)
    _locks: dict[str, threading.Lock] = defaultdict(threading.Lock)

    @mcp.tool()
    def request_payment(recipient: str, amount: float, reason: str = "") -> dict:
        """Request to pay some ETH to an address. The spend policy decides whether
        it is allowed, blocked, or requires human approval. Use this whenever you
        need to send a payment; do not attempt to move funds any other way.

        Args:
            recipient: destination 0x address
            amount: amount of ETH to send
            reason: what the payment is for (recorded in the audit log)
        """
        # Identity comes from authentication (Bearer key over HTTP, or the
        # configured local identity over stdio) — never from the agent's input,
        # which could simply lie about who it is.
        agent_id = current_agent_id.get()
        now = _now()

        # Validate the amount at the boundary: reject NaN/Infinity before it can
        # reach (and crash) the policy engine's comparisons.
        try:
            amt = Decimal(str(amount))
            if not amt.is_finite():
                raise InvalidOperation
        except (InvalidOperation, ValueError):
            return _reject(audit, agent_id, recipient, amount, reason, now,
                           "amount must be a finite number", "amount_finite")

        request = PaymentRequest(agent_id=agent_id, recipient=recipient,
                                 amount=amt, reason=reason)

        # Validate recipient format before we ever return ALLOW.
        if not _looks_like_address(recipient):
            return _reject(audit, agent_id, recipient, amount, reason, now,
                           f"recipient {recipient!r} is not a valid address",
                           "recipient_format")

        with _locks[agent_id]:
            # 1. THIS agent's recent spends (bounded to the budget window) + policy.
            history = [
                SpendRecord(recipient=r, amount=a, timestamp=t)
                for (r, a, t) in audit.approved_spends(agent_id, since=now - _BUDGET_WINDOW)
            ]
            engine = PolicyEngine(store.for_agent(agent_id))
            decision = engine.evaluate(request, history, now)

            # 2. Record the attempt BEFORE any send, so nothing goes unlogged.
            row_id = audit.record(request, decision, now)

            # 3. Execute only on outright ALLOW with sends enabled.
            tx_hash = None
            executed = False
            error = None
            if decision.decision is Decision.ALLOW and enable_sends and get_chain:
                try:
                    tx_hash = get_chain().send_eth(request.recipient, request.amount)
                    executed = True
                    audit.mark_executed(row_id, tx_hash)
                except Exception as e:  # noqa: BLE001 - record every outcome
                    error = str(e)
                    audit.mark_failed(row_id, error)

        return {
            "decision": decision.decision.value,
            "allowed": decision.allowed,
            "rule": decision.rule,
            "detail": decision.reason,
            "executed": executed,
            "tx_hash": tx_hash,
            "error": error,
        }

    @mcp.tool()
    def get_balance(address: str) -> dict:
        """Get the ETH balance of an address (read-only)."""
        if not get_chain:
            return {"error": "chain not configured"}
        return {"address": address, "balance_eth": str(get_chain().get_balance(address))}

    @mcp.tool()
    def get_gas_price() -> dict:
        """Get the current gas price in gwei (read-only)."""
        if not get_chain:
            return {"error": "chain not configured"}
        return {"gas_price_gwei": str(get_chain().gas_price_gwei())}

    @mcp.tool()
    def get_audit_log() -> dict:
        """Return the full history of this agent's payment attempts and what the
        policy decided about each — approved, denied, or executed."""
        return {"entries": audit.history(current_agent_id.get())}


def _looks_like_address(addr: str) -> bool:
    """Cheap 0x + 40-hex check (avoids importing web3 for validation)."""
    if not isinstance(addr, str) or not addr.startswith("0x") or len(addr) != 42:
        return False
    try:
        int(addr, 16)
        return True
    except ValueError:
        return False


def _reject(audit, agent_id, recipient, amount, reason, now, detail, rule) -> dict:
    """Record a boundary-level denial and return the standard response shape."""
    from agentpay.schemas.schemas import PolicyDecision

    decision = PolicyDecision(Decision.DENY, detail, rule)
    request = PaymentRequest(
        agent_id=agent_id,
        recipient=str(recipient),
        amount=Decimal(0),
        reason=reason,
    )
    audit.record(request, decision, now)
    return {
        "decision": "deny", "allowed": False, "rule": rule, "detail": detail,
        "executed": False, "tx_hash": None, "error": None,
    }
