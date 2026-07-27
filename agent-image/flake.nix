{
  description = "agent-container workspace image (Nix-built, per-uid)";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
    claude-code.url = "github:sadjow/claude-code-nix";
    claude-code.inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs =
    {
      self,
      nixpkgs,
      claude-code,
    }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      allSystems = systems ++ [
        "aarch64-darwin"
        "x86_64-darwin"
      ];
      forAllSystems = nixpkgs.lib.genAttrs systems;

      # uid/gid are machine-local: read from env (default 1000). Needs --impure.
      # Inputs stay pinned by flake.lock regardless of --impure.
      envInt =
        name: default:
        let
          v = builtins.getEnv name;
        in
        if v == "" then default else nixpkgs.lib.toInt v;

      # nixpkgs with a single unfree exception and one local patch:
      # - `textual-speedups` (pulled by `mistral-vibe` via `textual`) is marked
      #   unfree only because upstream hasn't declared a license file yet —
      #   same author as textual, source on GitHub. Configured in-flake (not
      #   via NIXPKGS_ALLOW_UNFREE) so the pure `.#toolchain` build permits it.
      # - `mistral-vibe`'s bash tool spawns subprocesses with
      #   `LC_ALL=en_US.UTF-8`; the build sandbox has no generated locale, so
      #   `/bin/sh` prints a `setlocale` warning to stderr. Two pytest cases
      #   assert stderr is empty and fail on that warning alone (the tool's
      #   actual behaviour is fine). The package uses `__structuredAttrs`, so
      #   input-list overrides are silently dropped — only `disabledTests`
      #   survives an override. Skip the two locale-fragile cases.
      pkgsFor =
        system:
        import nixpkgs {
          inherit system;
          config.allowUnfreePredicate =
            pkg:
            nixpkgs.lib.elem (nixpkgs.lib.getName pkg) [
              "textual-speedups"
            ];
          overlays = [
            (_final: prev: {
              mistral-vibe = prev.mistral-vibe.overrideAttrs (old: {
                disabledTests = (old.disabledTests or [ ]) ++ [
                  "test_runs_echo_successfully"
                  "test_truncates_output_to_max_bytes"
                ];
              });
            })
          ];
        };

      # `.#toolchain` is pure and uid-agnostic: the same store path serves every
      # user, so all per-user /nix volumes seed and upgrade from one closure.
      mkToolchain =
        system:
        (import ./toolchain.nix {
          pkgs = pkgsFor system;
          claude-code-pkg = claude-code.packages.${system}.claude-code;
        }).toolchain;

      # `.#image` carries the uid (passwd/group, home chown, per-user profile
      # dir), so it stays impure. It installs the toolchain as generation 1 of
      # the default profile.
      mkImage =
        system:
        import ./image.nix {
          pkgs = pkgsFor system;
          claude-code-pkg = claude-code.packages.${system}.claude-code;
          uid = envInt "AGENT_UID" 1000;
          gid = envInt "AGENT_GID" 1000;
        };
    in
    {
      packages = forAllSystems (system: {
        toolchain = mkToolchain system;
        image = mkImage system;
        default = mkImage system;
      });
      formatter = nixpkgs.lib.genAttrs allSystems (system: nixpkgs.legacyPackages.${system}.nixfmt-tree);
    };
}
