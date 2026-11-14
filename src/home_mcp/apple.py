"""Apple TV / HomePod via pyatv, and Apple Home via the macOS `shortcuts` CLI.

pyatv: Apple TVs need Companion (+ AirPlay) pairing for remote/app control —
``apple_pair_begin`` shows a PIN on the TV, ``apple_pair_finish`` completes it.
HomePods generally work for now-playing/volume without pairing.

Apple Home has no public API on macOS outside Catalyst apps, so Home scenes and
accessories are driven through Shortcuts: build a shortcut per action in the
Shortcuts app (e.g. "Home: Movie Time") and run it with ``shortcut_run``.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from typing import Any

import pyatv
from mcp.server.mcpserver import MCPServer
from pyatv.const import Protocol

from home_mcp import config

_pairing: dict[str, Any] = {}

REMOTE_COMMANDS = {
    "play",
    "pause",
    "play_pause",
    "stop",
    "next",
    "previous",
    "up",
    "down",
    "left",
    "right",
    "select",
    "menu",
    "home",
    "volume_up",
    "volume_down",
    "skip_forward",
    "skip_backward",
}


async def _scan(timeout: int = 4):
    """Multicast scan + unicast scan of every Apple host seen before, merged by identifier.

    On Linux the netaudio engine shares UDP 5353 (SO_REUSEPORT), so unicast mDNS replies
    to a multicast query can land on its socket instead of pyatv's. Unicast queries to a
    known host reply to an ephemeral port and are unaffected, so known hosts always answer.
    """
    loop = asyncio.get_running_loop()
    known = list(config.get("apple_hosts") or [])
    multicast, unicast = await asyncio.gather(
        pyatv.scan(loop, timeout=timeout),
        pyatv.scan(loop, timeout=timeout, hosts=known) if known else asyncio.sleep(0, []),
    )
    merged = {c.identifier: c for c in [*multicast, *unicast]}
    seen = sorted({str(c.address) for c in merged.values()} | set(known))
    if seen != sorted(known):
        config.update(apple_hosts=seen)
    return list(merged.values())


async def _conf(device: str):
    confs = await _scan()
    needle = device.lower()
    exact = [c for c in confs if needle in {c.name.lower(), str(c.address), c.identifier}]
    partial = [c for c in confs if needle in c.name.lower()]
    chosen = exact or partial
    if len(chosen) != 1:
        raise ValueError(f"device '{device}' not found/ambiguous. Known: {[c.name for c in confs]}")
    conf = chosen[0]
    for proto_name, creds in (config.get("atv_credentials").get(conf.identifier) or {}).items():
        conf.set_credentials(Protocol[proto_name], creds)
    return conf


async def _connect(device: str):
    conf = await _conf(device)
    return await pyatv.connect(conf, asyncio.get_running_loop())


def register(mcp: MCPServer) -> None:
    @mcp.tool()
    async def apple_devices() -> list[dict]:
        """Scan for Apple TVs, HomePods and AirPlay targets on the LAN."""
        stored = config.get("atv_credentials")
        return [
            {
                "name": c.name,
                "address": str(c.address),
                "identifier": c.identifier,
                "model": str(c.device_info.model_str),
                "os": str(c.device_info.operating_system),
                "protocols": sorted(s.protocol.name for s in c.services),
                "paired": sorted((stored.get(c.identifier) or {}).keys()),
            }
            for c in await _scan()
        ]

    @mcp.tool()
    async def apple_pair_begin(device: str, protocol: str = "Companion") -> str:
        """Start pairing (Companion for remote/apps, AirPlay for audio/now-playing).
        A PIN appears on the TV; then call apple_pair_finish with it."""
        conf = await _conf(device)
        handler = await pyatv.pair(conf, Protocol[protocol], asyncio.get_running_loop())
        await handler.begin()
        if _pairing.get("handler"):
            await _pairing["handler"].close()
        _pairing.update(handler=handler, identifier=conf.identifier, protocol=protocol)
        if handler.device_provides_pin:
            return f"PIN shown on {conf.name}; call apple_pair_finish(pin)"
        return (
            "device expects a PIN from us: call apple_pair_finish with pin 1111 and enter it there"
        )

    @mcp.tool()
    async def apple_pair_finish(pin: str) -> str:
        """Finish pairing with the PIN displayed on the device."""
        handler = _pairing.get("handler")
        if not handler:
            raise RuntimeError("no pairing in progress; call apple_pair_begin first")
        try:
            handler.pin(int(pin))
            await handler.finish()
            if not handler.has_paired:
                raise RuntimeError("pairing failed (wrong PIN?)")
            creds = dict(config.get("atv_credentials"))
            entry = dict(creds.get(_pairing["identifier"]) or {})
            entry[_pairing["protocol"]] = handler.service.credentials
            creds[_pairing["identifier"]] = entry
            config.update(atv_credentials=creds)
        finally:
            await handler.close()
            _pairing.clear()
        return "paired"

    @mcp.tool()
    async def apple_now_playing(device: str) -> dict:
        """What's playing on an Apple TV/HomePod, plus power and volume when available."""
        atv = await _connect(device)
        try:
            playing = await atv.metadata.playing()
            out: dict[str, Any] = {
                "state": playing.device_state.name,
                "title": playing.title,
                "artist": playing.artist,
                "album": playing.album,
                "media_type": playing.media_type.name,
                "position": playing.position,
                "total_time": playing.total_time,
            }
            try:
                out["app"] = atv.metadata.app.name if atv.metadata.app else None
            except Exception:  # noqa: BLE001 - feature not available on this device/protocol
                pass
            try:
                out["volume"] = atv.audio.volume
            except Exception:  # noqa: BLE001
                pass
            try:
                out["power"] = atv.power.power_state.name
            except Exception:  # noqa: BLE001
                pass
            return out
        finally:
            atv.close()

    @mcp.tool()
    async def apple_remote(device: str, command: str) -> str:
        """Send a remote command: play, pause, play_pause, stop, next, previous, up, down, left,
        right, select, menu, home, volume_up, volume_down, skip_forward, skip_backward,
        turn_on, turn_off."""
        atv = await _connect(device)
        try:
            if command in {"turn_on", "turn_off"}:
                await getattr(atv.power, command)()
            elif command in {"volume_up", "volume_down"}:
                await getattr(atv.audio, command)()
            elif command in REMOTE_COMMANDS:
                await getattr(atv.remote_control, command)()
            else:
                raise ValueError(f"unknown command {command}")
            return f"{command} -> {device}"
        finally:
            atv.close()

    @mcp.tool()
    async def apple_volume(device: str, level: float) -> str:
        """Set volume 0-100 on an Apple TV/HomePod."""
        atv = await _connect(device)
        try:
            await atv.audio.set_volume(max(0.0, min(100.0, level)))
            return f"volume {level} on {device}"
        finally:
            atv.close()

    @mcp.tool()
    async def apple_apps(device: str) -> list[dict]:
        """List installed apps on an Apple TV (needs Companion pairing)."""
        atv = await _connect(device)
        try:
            return [{"name": a.name, "id": a.identifier} for a in await atv.apps.app_list()]
        finally:
            atv.close()

    @mcp.tool()
    async def apple_launch_app(device: str, app: str) -> str:
        """Launch an app on an Apple TV by bundle id or name (needs Companion pairing)."""
        atv = await _connect(device)
        try:
            apps = await atv.apps.app_list()
            match = [
                a for a in apps if app in {a.identifier, a.name} or app.lower() == a.name.lower()
            ]
            bundle = match[0].identifier if match else app
            await atv.apps.launch_app(bundle)
            return f"launched {bundle}"
        finally:
            atv.close()

    # ── Apple Home via Shortcuts (only when the server runs on macOS) ────

    if sys.platform != "darwin":
        return

    async def _shortcuts(*args: str, stdin: bytes | None = None) -> str:
        exe = shutil.which("shortcuts") or "/usr/bin/shortcuts"
        proc = await asyncio.create_subprocess_exec(
            exe,
            *args,
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout=60)
        if proc.returncode != 0:
            raise RuntimeError(err.decode().strip() or f"shortcuts exited {proc.returncode}")
        return out.decode().strip()

    @mcp.tool()
    async def shortcut_list() -> list[str]:
        """List macOS Shortcuts (the bridge to Apple Home scenes/accessories)."""
        return [line for line in (await _shortcuts("list")).splitlines() if line]

    @mcp.tool()
    async def shortcut_run(name: str, input_text: str | None = None) -> str:
        """Run a macOS Shortcut by exact name (e.g. a Home scene shortcut); returns its output."""
        if input_text is None:
            return await _shortcuts("run", name) or "ok"
        return await _shortcuts("run", name, "--input-path", "-", stdin=input_text.encode()) or "ok"
