# NixOS service: `home-mcp serve` as a long-running streamable-HTTP MCP endpoint.
#
#   imports = [ inputs.home-mcp.nixosModules.default ];
#   services.home-mcp.enable = true;
#
# Building the package needs `nix.settings.sandbox = "relaxed"` on the build host
# (impure uv mode, see nix/python-uv.nix). Running it needs nix-ld (uv-managed
# cpython + manylinux wheels + netaudio's bundled engine), enabled below.
self:
{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.services.home-mcp;
in
{
  options.services.home-mcp = {
    enable = lib.mkEnableOption "home-mcp LAN device control MCP server";

    package = lib.mkOption {
      type = lib.types.package;
      default = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
    };

    port = lib.mkOption {
      type = lib.types.port;
      default = 8765;
    };

    allowedHosts = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ config.networking.hostName ];
      description = "Host header values clients may use (DNS rebinding protection). localhost is always allowed.";
    };

    allowedNetworks = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ "192.168.1.0/24" ];
      description = ''
        Networks the service may talk to and accept clients from (systemd IPAddressAllow).
        localhost, multicast and link-local are always allowed for mDNS / Dante discovery.
      '';
    };

    tokenFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      description = "Optional file holding a bearer token clients must send. null = no auth (LAN-only via allowedNetworks).";
    };

    openFirewall = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Open the MCP port plus mDNS and Dante notification ports.";
    };
  };

  config = lib.mkIf cfg.enable {
    programs.nix-ld.enable = true;

    networking.firewall = lib.mkIf cfg.openFirewall {
      allowedTCPPorts = [ cfg.port ];
      # 5353 mDNS; 8702/8708 Dante multicast control notifications + heartbeats.
      allowedUDPPorts = [
        5353
        8702
        8708
      ];
    };

    systemd.services.home-mcp = {
      description = "home-mcp LAN device control MCP server";
      wantedBy = [ "multi-user.target" ];
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];

      environment = {
        HOME = "/var/lib/home-mcp";
        HOME_MCP_CONFIG_DIR = "/var/lib/home-mcp";
        NIX_LD = pkgs.stdenv.cc.bintools.dynamicLinker;
        NIX_LD_LIBRARY_PATH = lib.makeLibraryPath (
          with pkgs;
          [
            stdenv.cc.cc.lib
            zlib
            openssl
            libffi
            bzip2
            xz
            zstd
            sqlite
            expat
            ncurses
            readline
            libuuid
          ]
        );
      }
      // lib.optionalAttrs (cfg.tokenFile != null) {
        HOME_MCP_TOKEN_FILE = "%d/token";
      };

      serviceConfig = {
        ExecStart = lib.escapeShellArgs (
          [
            "${cfg.package}/bin/home-mcp"
            "serve"
            "--port"
            (toString cfg.port)
          ]
          ++ lib.concatMap (h: [
            "--allowed-host"
            h
          ]) cfg.allowedHosts
        );
        DynamicUser = true;
        StateDirectory = "home-mcp";
        StateDirectoryMode = "0700";
        Restart = "always";
        RestartSec = 5;
        LoadCredential = lib.optional (cfg.tokenFile != null) "token:${cfg.tokenFile}";

        IPAddressDeny = "any";
        IPAddressAllow = [
          "localhost"
          "multicast"
          "link-local"
        ]
        ++ cfg.allowedNetworks;

        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        PrivateTmp = true;
        PrivateDevices = true;
        ProtectKernelTunables = true;
        ProtectKernelModules = true;
        ProtectControlGroups = true;
        RestrictAddressFamilies = [
          "AF_INET"
          "AF_INET6"
          "AF_UNIX"
          "AF_NETLINK"
        ];
      };
    };
  };
}
