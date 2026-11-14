"""Dante network audio via netaudio's native engine.

netaudio's Python client talks to a local daemon over HTTP; if no system daemon
is running it spawns a private engine for this process (``engine.start``). The
first call after startup waits for mDNS discovery to settle.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from mcp.server.mcpserver import MCPServer
from netaudio.daemon.client import call_daemon, get_devices_from_daemon
from netaudio.device_record import Device

from home_mcp import config

DISCOVERY_SETTLE_S = 8.0


async def _devices(settle: bool = True) -> dict[str, Device]:
    """server_name -> Device. Polls until the device count stops changing."""
    devices = await get_devices_from_daemon() or {}
    if not settle:
        return devices
    deadline = time.monotonic() + DISCOVERY_SETTLE_S
    stable_since = time.monotonic()
    while time.monotonic() < deadline:
        await asyncio.sleep(0.75)
        latest = await get_devices_from_daemon() or {}
        if len(latest) != len(devices) or not latest:
            devices, stable_since = latest, time.monotonic()
        elif time.monotonic() - stable_since > 2.0:
            return latest
        else:
            devices = latest
    return devices


def _find(devices: dict[str, Device], ident: str) -> tuple[str, Device]:
    needle = ident.strip().lower()
    for server_name, device in devices.items():
        candidates = {server_name.lower(), device.name.lower(), str(device.ipv4 or "")}
        if needle in candidates:
            return server_name, device
    partial = [
        (s, d) for s, d in devices.items() if needle in d.name.lower() or needle in s.lower()
    ]
    if len(partial) == 1:
        return partial[0]
    names = sorted(d.name or s for s, d in devices.items())
    if partial:
        raise ValueError(f"'{ident}' is ambiguous: {[d.name for _, d in partial]}")
    raise ValueError(f"Dante device '{ident}' not found. Known: {names}")


def _channel_label(channel) -> str:
    return channel.friendly_name or channel.name or str(channel.number)


def _resolve_channel(device: Device, direction: str, ident: str | int):
    channels = device.tx_channels if direction == "tx" else device.rx_channels
    text = str(ident).strip()
    if text.isdigit() and int(text) in channels:
        return channels[int(text)]
    for channel in channels.values():
        if text.lower() in {
            str(channel.name or "").lower(),
            str(channel.friendly_name or "").lower(),
        }:
            return channel
    labels = [
        f"{c.number}:{_channel_label(c)}" for c in sorted(channels.values(), key=lambda c: c.number)
    ]
    raise ValueError(f"{direction} channel '{ident}' not on {device.name}. Channels: {labels}")


def _summary(server_name: str, device: Device) -> dict[str, Any]:
    r = device.record
    return {
        "name": device.name,
        "server_name": server_name,
        "ip": str(device.ipv4) if device.ipv4 else None,
        "online": device.online,
        "model": r.get("model") or r.get("model_name") or r.get("product_name"),
        "manufacturer": r.get("manufacturer"),
        "sample_rate_hz": r.get("sample_rate_hz"),
        "latency_ms": r.get("latency_ms") or r.get("configured_latency_ms"),
        "tx_channels": len(device.tx_channels),
        "rx_channels": len(device.rx_channels),
    }


def _subscriptions(devices: dict[str, Device], only: str | None, include_unused: bool):
    rows = []
    for server_name, device in sorted(devices.items(), key=lambda kv: kv[1].name):
        if only and server_name != only:
            continue
        for sub in device.record.get("subscriptions") or []:
            if not include_unused and not sub.get("tx_device"):
                continue
            status = sub.get("status") or {}
            rows.append(
                {
                    "rx": f"{sub.get('rx_channel')}@{sub.get('rx_device') or device.name}",
                    "rx_channel_number": sub.get("rx_channel_number"),
                    "tx": f"{sub.get('tx_channel')}@{sub.get('tx_device')}"
                    if sub.get("tx_device")
                    else None,
                    "status": status.get("label") or status.get("status"),
                    "severity": status.get("severity"),
                }
            )
    return rows


async def _apply(routes: list[dict]) -> list[dict]:
    result = await call_daemon("POST", "/subscriptions/apply", {"routes": routes})
    outcomes = result.get("routes") if isinstance(result, dict) else None
    return [
        {**route, "ok": (o or {}).get("ok"), "error": (o or {}).get("error")}
        for route, o in zip(routes, outcomes or [{}] * len(routes), strict=False)
    ]


def register(mcp: MCPServer) -> None:
    @mcp.tool()
    async def dante_devices() -> list[dict]:
        """List Dante devices on the network (name, IP, model, sample rate, channel counts)."""
        devices = await _devices()
        return [_summary(s, d) for s, d in sorted(devices.items(), key=lambda kv: kv[1].name)]

    @mcp.tool()
    async def dante_channels(device: str) -> dict:
        """List a Dante device's transmit (tx) and receive (rx) channels by number and name.

        device: Dante name, mDNS server name, or IP (partial names ok if unique).
        """
        devices = await _devices()
        server_name, d = _find(devices, device)

        def rows(channels):
            return [
                {"number": c.number, "name": _channel_label(c)}
                for c in sorted(channels.values(), key=lambda c: c.number)
            ]

        return {
            **_summary(server_name, d),
            "tx": rows(d.tx_channels),
            "rx": rows(d.rx_channels),
        }

    @mcp.tool()
    async def dante_routes(device: str | None = None, include_unused: bool = False) -> list[dict]:
        """Show the Dante routing matrix (receiver subscriptions) as rx <- tx pairs.

        device: limit to one receiving device. include_unused: also list unsubscribed rx channels.
        """
        devices = await _devices()
        only = _find(devices, device)[0] if device else None
        return _subscriptions(devices, only, include_unused)

    @mcp.tool()
    async def dante_route(
        tx_device: str, tx_channel: str, rx_device: str, rx_channel: str
    ) -> list[dict]:
        """Route one Dante transmit channel into one receive channel (subscribe rx to tx).

        Channels accept a channel number ("1") or channel name ("Out L").
        """
        devices = await _devices()
        _, txd = _find(devices, tx_device)
        rx_server, rxd = _find(devices, rx_device)
        txc = _resolve_channel(txd, "tx", tx_channel)
        rxc = _resolve_channel(rxd, "rx", rx_channel)
        return await _apply(
            [
                {
                    "rx_device": rx_server,
                    "rx_channel": rxc.number,
                    "tx_device": txd.name,
                    "tx_channel": _channel_label(txc),
                }
            ]
        )

    @mcp.tool()
    async def dante_route_bulk(
        tx_device: str, rx_device: str, count: int = 0, tx_offset: int = 0, rx_offset: int = 0
    ) -> list[dict]:
        """Route channels 1:1 from a tx device to an rx device (e.g. a stereo pair or a whole bank).

        count 0 = as many pairs as fit. Offsets are 0-based channel positions
        (tx_offset=2, rx_offset=0, count=2 routes tx 3-4 into rx 1-2).
        """
        devices = await _devices()
        _, txd = _find(devices, tx_device)
        rx_server, rxd = _find(devices, rx_device)
        txs = sorted(txd.tx_channels.values(), key=lambda c: c.number)[tx_offset:]
        rxs = sorted(rxd.rx_channels.values(), key=lambda c: c.number)[rx_offset:]
        pairs = list(zip(txs, rxs, strict=False))
        if count:
            pairs = pairs[:count]
        if not pairs:
            raise ValueError("no channel pairs available with those offsets")
        return await _apply(
            [
                {
                    "rx_device": rx_server,
                    "rx_channel": rc.number,
                    "tx_device": txd.name,
                    "tx_channel": _channel_label(tc),
                }
                for tc, rc in pairs
            ]
        )

    @mcp.tool()
    async def dante_unroute(rx_device: str, rx_channels: list[str] | None = None) -> list[dict]:
        """Clear subscriptions on a receiving device. rx_channels None = clear every rx channel."""
        devices = await _devices()
        rx_server, rxd = _find(devices, rx_device)
        if rx_channels:
            numbers = [_resolve_channel(rxd, "rx", c).number for c in rx_channels]
        else:
            numbers = [
                s["rx_channel_number"]
                for s in rxd.record.get("subscriptions") or []
                if s.get("tx_device")
            ]
        if not numbers:
            return []
        return await _apply([{"rx_device": rx_server, "rx_channel": n} for n in numbers])

    @mcp.tool()
    async def dante_rename_channel(device: str, direction: str, channel: str, name: str) -> str:
        """Rename a Dante channel. direction: "tx" or "rx". Empty name resets to factory label."""
        if direction not in ("tx", "rx"):
            raise ValueError("direction must be 'tx' or 'rx'")
        devices = await _devices()
        server_name, d = _find(devices, device)
        c = _resolve_channel(d, direction, channel)
        await call_daemon(
            "POST",
            "/rename-channel",
            {
                "device": server_name,
                "channel_type": direction,
                "channel_number": c.number,
                "name": name,
            },
        )
        return f"{direction}{c.number}@{d.name} -> {name or '(reset)'}"

    @mcp.tool()
    async def dante_rename_device(device: str, name: str) -> str:
        """Rename a Dante device (empty name resets). Existing routes to it re-resolve by name."""
        devices = await _devices()
        server_name, d = _find(devices, device)
        await call_daemon("POST", "/rename-device", {"device": server_name, "name": name})
        return f"{d.name} -> {name or '(reset)'}"

    @mcp.tool()
    async def dante_set_latency(device: str, latency_ms: float) -> str:
        """Set a Dante device's receive latency in ms (e.g. 0.25, 0.5, 1, 2, 5)."""
        devices = await _devices()
        server_name, d = _find(devices, device)
        await call_daemon("POST", "/set-latency", {"device": server_name, "latency": latency_ms})
        return f"{d.name} latency -> {latency_ms} ms"

    @mcp.tool()
    async def dante_identify(device: str) -> str:
        """Flash a Dante device's identify LED."""
        devices = await _devices()
        server_name, d = _find(devices, device)
        await call_daemon("POST", "/identify", {"device": server_name})
        return f"identifying {d.name}"

    @mcp.tool()
    async def dante_refresh(device: str | None = None) -> str:
        """Force netaudio to re-read devices (all, or one) after changes made elsewhere."""
        if device:
            devices = await _devices()
            server_name, d = _find(devices, device)
            await call_daemon("POST", "/refresh", {"device": server_name}, timeout=120.0)
            return f"re-read {d.name}"
        await call_daemon("POST", "/refresh", {}, timeout=120.0)
        return "re-read all devices"

    # ── routing snapshots: named presets of the whole matrix ──────────────

    def _snapshot_path(name: str):
        safe = "".join(ch for ch in name if ch.isalnum() or ch in "-_ ").strip()
        if not safe:
            raise ValueError("invalid snapshot name")
        return config.state_dir("dante-snapshots") / f"{safe}.json"

    @mcp.tool()
    async def dante_snapshot_save(name: str) -> dict:
        """Save the current Dante routing matrix as a named snapshot (e.g. "tracking")."""
        devices = await _devices()
        routes = []
        for server_name, d in devices.items():
            for s in d.record.get("subscriptions") or []:
                if s.get("tx_device"):
                    routes.append(
                        {
                            "rx_device_name": d.name,
                            "rx_device": server_name,
                            "rx_channel": s.get("rx_channel_number"),
                            "tx_device": s.get("tx_device"),
                            "tx_channel": s.get("tx_channel"),
                        }
                    )
        path = _snapshot_path(name)
        path.write_text(json.dumps(routes, indent=2))
        return {"saved": str(path), "routes": len(routes)}

    @mcp.tool()
    async def dante_snapshot_list() -> list[str]:
        """List saved Dante routing snapshots."""
        return sorted(p.stem for p in config.state_dir("dante-snapshots").glob("*.json"))

    @mcp.tool()
    async def dante_snapshot_load(name: str, clear_others: bool = False) -> dict:
        """Recall a saved routing snapshot. clear_others: also clear rx channels on the
        snapshot's devices that were unrouted when it was saved."""
        saved = json.loads(_snapshot_path(name).read_text())
        devices = await _devices()
        routes = []
        for r in saved:
            try:
                rx_server, _ = _find(devices, r["rx_device_name"])
            except ValueError:
                rx_server = r["rx_device"]
            routes.append(
                {
                    "rx_device": rx_server,
                    "rx_channel": r["rx_channel"],
                    "tx_device": r["tx_device"],
                    "tx_channel": r["tx_channel"],
                }
            )
        if clear_others:
            keep = {(r["rx_device"], r["rx_channel"]) for r in routes}
            for rx_server in {r["rx_device"] for r in routes}:
                d = devices.get(rx_server)
                for s in (d.record.get("subscriptions") or []) if d else []:
                    key = (rx_server, s.get("rx_channel_number"))
                    if s.get("tx_device") and key not in keep:
                        routes.append({"rx_device": rx_server, "rx_channel": key[1]})
        results = await _apply(routes)
        return {
            "applied": sum(1 for r in results if r["ok"]),
            "failed": [r for r in results if not r["ok"]],
        }
