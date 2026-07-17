"""Append-only audit log.

Every payment attempt — approved, denied, or executed — is recorded. For a
spend-control product this log IS half the value: "show me everything my agent
tried to spend, and what your policy did about it."

SQLite because it's zero-setup and easy to query. Rows are INSERTed; the only
UPDATE is stamping a pending payment with its on-chain outcome (tx hash or
failure), which is why each row carries a `status`:

    recorded  - policy decision made, no send attempted
                (covers deny, needs_approval, and allow-with-sends-off)
    executed  - the transfer was broadcast; tx_hash is set
    failed    - the transfer was attempted and raised; no funds moved

Budget accounting (`approved_spends`) counts only decision='allow' rows that did
NOT fail — so needs_approval never consumes budget, and a failed send doesn't
either.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from decimal import Decimal

from agentpay.schemas.schemas import PaymentRequest, PolicyDecision


class AuditLog:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        # check_same_thread=False + a lock: the MCP runtime may execute tools on
        # the event loop today, but a future threadpool/async move must not turn
        # this into a crash. All access goes through self._lock.
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS audit (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts         TEXT    NOT NULL,
                    agent_id   TEXT    NOT NULL,
                    recipient  TEXT    NOT NULL,
                    amount     TEXT    NOT NULL,   -- Decimal as text: exact precision
                    reason     TEXT,
                    decision   TEXT    NOT NULL,   -- allow / deny / needs_approval
                    rule       TEXT    NOT NULL,
                    detail     TEXT,
                    status     TEXT    NOT NULL DEFAULT 'recorded',  -- recorded/executed/failed
                    tx_hash    TEXT,
                    error      TEXT
                )
                """
            )
            # budget queries filter by agent + recency; index makes them O(log n).
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_audit_agent_ts ON audit (agent_id, ts)"
            )
            self._conn.commit()

    def record(
        self,
        request: PaymentRequest,
        decision: PolicyDecision,
        now: datetime,
        status: str = "recorded",
        tx_hash: str | None = None,
    ) -> int:
        """Insert an attempt and return its row id (used to stamp the outcome later)."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO audit (ts, agent_id, recipient, amount, reason, decision, "
                "rule, detail, status, tx_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    now.isoformat(),
                    request.agent_id,
                    request.recipient,
                    str(request.amount),
                    request.reason,
                    decision.decision.value,
                    decision.rule,
                    decision.reason,
                    status,
                    tx_hash,
                ),
            )
            self._conn.commit()
            return cur.lastrowid

    def mark_executed(self, row_id: int, tx_hash: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE audit SET status = 'executed', tx_hash = ? WHERE id = ?",
                (tx_hash, row_id),
            )
            self._conn.commit()

    def mark_failed(self, row_id: int, error: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE audit SET status = 'failed', error = ? WHERE id = ?",
                (error, row_id),
            )
            self._conn.commit()

    def history(self, agent_id: str | None = None) -> list[dict]:
        """Return audit rows, optionally filtered to one agent, oldest first."""
        cols = ["ts", "agent_id", "recipient", "amount", "decision", "rule",
                "detail", "status", "tx_hash", "error"]
        select = f"SELECT {', '.join(cols)} FROM audit"
        with self._lock:
            if agent_id:
                rows = self._conn.execute(
                    select + " WHERE agent_id = ? ORDER BY id", (agent_id,)
                ).fetchall()
            else:
                rows = self._conn.execute(select + " ORDER BY id").fetchall()
        return [dict(zip(cols, r)) for r in rows]

    def approved_spends(
        self, agent_id: str, since: datetime | None = None
    ) -> list[tuple[str, Decimal, datetime]]:
        """(recipient, amount, ts) for spends that count toward the budget.

        Counts decision='allow' rows that did not fail — so needs_approval and
        failed sends are excluded. Optionally bounded to rows at/after `since`
        (the caller passes now-24h; the widest policy window is daily).
        """
        sql = ("SELECT recipient, amount, ts FROM audit "
               "WHERE agent_id = ? AND decision = 'allow' AND status != 'failed'")
        params: list = [agent_id]
        if since is not None:
            sql += " AND ts >= ?"
            params.append(since.isoformat())
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [(r[0], Decimal(r[1]), datetime.fromisoformat(r[2])) for r in rows]
