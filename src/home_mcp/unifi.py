"""UniFi OS console: Network clients + Protect cameras via the official integration APIs.

Needs an API key (UniFi OS → Settings → Control Plane → Integrations → Create API key),
stored with ``unifi_set_api_key``. The console uses a self-signed cert.
"""

from __future__ import annotations

from typing import Any

import httpx
from mcp.server.mcpserver import Image, MCPServer

from home_mcp import config


async def _get(path: str, raw: bool = False, **params: Any) -> Any:
    key = config.get("unifi_api_key")
    if not key:
        raise RuntimeError("no UniFi API key: create one in UniFi OS, then call unifi_set_api_key")
    async with httpx.AsyncClient(timeout=15, verify=False) as client:
        resp = await client.get(
            f"https://{config.get('unifi_host')}/proxy/{path}",
            headers={"X-API-KEY": key, "Accept": "application/json"},
            params=params,
        )
    resp.raise_for_status()
    return resp.content if raw else resp.json()


def register(mcp: MCPServer) -> None:
    @mcp.tool()
    async def unifi_set_api_key(api_key: str) -> str:
        """Store a UniFi OS integration API key (Settings → Control Plane → Integrations)."""
        config.update(unifi_api_key=api_key)
        return "stored"

    @mcp.tool()
    async def unifi_clients() -> list[dict]:
        """List clients connected to the UniFi network (name, IP, MAC, wired/wireless)."""
        sites = (await _get("network/integration/v1/sites"))["data"]
        out = []
        for site in sites:
            offset = 0
            while True:
                page = await _get(
                    f"network/integration/v1/sites/{site['id']}/clients", offset=offset, limit=200
                )
                for c in page.get("data", []):
                    out.append(
                        {
                            "name": c.get("name"),
                            "ip": c.get("ipAddress"),
                            "mac": c.get("macAddress"),
                            "type": c.get("type"),
                            "connected_at": c.get("connectedAt"),
                        }
                    )
                offset += page.get("count", 0)
                if not page.get("count") or offset >= page.get("totalCount", 0):
                    break
        return out

    @mcp.tool()
    async def unifi_devices() -> list[dict]:
        """List UniFi network gear (gateway, switches, APs) with state."""
        sites = (await _get("network/integration/v1/sites"))["data"]
        out = []
        for site in sites:
            page = await _get(f"network/integration/v1/sites/{site['id']}/devices", limit=200)
            out += [
                {k: d.get(k) for k in ("name", "model", "ipAddress", "state", "macAddress")}
                for d in page.get("data", [])
            ]
        return out

    @mcp.tool()
    async def protect_cameras() -> list[dict]:
        """List UniFi Protect cameras."""
        cams = await _get("protect/integration/v1/cameras")
        return [
            {k: c.get(k) for k in ("id", "name", "state", "modelKey", "isMicEnabled")} for c in cams
        ]

    @mcp.tool()
    async def protect_snapshot(camera: str) -> Image:
        """Grab a current JPEG snapshot from a Protect camera (id or name)."""
        cams = await _get("protect/integration/v1/cameras")
        match = [c for c in cams if camera in {c["id"]} or camera.lower() in c["name"].lower()]
        if not match:
            raise ValueError(f"camera '{camera}' not found: {[c['name'] for c in cams]}")
        data = await _get(f"protect/integration/v1/cameras/{match[0]['id']}/snapshot", raw=True)
        return Image(data=data, format="jpeg")
