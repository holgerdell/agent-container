{ pkgs, claude-code-pkg, uid ? 1000, gid ? 1000 }:
let
  lib = pkgs.lib;

  passwd = pkgs.writeText "passwd" ''
    root:x:0:0:root:/root:/bin/sh
    agent:x:${toString uid}:${toString gid}:agent:/home/agent:/bin/bash
    nobody:x:65534:65534:nobody:/:/sbin/nologin
  '';

  group = pkgs.writeText "group" ''
    root:x:0:
    agent:x:${toString gid}:
    nobody:x:65534:
  '';

  env = pkgs.buildEnv {
    name = "agent-env";
    paths = with pkgs; [
      bashInteractive coreutils findutils gnused gawk diffutils sudo which
      fzf gnutar gzip bzip2 xz zstd zip unzip gh jq less man-db nano neovim ripgrep
      shellcheck shfmt curl cacert git openssh gnupg just
      delta typos uv ruff mypy ty python3 typst
      nix katex texlab
      nodejs claude-code-pkg opencode starship
      elan
      procps htop tmux
    ];
    ignoreCollisions = true;
    pathsToLink = [ "/bin" "/lib" "/etc" "/share/git-core" "/share/git" "/share/man" ];
  };

  # Libraries exposed to unpatched ELF binaries (e.g. pip/uv wheels) via nix-ld.
  # Mirrors the default set baked into the NixOS `programs.nix-ld` module
  # (nixos/modules/programs/nix-ld.nix). Extend for heavier wheels:
  # torch/scipy → openblas, opencv → libGL, cuda packages, etc.
  nixLdLibraries = with pkgs; [
    zlib zstd stdenv.cc.cc.lib curl openssl attr libssh bzip2
    libxml2 acl libsodium util-linux xz systemd
    z3
  ];

  # FHS path where unpatched ELF binaries expect to find the dynamic loader.
  # Derived from the real glibc loader's basename so it matches whatever
  # manylinux wheels were built against. Older nixpkgs channels lack
  # `hostPlatform.dynamicLinker`, so we compute it from `bintools.dynamicLinker`
  # (a Nix store path) plus the FHS directory convention per arch.
  fhsDynamicLinker =
    let
      loaderBasename = baseNameOf pkgs.stdenv.cc.bintools.dynamicLinker;
      loaderDir = if pkgs.stdenv.hostPlatform.isx86_64 then "/lib64" else "/lib";
    in
      "${loaderDir}/${loaderBasename}";

in pkgs.dockerTools.buildLayeredImage {
  name = "agent";

  contents = [ env pkgs.dockerTools.binSh pkgs.dockerTools.usrBinEnv ];

  fakeRootCommands = ''
    cp -r ${./root/etc}/. etc/
    install -m 0644 ${passwd} etc/passwd
    install -m 0644 ${group} etc/group

    mkdir -p home/agent tmp
    mkdir -p nix/var/nix/profiles/per-user/agent
    # nix-ld: place the shim at the FHS loader path so unpatched ELFs (e.g.
    # manylinux wheels installed by uv) resolve their interpreter to a shim
    # that honors NIX_LD and NIX_LD_LIBRARY_PATH instead of failing to find
    # /lib64/ld-linux-*.so. Nix-built binaries are unaffected — they carry
    # their own RUNPATH and point at the real glibc loader in /nix/store.
    mkdir -p .$(dirname ${fhsDynamicLinker})
    ln -s ${pkgs.nix-ld}/libexec/nix-ld .${fhsDynamicLinker}
    chmod 0777 tmp
    chown -R ${toString uid}:${toString gid} home/agent
    chown -R ${toString uid}:${toString gid} nix/var/nix/profiles/per-user/agent
  '';

  config = {
    Cmd = [ "claude" "--dangerously-skip-permissions" ];
    User = "agent";
    Env = [
      "HOME=/home/agent"
      "PATH=${env}/bin:/home/agent/.local/bin:/home/agent/.elan/bin"
      "SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
      "EDITOR=nvim"
      "VISUAL=nvim"
      "NPM_CONFIG_PREFIX=/home/agent/.local"
      "UV_LINK_MODE=copy"
      "UV_PROJECT_ENVIRONMENT=.venv-linux"
      "UV_PYTHON_PREFERENCE=only-system"
      "DEVCONTAINER=true"
      "XDG_CONFIG_HOME=/home/agent/.config"
      "XDG_DATA_HOME=/home/agent/.local/share"
      "XDG_STATE_HOME=/home/agent/.local/state"
      "XDG_CACHE_HOME=/home/agent/.cache"
      "NIX_CONFIG=experimental-features = nix-command flakes"
      "NIX_LD=${pkgs.stdenv.cc.bintools.dynamicLinker}"
      "NIX_LD_LIBRARY_PATH=${lib.makeLibraryPath nixLdLibraries}"
      "LD_LIBRARY_PATH=${lib.makeLibraryPath nixLdLibraries}"
    ];
  };
}
