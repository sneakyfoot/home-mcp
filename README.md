# home-mcp

One MCP server for everything controllable on the home LAN. Built from the
`sneakyfoot/flake-templates#python` template (impure uv mode).

| Area | Tools | Backend | Setup |
|---|---|---|---|
| Dante audio | `dante_devices`, `dante_channels`, `dante_routes`, `dante_route`, `dante_route_bulk`, `dante_unroute`, `dante_rename_*`, `dante_set_latency`, `dante_identify`, `dante_refresh`, `dante_snapshot_save/load/list` | [netaudio](https://github.com/chris-ritsen/network-audio-controller) (spawns a private native engine) | none |
| Yamaha AVR | `yamaha_status`, `yamaha_capabilities`, `yamaha_power`, `yamaha_volume`, `yamaha_mute`, `yamaha_input`, `yamaha_sound_program`, `yamaha_pure_direct`, `yamaha_playback`, `yamaha_presets`, `yamaha_recall_preset` | YXC HTTP API | none |
| Hue | `hue_pair`, `hue_lights`, `hue_rooms`, `hue_scenes`, `hue_set_light`, `hue_set_room`, `hue_scene` | CLIP v1 | press bridge button → `hue_pair` |
| Apple TV / HomePod | `apple_devices`, `apple_now_playing`, `apple_remote`, `apple_volume`, `apple_apps`, `apple_launch_app`, `apple_pair_begin/finish` | pyatv | Apple TV remote/apps: `apple_pair_begin` → PIN on TV → `apple_pair_finish` |
| Apple Home | `shortcut_list`, `shortcut_run` | macOS `shortcuts` CLI | only when the server runs on a Mac |
| UniFi | `unifi_set_api_key`, `unifi_clients`, `unifi_devices`, `protect_cameras`, `protect_snapshot` | UniFi OS integration API | create API key in UniFi OS → `unifi_set_api_key` |
| Discovery | `network_discover` | zeroconf | none |

Config and credentials: `~/.config/home-mcp/config.json` (or `$HOME_MCP_CONFIG_DIR`);
Dante routing snapshots live next to it. Host defaults can be overridden with
`HOME_MCP_<KEY>` env vars (e.g. `HOME_MCP_YAMAHA_HOST`).

## Running

- `nix run .` — stdio transport.
- `nix run . -- serve --port 8765 --allowed-host <hostname>` — streamable HTTP at `/mcp`.
  DNS-rebinding protection rejects Host headers not in `--allowed-host`.
  Optional bearer auth with `HOME_MCP_TOKEN_FILE`.

## NixOS

```nix
imports = [ inputs.home-mcp.nixosModules.default ];
nix.settings.sandbox = "relaxed";   # impure uv build
services.home-mcp = {
  enable = true;
  allowedHosts = [ "ez" "192.168.1.100" ];
};
```

The service runs as a DynamicUser with state in `/var/lib/home-mcp`, enables
nix-ld (uv-managed cpython, manylinux wheels, netaudio's bundled engine), and is
restricted by systemd `IPAddressAllow` to `allowedNetworks` (default
`192.168.1.0/24`) plus localhost/multicast/link-local. Without `tokenFile` there
is no auth, so the LAN restriction is the boundary.

## Notes

- `srptools<2` is pinned: pyatv 0.18 passes the SRP private key positionally,
  which srptools 2.0 made keyword-only (breaks HomePod/Apple TV connections).
- `nix/python-uv.nix` gates the nix-ld build attrs to Linux; the template applied
  them unconditionally, which pulls glibc and fails evaluation on macOS.
