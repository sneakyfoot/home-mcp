"""Scenes: named one-shot recipes across projector, Yamaha and Hue.

A scene is a dict with any of:
  projector:  true / false                  (via the Vertex2, see hdfury.py)
  yamaha:     {power, input, program, volume_db}
  hue_dim:    {rooms: [...], brightness: 0-100}
              Dims lights that are already ON to `brightness` at the warm default kelvin.
              Never turns lights on, never turns them off, never brightens them.
              (The Hue rooms are elsewhere in the house; this is bleed-light control.)

Built-in defaults live in DEFAULT_SCENES; scenes saved with scene_save live in config.json
under "scenes" and override defaults of the same name.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from mcp.server.mcpserver import MCPServer

from home_mcp import config, hdfury, hue, yamaha

DEFAULT_SCENES: dict[str, dict[str, Any]] = {
    "movie": {
        "description": "Projector on, Yamaha HDMI1 in Straight, dim bleed light from Hue rooms",
        "projector": True,
        "yamaha": {"power": True, "input": "hdmi1", "program": "straight"},
        "hue_dim": {"rooms": ["Bedroom", "Office"], "brightness": 15},
    },
    "music": {
        "description": "Projector off, Yamaha HDMI1 (Apple TV) in 7ch Stereo",
        "projector": False,
        "yamaha": {"power": True, "input": "hdmi1", "program": "7ch_stereo"},
    },
    "all_off": {
        "description": "Projector and Yamaha off (Hue untouched)",
        "projector": False,
        "yamaha": {"power": False},
    },
}

ALLOWED_KEYS = {"description", "projector", "yamaha", "hue_dim"}


def _scenes() -> dict[str, dict[str, Any]]:
    return {**DEFAULT_SCENES, **(config.get("scenes") or {})}


async def _yamaha(spec: dict[str, Any]) -> list[str]:
    done = []
    if "power" in spec:
        await yamaha._get("main/setPower", power="on" if spec["power"] else "standby")
        done.append(f"power {'on' if spec['power'] else 'standby'}")
        if not spec["power"]:
            return done
        # Freshly woken receivers answer "guarded" to input/program changes for a few seconds.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if (await yamaha._get("main/getStatus")).get("power") == "on":
                break
            await asyncio.sleep(0.5)
    for key, path, param in (
        ("input", "main/setInput", "input"),
        ("program", "main/setSoundProgram", "program"),
    ):
        if key in spec:
            for attempt in range(6):
                try:
                    await yamaha._get(path, **{param: spec[key]})
                    break
                except RuntimeError as exc:
                    if "guarded" not in str(exc) or attempt == 5:
                        raise
                    await asyncio.sleep(1)
            done.append(f"{key} {spec[key]}")
    if "volume_db" in spec:
        await yamaha._get(
            "main/setActualVolume", mode="db", value=f"{float(spec['volume_db']):.1f}"
        )
        done.append(f"volume {spec['volume_db']} dB")
    return done


async def _hue_dim(spec: dict[str, Any]) -> list[str]:
    target = int(spec["brightness"])
    bri = max(1, min(254, round(target * 254 / 100)))
    ct = max(153, min(500, round(1_000_000 / int(config.get("hue_default_kelvin")))))
    lights = await hue._req("GET", "lights")
    groups = await hue._req("GET", "groups")
    wanted = {r.lower() for r in spec.get("rooms", [])}
    done = []
    for group in groups.values():
        if group.get("name", "").lower() not in wanted:
            continue
        for light_id in group.get("lights", []):
            state = lights.get(light_id, {}).get("state", {})
            name = lights.get(light_id, {}).get("name", light_id)
            if not state.get("on"):
                done.append(f"{name}: off, left off")
            elif state.get("bri", 0) <= bri:
                done.append(f"{name}: already at/below {target}%, left as is")
            else:
                await hue._req(
                    "PUT", f"lights/{light_id}/state", {"bri": bri, "ct": ct, "transitiontime": 20}
                )
                done.append(f"{name}: dimmed to {target}% warm")
    return done


async def run_scene(name: str) -> dict[str, Any]:
    scenes = _scenes()
    if name not in scenes:
        raise ValueError(f"unknown scene '{name}'. Known: {sorted(scenes)}")
    scene = scenes[name]
    steps: dict[str, Any] = {}

    async def step(label: str, coro) -> None:
        try:
            steps[label] = await coro
        except Exception as exc:  # noqa: BLE001 - one failing device shouldn't abort the rest
            steps[label] = f"FAILED: {type(exc).__name__}: {exc}"

    jobs = []
    if "projector" in scene:
        jobs.append(step("projector", hdfury.set_projector_power(bool(scene["projector"]))))
    if "yamaha" in scene:
        jobs.append(step("yamaha", _yamaha(scene["yamaha"])))
    if "hue_dim" in scene:
        jobs.append(step("hue", _hue_dim(scene["hue_dim"])))
    await asyncio.gather(*jobs)
    return {"scene": name, "steps": steps}


def register(mcp: MCPServer) -> None:
    @mcp.tool()
    async def scene_list() -> dict[str, dict]:
        """List scenes (movie, music, all_off, plus any saved) with their full definitions."""
        return _scenes()

    @mcp.tool()
    async def scene_run(name: str) -> dict:
        """Run a scene by name. Devices run in parallel; each step reports its own result, so a
        failure on one device (reported as FAILED) doesn't stop the others."""
        return await run_scene(name)

    @mcp.tool()
    async def scene_save(name: str, definition: dict) -> dict:
        """Create or replace a scene. definition keys: description, projector (bool),
        yamaha {power, input, program, volume_db}, hue_dim {rooms, brightness}.
        Saving over a built-in name (movie/music/all_off) overrides it."""
        unknown = set(definition) - ALLOWED_KEYS
        if unknown:
            raise ValueError(f"unknown keys {sorted(unknown)}; allowed: {sorted(ALLOWED_KEYS)}")
        saved = dict(config.get("scenes") or {})
        saved[name] = definition
        config.update(scenes=saved)
        return {name: definition}
