"""LAN-wide mDNS sweep so the agent can see what's out there (and what's new)."""

from __future__ import annotations

import asyncio

from mcp.server.mcpserver import MCPServer
from zeroconf import ServiceStateChange
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

SERVICE_TYPES = {
    "_netaudio-arc._udp.local.": "dante",
    "_hue._tcp.local.": "hue",
    "_hap._tcp.local.": "homekit",
    "_matter._tcp.local.": "matter",
    "_airplay._tcp.local.": "airplay",
    "_companion-link._tcp.local.": "apple-companion",
    "_spotify-connect._tcp.local.": "spotify-connect",
    "_googlecast._tcp.local.": "chromecast",
    "_sonos._tcp.local.": "sonos",
    "_ndi._tcp.local.": "ndi",
    "_nvstream._tcp.local.": "sunshine",
    "_http._tcp.local.": "http",
    "_ipp._tcp.local.": "printer",
    "_esphomelib._tcp.local.": "esphome",
    "_home-assistant._tcp.local.": "home-assistant",
    "_elg._tcp.local.": "elgato",
    "_shelly._tcp.local.": "shelly",
}


def register(mcp: MCPServer) -> None:
    @mcp.tool()
    async def network_discover(seconds: float = 4.0) -> dict[str, list[dict]]:
        """Sweep the LAN via mDNS for controllable devices, grouped by kind (dante, hue...)."""
        found: dict[str, set[str]] = {}

        def on_change(zeroconf, service_type, name, state_change):
            if state_change is ServiceStateChange.Added:
                found.setdefault(service_type, set()).add(name)

        azc = AsyncZeroconf()
        browser = AsyncServiceBrowser(azc.zeroconf, list(SERVICE_TYPES), handlers=[on_change])
        try:
            await asyncio.sleep(seconds)
            result: dict[str, list[dict]] = {}
            for service_type, names in found.items():
                kind = SERVICE_TYPES[service_type]
                for name in sorted(names):
                    info = AsyncServiceInfo(service_type, name)
                    await info.async_request(azc.zeroconf, 1500)
                    result.setdefault(kind, []).append(
                        {
                            "name": name.removesuffix("." + service_type),
                            "host": info.server,
                            "addresses": info.parsed_addresses(),
                            "port": info.port,
                        }
                    )
            return result
        finally:
            await browser.async_cancel()
            await azc.async_close()
