"""Yamaha AV receiver via the MusicCast / Yamaha Extended Control (YXC) HTTP API.

No auth; plain GET requests. Every response carries ``response_code`` (0 = ok).
"""

from __future__ import annotations

from typing import Any, Literal

import httpx
from mcp.server.mcpserver import MCPServer

from home_mcp import config

Zone = Literal["main", "zone2"]

_ERRORS = {
    1: "initializing",
    2: "internal error",
    3: "invalid request",
    4: "invalid parameter",
    5: "guarded (not available in current state; is the zone powered on?)",
    6: "timeout",
}


async def _get(path: str, **params: Any) -> dict:
    host = config.get("yamaha_host")
    async with httpx.AsyncClient(timeout=6) as client:
        resp = await client.get(f"http://{host}/YamahaExtendedControl/v1/{path}", params=params)
    resp.raise_for_status()
    data = resp.json()
    code = data.get("response_code", 0)
    if code != 0:
        raise RuntimeError(f"yamaha {path}: {_ERRORS.get(code, f'error {code}')}")
    return data


def register(mcp: MCPServer) -> None:
    @mcp.tool()
    async def yamaha_status(zone: Zone = "main") -> dict:
        """Receiver status: power, input, volume (raw + dB), mute, sound program, now playing."""
        status = await _get(f"{zone}/getStatus")
        status.pop("response_code", None)
        if status.get("input") in {"spotify", "airplay", "server", "net_radio", "bluetooth"}:
            play = await _get("netusb/getPlayInfo")
            status["now_playing"] = {
                k: play.get(k) for k in ("playback", "artist", "album", "track", "input")
            }
        return status

    @mcp.tool()
    async def yamaha_capabilities() -> dict:
        """Inputs, sound programs and volume range per zone (use before setting input/program)."""
        features = await _get("system/getFeatures")
        names = await _get("system/getNameText")
        zones = {}
        for z in features.get("zone", []):
            zones[z["id"]] = {
                "inputs": z.get("input_list"),
                "sound_programs": z.get("sound_program_list"),
                "ranges": {r["id"]: r for r in z.get("range_step", [])},
            }
        input_names = {i["id"]: i.get("text") for i in names.get("input_list", [])}
        return {"zones": zones, "input_names": input_names}

    @mcp.tool()
    async def yamaha_power(on: bool, zone: Zone = "main") -> str:
        """Power a receiver zone on or to standby."""
        await _get(f"{zone}/setPower", power="on" if on else "standby")
        return f"{zone} {'on' if on else 'standby'}"

    @mcp.tool()
    async def yamaha_volume(
        db: float | None = None, step: int | None = None, zone: Zone = "main"
    ) -> dict:
        """Set volume. db: absolute level in dB (e.g. -35.0). step: relative raw steps
        (+/-; one step = 0.5 dB). Exactly one of db/step."""
        if (db is None) == (step is None):
            raise ValueError("pass exactly one of db or step")
        if db is not None:
            if db > -5:
                raise ValueError("refusing volume above -5 dB; set it explicitly at the receiver")
            await _get(f"{zone}/setActualVolume", mode="db", value=f"{db:.1f}")
        elif step is not None:
            await _get(f"{zone}/setVolume", volume="up" if step > 0 else "down", step=abs(step))
        status = await _get(f"{zone}/getStatus")
        return {"volume": status.get("volume"), "actual_volume": status.get("actual_volume")}

    @mcp.tool()
    async def yamaha_mute(mute: bool, zone: Zone = "main") -> str:
        """Mute or unmute a zone."""
        await _get(f"{zone}/setMute", enable=str(mute).lower())
        return f"{zone} mute={mute}"

    @mcp.tool()
    async def yamaha_input(input: str, zone: Zone = "main") -> str:
        """Switch input (ids like hdmi1, av1, spotify, airplay, bluetooth, tuner)."""
        await _get(f"{zone}/setInput", input=input)
        return f"{zone} input -> {input}"

    @mcp.tool()
    async def yamaha_sound_program(program: str, zone: Zone = "main") -> str:
        """Set DSP sound program (e.g. straight, 2ch_stereo, 7ch_stereo, surr_decoder, movie)."""
        await _get(f"{zone}/setSoundProgram", program=program)
        return f"{zone} program -> {program}"

    @mcp.tool()
    async def yamaha_pure_direct(enable: bool) -> str:
        """Toggle Pure Direct on the main zone (bypasses DSP/tone; disables video OSD)."""
        await _get("main/setPureDirect", enable=str(enable).lower())
        return f"pure_direct={enable}"

    @mcp.tool()
    async def yamaha_playback(action: Literal["play", "pause", "stop", "next", "previous"]) -> str:
        """Transport control for network sources (Spotify, AirPlay, server, net radio)."""
        await _get("netusb/setPlayback", playback=action)
        return action

    @mcp.tool()
    async def yamaha_presets() -> list[dict]:
        """List the receiver's saved network presets."""
        info = await _get("netusb/getPresetInfo")
        return [
            {"num": i + 1, **p}
            for i, p in enumerate(info.get("preset_info", []))
            if p.get("input") != "unknown"
        ]

    @mcp.tool()
    async def yamaha_recall_preset(num: int, zone: Zone = "main") -> str:
        """Recall a saved network preset by number."""
        await _get("netusb/recallPreset", zone=zone, num=num)
        return f"recalled preset {num} on {zone}"
