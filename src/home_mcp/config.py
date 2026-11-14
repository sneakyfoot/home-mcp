"""Persistent config + secrets at ~/.config/home-mcp/config.json.

Hosts default to what was discovered on the LAN; override any of them via the
config file or env vars (HOME_MCP_<KEY>, e.g. HOME_MCP_YAMAHA_HOST).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

CONFIG_DIR = Path(os.environ.get("HOME_MCP_CONFIG_DIR", Path.home() / ".config" / "home-mcp"))
CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULTS: dict[str, Any] = {
    "yamaha_host": "192.168.1.94",
    "hue_host": "192.168.1.169",
    "hue_username": None,
    # Warm tungsten white applied whenever brightness changes without an explicit color.
    "hue_default_kelvin": 2700,
    "vertex2_host": "192.168.1.193",
    "vertex2_port": 2220,
    "unifi_host": "192.168.1.1",
    "unifi_api_key": None,
    "atv_credentials": {},  # identifier -> {protocol_name: credentials}
    # Apple TV / HomePod IPs always scanned by unicast; grows as new devices are seen.
    "apple_hosts": ["192.168.1.65", "192.168.1.74", "192.168.1.226"],
}


def load() -> dict[str, Any]:
    data = dict(DEFAULTS)
    if CONFIG_FILE.exists():
        data.update(json.loads(CONFIG_FILE.read_text()))
    for key in DEFAULTS:
        env = os.environ.get(f"HOME_MCP_{key.upper()}")
        if env:
            data[key] = env
    return data


def get(key: str) -> Any:
    return load().get(key)


def update(**changes: Any) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    current = json.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {}
    current.update(changes)
    CONFIG_FILE.write_text(json.dumps(current, indent=2))
    CONFIG_FILE.chmod(0o600)


def state_dir(name: str) -> Path:
    path = CONFIG_DIR / name
    path.mkdir(parents=True, exist_ok=True)
    return path
