"""HDFury Vertex2 (HDMI path to the projector) + projector power over its CEC engine.

The projector (Epson HC 3800) has no LAN, so power goes over HDMI-CEC from the Vertex2:
targeted messages from the Vertex2 (logical address 0xE) to the display (0x0):
  e0:04  Image View On  -> power on
  e0:36  Standby        -> power off
The Vertex2's built-in `set ceccmd poweroff` is a *broadcast* standby that also turns the
AVR off, so it is deliberately not used. Projector setting HDMI Link -> Power Off Link must
be Off, or the projector itself tells the AVR to follow it into standby.

The Epson ignores standby while warming up, so power-off re-sends for ~90 s in the
background (cancelled by any later projector_power call). CEC gives no power-state readback
through the Vertex2, so status reports what the Vertex2 is outputting instead.

Control: telnet-style TCP (default 2220, one command per line), status: /ssi/infopage.ssi.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer

from home_mcp import config

POWER_ON = "e0:04"
STANDBY = "e0:36"
OFF_RETRY_SCHEDULE_S = (15, 15, 15, 15, 15, 15)  # re-send standby through Epson warm-up

_off_retry: asyncio.Task | None = None


async def _command(cmd: str) -> str:
    host, port = config.get("vertex2_host"), int(config.get("vertex2_port"))
    reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=4)
    try:
        writer.write(f"{cmd}\r\n".encode())
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=4)
        return line.decode(errors="replace").strip()
    finally:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()


async def _info() -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=5) as client:
        resp = await client.get(f"http://{config.get('vertex2_host')}/ssi/infopage.ssi")
    resp.raise_for_status()
    return {k: v.strip() if isinstance(v, str) else v for k, v in resp.json().items()}


async def _retry_standby() -> None:
    for delay in OFF_RETRY_SCHEDULE_S:
        await asyncio.sleep(delay)
        with contextlib.suppress(OSError, TimeoutError):
            await _command(f"set ceccmd {STANDBY}")


async def set_projector_power(on: bool) -> str:
    """Power the projector; also used by scenes. Cancels any pending off-retry first."""
    global _off_retry
    if _off_retry and not _off_retry.done():
        _off_retry.cancel()
    reply = await _command(f"set ceccmd {POWER_ON if on else STANDBY}")
    if not on:
        _off_retry = asyncio.create_task(_retry_standby())
    return f"projector {'on' if on else 'off'} sent (vertex2: {reply or 'no reply'})"


def register(mcp: MCPServer) -> None:
    @mcp.tool()
    async def projector_power(on: bool) -> str:
        """Turn the projector on or off (HDMI-CEC via the Vertex2; the AVR is not affected).

        Warm-up takes ~1 min and the projector ignores off during it; off keeps re-sending
        for ~90 s so "off" right after "on" still lands. Use yamaha_* for the AVR."""
        return await set_projector_power(on)

    @mcp.tool()
    async def vertex2_status() -> dict:
        """HDMI signal path: input signal from the AVR, output to the projector (empty when
        the projector isn't taking video, e.g. off), projector capabilities, audio format."""
        info = await _info()
        return {
            "input_signal": info.get("RX0") or None,
            "output_to_projector": info.get("TX0") or None,
            "projector_capabilities": info.get("SINK0") or None,
            "audio_out": info.get("AUDOUT") or None,
            "audio_sink": info.get("SPDASC") or None,
            "selected_inputs": {"tx0": info.get("portseltx0"), "tx1": info.get("portseltx1")},
        }
