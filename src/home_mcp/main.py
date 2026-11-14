"""home-mcp: one MCP server for everything controllable on the home LAN.

home-mcp                      stdio transport (spawned by a local MCP client)
home-mcp serve [--port 8765]  streamable HTTP at http://HOST:PORT/mcp (long-running service)
"""

from __future__ import annotations

import argparse
import functools
import hmac
import logging
import os
import sys
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError

from home_mcp import apple, dante, discovery, hue, unifi, yamaha

INSTRUCTIONS = """\
Controls devices on the home network:
- dante_*: Dante audio network routing (instruments, interfaces, AVIO adapters). Routes are
  receiver subscriptions: rx channel <- tx channel. Use dante_devices / dante_channels first,
  and dante_snapshot_save before large routing changes so they can be undone.
- yamaha_*: AV receiver (power, input, volume in dB, DSP program, network playback).
- hue_*: Philips Hue lights, rooms and scenes (hue_pair once, after pressing the bridge button).
- apple_*: Apple TV / HomePods via pyatv. shortcut_* (macOS host only): Apple Home via Shortcuts.
- unifi_* / protect_*: UniFi network clients and Protect camera snapshots (needs API key).
- network_discover: mDNS sweep of the LAN.
"""


def _surface_errors(fn):
    """mcp 2.x hides the text of any exception that isn't a ToolError. Device errors
    ("not paired, press the bridge button", "device not found, known: ...") are exactly
    what the model needs to see, so re-raise everything as ToolError."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except (ToolError, MCPError):
            raise
        except Exception as exc:
            raise ToolError(f"{type(exc).__name__}: {exc}") from exc

    return wrapper


class HomeServer(MCPServer):
    def add_tool(self, fn, *args, **kwargs) -> None:
        super().add_tool(_surface_errors(fn), *args, **kwargs)


def build() -> MCPServer:
    mcp = HomeServer(name="home-mcp", instructions=INSTRUCTIONS)
    for module in (dante, yamaha, hue, apple, unifi, discovery):
        module.register(mcp)
    return mcp


class BearerAuth:
    """Optional static bearer token (HOME_MCP_TOKEN_FILE). Off when no token is configured."""

    def __init__(self, app, token: str) -> None:
        self.app, self.expected = app, f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            presented = dict(scope["headers"]).get(b"authorization", b"")
            if not hmac.compare_digest(presented, self.expected):
                await send({"type": "http.response.start", "status": 401, "headers": []})
                await send({"type": "http.response.body", "body": b"unauthorized"})
                return
        await self.app(scope, receive, send)


def serve(host: str, port: int, allowed_hosts: list[str]) -> None:
    import uvicorn

    hosts = ["localhost:*", "127.0.0.1:*", *(h if ":" in h else f"{h}:*" for h in allowed_hosts)]
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=[f"http://{h}" for h in hosts] + ["https://claude.ai"],
    )
    app = build().streamable_http_app(transport_security=security, host=host)
    token_file = os.environ.get("HOME_MCP_TOKEN_FILE")
    if token_file:
        app = BearerAuth(app, Path(token_file).read_text().strip())
    uvicorn.run(app, host=host, port=port, log_level="warning")


def main() -> None:
    # stdout is the stdio MCP transport; keep every log on stderr.
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    parser = argparse.ArgumentParser(prog="home-mcp")
    sub = parser.add_subparsers(dest="cmd")
    s = sub.add_parser("serve", help="run the streamable HTTP transport")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument(
        "--allowed-host",
        action="append",
        default=[],
        help="Host header values clients use (e.g. 192.168.1.100, ez). Repeatable.",
    )
    args = parser.parse_args()
    if args.cmd == "serve":
        serve(args.host, args.port, args.allowed_host)
    else:
        build().run("stdio")


if __name__ == "__main__":
    main()
