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

      # `.#toolchain` is pure and uid-agnostic: the same store path serves every
      # user, so all per-user /nix volumes seed and upgrade from one closure.
      mkToolchain =
        system:
        (import ./toolchain.nix {
          pkgs = nixpkgs.legacyPackages.${system};
          claude-code-pkg = claude-code.packages.${system}.claude-code;
        }).toolchain;

      # `.#image` carries the uid (passwd/group, home chown, per-user profile
      # dir), so it stays impure. It installs the toolchain as generation 1 of
      # the default profile.
      mkImage =
        system:
        import ./image.nix {
          pkgs = nixpkgs.legacyPackages.${system};
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
