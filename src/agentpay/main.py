"""Console entrypoint — `agentpay` on the command line (or `uvx agentpay`).

Transport comes from settings/.env:
  TRANSPORT=stdio            local: each MCP client spawns its own server;
                             the OS is the auth boundary, identity = AGENT_ID
  TRANSPORT=streamable-http  hosted: one server at http://HOST:PORT/mcp.
                             Requires AGENTPAY_API_KEYS ("key:agent-id,...");
                             refuses to start without them unless
                             ALLOW_ANONYMOUS=true is set explicitly.
"""

import logging
import sys

from agentpay.application import create_application
from agentpay.configs.base import settings
from agentpay.services.auth import (
    AuthMiddleware,
    current_agent_id,
    current_is_admin,
    parse_api_keys,
)


def main() -> None:
    # Logs go to STDERR — stdout is the MCP protocol channel over stdio, so
    # writing logs there would corrupt it. This lights up the agentpay.* loggers.
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    mcp = create_application()

    if settings.transport != "streamable-http":
        # stdio: local single-owner use; identity is configured, not proven. The
        # operator owns the box, so they're also the approver (is_admin).
        current_agent_id.set(settings.agent_id)
        current_is_admin.set(True)
        mcp.run()
        return

    # --- hosted mode ---
    api_keys = parse_api_keys(settings.agentpay_api_keys)
    admin_keys = parse_api_keys(settings.agentpay_admin_keys)
    if not api_keys and not settings.allow_anonymous:
        sys.exit(
            "agentpay: refusing to serve HTTP without authentication.\n"
            "This server fronts a wallet — an open endpoint means anyone who can\n"
            "reach it can spend the budget. Set AGENTPAY_API_KEYS='<key>:<agent-id>,...'\n"
            "or, for local experiments only, ALLOW_ANONYMOUS=true."
        )

    mcp.settings.host = settings.host
    mcp.settings.port = settings.port

    # Stateless HTTP: each request spawns its server task from the REQUEST's
    # async context. That is what lets AuthMiddleware's per-request identity
    # contextvar propagate into the tool — in stateful mode the tool runs in a
    # long-lived session task that captured identity once at session creation,
    # so a per-request Bearer key would be ignored (the identity bug we fixed).
    mcp.settings.stateless_http = True
    mcp.settings.json_response = True

    if api_keys:
        # Wrap the MCP ASGI app so unauthenticated requests die at the door.
        import uvicorn

        app = AuthMiddleware(mcp.streamable_http_app(), api_keys, admin_keys)
        uvicorn.run(app, host=settings.host, port=settings.port)
    else:
        mcp.run(transport="streamable-http")  # anonymous, explicitly allowed


if __name__ == "__main__":
    main()
