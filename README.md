# agentpay

**Programmable spend limits and audit trails so AI agents can pay for things
without risking the wallet.**

AI agents are probabilistic — they can be prompt-injected, loop, or simply
choose the wrong tool. The moment an agent can move money, one bad decision is
irreversible. `agentpay` is the guardrail layer between an agent and an Ethereum
wallet: every payment the agent requests is checked against a policy it **cannot
override**, and every attempt is logged.

Think *corporate-card controls (Ramp/Brex) or Stripe Radar — but for agents.*

## How it works

```
Agent: "pay 0.03 ETH to 0xabc… for the data API"
        │  (MCP tool call: request_payment)
        ▼
   ┌───────────────────────────── agentpay ─────────────────────────────┐
   │  policy engine:  per-tx cap · hourly/daily budget · allow/deny      │
   │                  list · rate limit · human-approval threshold        │
   └─────────────────────────────────────────────────────────────────────┘
        │ ALLOW → sign & send (testnet)      │ DENY → block + log
        │ NEEDS_APPROVAL → wait for human    ▼
        ▼                              agent gets a clear reason
   tx executes, logged                every attempt is audited
```

The **MCP server is the product**; the LangChain agent in `examples/` is just
one client. Any MCP-aware client (Claude Desktop, Cursor, another agent) can use
the same server.

## Design principles

- **Non-custodial, testnet-first.** Defaults to Sepolia. Sends are OFF until you
  explicitly enable them. Never put a mainnet key behind an autonomous agent.
- **The policy engine is pure logic** (`src/agentpay/services/policy.py`) — no
  I/O — so it is exhaustively unit-tested. The code guarding money is the code
  under the most tests (46 across the engine, auth, audit, and payment flow).
- **Config, not code.** Limits live in `policy.yaml`.

## Layout

```
main.py                          # repo-root shim (python main.py)
src/agentpay/
├── main.py                      # console entrypoint (`agentpay`)
├── application.py               # app factory: create_application()
├── api/payments.py              # MCP tools (transport)
├── services/
│   ├── policy.py                # ⭐ the policy engine — pure, tested
│   ├── audit.py                 # append-only SQLite audit log
│   ├── auth.py                  # Bearer API-key auth + per-request identity
│   ├── chain.py                 # web3.py wrapper (Sepolia)
│   └── wallet.py                # throwaway testnet key
├── schemas/schemas.py           # contracts (Decimal money, dataclasses)
└── configs/base.py              # pydantic settings
tests/                           # 46 tests — policy, auth, audit, payment flow
examples/demo_agent.py           # a LangChain agent that uses the server
```

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[demo,dev]"
cp .env.example .env

pytest                 # prove the policy engine
agentpay               # run the MCP server (stdio)
python examples/demo_agent.py   # watch an agent get allowed / blocked / gated
```

## Deploying it

The wallet owner runs the server; agents connect as clients and set nothing.

**Local (stdio)** — each MCP client spawns its own server process:

```json
{"agentpay": {"transport": "stdio", "command": "agentpay"}}
```

**Hosted (HTTP)** — one server for the whole org; developers get a URL and an
API key. The server **refuses to start without keys** (an open endpoint would
mean anyone who can reach it can spend the budget):

```bash
TRANSPORT=streamable-http \
AGENTPAY_API_KEYS='sk-supp-…:support-bot,sk-proc-…:procurement' agentpay
# or
docker build -t agentpay . && docker run -p 8000:8000 \
  -e AGENTPAY_API_KEYS='…' -v $(pwd)/policy.yaml:/app/policy.yaml agentpay
```

```json
{"agentpay": {"transport": "streamable_http",
              "url": "http://payments.internal:8000/mcp",
              "headers": {"Authorization": "Bearer sk-supp-…"}}}
```

The API key is the agent's identity: it selects that agent's policy section in
`policy.yaml` and attributes its audit trail. The same request can be denied
for `support-bot` (0.01/tx cap) and allowed for `procurement` (0.05/tx) —
identity decides. Unauthenticated requests get a 401 before any tool runs.

Either way, the client's agent code never sees the policy, the keys, or the
audit log — it only gets `request_payment` and a verdict.

> **Hosted-mode operational notes.** Bearer keys travel in headers — terminate
> TLS at your ingress/reverse proxy; never expose the plain HTTP port publicly.
> The server is **single-process** today: the per-agent budget lock guarantees
> no double-spend within one process, but running multiple workers/replicas
> against one `audit.db` is not yet safe (needs DB-level locking). Run one
> replica per wallet until then.

## Status

MVP in progress. Policy engine + audit + MCP server done; demo agent next.
