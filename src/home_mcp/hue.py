"""Philips Hue bridge via the local CLIP v1 REST API.

Pairing: press the bridge's link button, then call ``hue_pair`` within 30 s.
The issued username is stored in config.json.
"""

from __future__ import annotations

import socket
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer

from home_mcp import config


def _base() -> str:
    user = config.get("hue_username")
    if not user:
        raise RuntimeError("Hue not paired: press the bridge link button, then call hue_pair")
    return f"http://{config.get('hue_host')}/api/{user}"


async def _req(method: str, path: str, body: Any = None) -> Any:
    async with httpx.AsyncClient(timeout=6) as client:
        resp = await client.request(method, f"{_base()}/{path}", json=body)
    resp.raise_for_status()
    data = resp.json()
    errors = (
        [e["error"]["description"] for e in data if "error" in e] if isinstance(data, list) else []
    )
    if errors:
        raise RuntimeError("; ".join(errors))
    return data


def _hex_to_xy(hex_color: str) -> list[float]:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) / 255 for i in (0, 2, 4))

    def lin(c: float) -> float:
        return ((c + 0.055) / 1.055) ** 2.4 if c > 0.04045 else c / 12.92

    r, g, b = lin(r), lin(g), lin(b)
    x = r * 0.4124 + g * 0.3576 + b * 0.1805
    y = r * 0.2126 + g * 0.7152 + b * 0.0722
    z = r * 0.0193 + g * 0.1192 + b * 0.9505
    total = x + y + z or 1
    return [round(x / total, 4), round(y / total, 4)]


def _state(on: bool | None, brightness: int | None, kelvin: int | None, color: str | None) -> dict:
    state: dict[str, Any] = {}
    if on is not None:
        state["on"] = on
    if brightness is not None:
        state["bri"] = max(1, min(254, round(brightness * 254 / 100)))
        state.setdefault("on", True)
        # House style: brightness changes land on warm tungsten unless a color is asked for.
        if kelvin is None and color is None:
            kelvin = int(config.get("hue_default_kelvin"))
    if kelvin is not None:
        state["ct"] = max(153, min(500, round(1_000_000 / kelvin)))
        state.setdefault("on", True)
    if color is not None:
        state["xy"] = _hex_to_xy(color)
        state.setdefault("on", True)
    if not state:
        raise ValueError("nothing to change")
    return state


async def _lookup(kind: str, ident: str) -> tuple[str, dict]:
    items = await _req("GET", kind)
    if ident in items:
        return ident, items[ident]
    for key, item in items.items():
        if item.get("name", "").lower() == ident.lower():
            return key, item
    matches = [(k, i) for k, i in items.items() if ident.lower() in i.get("name", "").lower()]
    if len(matches) == 1:
        return matches[0]
    names = sorted(i.get("name", "") for i in items.values())
    raise ValueError(f"{kind[:-1]} '{ident}' not found/ambiguous. Known: {names}")


def register(mcp: MCPServer) -> None:
    @mcp.tool()
    async def hue_pair() -> str:
        """Pair with the Hue bridge. Press the bridge link button first (30 s window)."""
        host = config.get("hue_host")
        async with httpx.AsyncClient(timeout=6) as client:
            resp = await client.post(
                f"http://{host}/api", json={"devicetype": f"home-mcp#{socket.gethostname()[:19]}"}
            )
        result = resp.json()[0]
        if "error" in result:
            raise RuntimeError(result["error"]["description"])
        config.update(hue_username=result["success"]["username"])
        return "paired with Hue bridge"

    @mcp.tool()
    async def hue_lights() -> list[dict]:
        """List Hue lights with on/brightness/reachable state."""
        lights = await _req("GET", "lights")
        return [
            {
                "id": k,
                "name": v["name"],
                "on": v["state"].get("on"),
                "brightness_pct": round(v["state"].get("bri", 0) * 100 / 254),
                "reachable": v["state"].get("reachable"),
                "type": v.get("type"),
            }
            for k, v in lights.items()
        ]

    @mcp.tool()
    async def hue_rooms() -> list[dict]:
        """List Hue rooms and zones with their light ids and any-on/all-on state."""
        groups = await _req("GET", "groups")
        return [
            {
                "id": k,
                "name": v["name"],
                "type": v.get("type"),
                "lights": v.get("lights"),
                "any_on": v.get("state", {}).get("any_on"),
                "all_on": v.get("state", {}).get("all_on"),
            }
            for k, v in groups.items()
            if v.get("type") in {"Room", "Zone", "Entertainment"}
        ]

    @mcp.tool()
    async def hue_scenes(room: str | None = None) -> list[dict]:
        """List Hue scenes, optionally filtered to one room/zone."""
        scenes = await _req("GET", "scenes")
        group_id = (await _lookup("groups", room))[0] if room else None
        return [
            {"id": k, "name": v["name"], "room": v.get("group")}
            for k, v in scenes.items()
            if group_id is None or v.get("group") == group_id
        ]

    @mcp.tool()
    async def hue_set_light(
        light: str,
        on: bool | None = None,
        brightness: int | None = None,
        kelvin: int | None = None,
        color: str | None = None,
        transition_s: float | None = None,
    ) -> dict:
        """Set one light. light: id or name. brightness 0-100 (defaults to warm 2700K unless kelvin
        or color is given), kelvin 2000-6500, color "#RRGGBB"."""
        light_id, _ = await _lookup("lights", light)
        state = _state(on, brightness, kelvin, color)
        if transition_s is not None:
            state["transitiontime"] = round(transition_s * 10)
        await _req("PUT", f"lights/{light_id}/state", state)
        return {"light": light_id, **state}

    @mcp.tool()
    async def hue_set_room(
        room: str,
        on: bool | None = None,
        brightness: int | None = None,
        kelvin: int | None = None,
        color: str | None = None,
        transition_s: float | None = None,
    ) -> dict:
        """Set every light in a room/zone. room: id or name ("all" = every light).
        Brightness changes default to warm 2700K unless kelvin or color is given."""
        group_id = "0" if room.lower() == "all" else (await _lookup("groups", room))[0]
        state = _state(on, brightness, kelvin, color)
        if transition_s is not None:
            state["transitiontime"] = round(transition_s * 10)
        await _req("PUT", f"groups/{group_id}/action", state)
        return {"room": group_id, **state}

    @mcp.tool()
    async def hue_scene(scene: str, room: str | None = None) -> str:
        """Activate a Hue scene by id or name (pass room to disambiguate same-named scenes)."""
        scenes = await _req("GET", "scenes")
        group_id = (await _lookup("groups", room))[0] if room else None
        match = [
            (k, v)
            for k, v in scenes.items()
            if (k == scene or v["name"].lower() == scene.lower())
            and (group_id is None or v.get("group") == group_id)
        ]
        if not match:
            raise ValueError(f"scene '{scene}' not found")
        scene_id, data = match[0]
        await _req("PUT", f"groups/{data.get('group') or '0'}/action", {"scene": scene_id})
        return f"activated {data['name']}"
