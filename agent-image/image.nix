{
  pkgs,
  claude-code-pkg,
  uid ? 1000,
  gid ? 1000,
}:
let
  lib = pkgs.lib;

  leanVersion = "4.31.0";
  # Upstream tarball names: lean-4.31.0-linux.tar.zst (x86_64), lean-4.31.0-linux_aarch64.tar.zst
  leanArchSuffix = if pkgs.stdenv.hostPlatform.isx86_64 then "" else "_aarch64";

  # Lean 4 toolchain fetched into the nix store and auto-patched at image build time.
  # Being in the store makes glibc/libstdc++ runtime deps proper GC roots — the
  # root cause of the previous breakage (patched ELFs in mutable ~/.elan, depending
  # on store paths that GC was free to collect). autoPatchelfHook resolves every
  # DT_NEEDED against buildInputs, handles PIE binaries correctly, and errors loudly
  # on any missing lib rather than silently leaving broken ELFs.
  lean4Toolchain = pkgs.stdenv.mkDerivation {
    pname = "lean4-toolchain";
    version = leanVersion;

    src = pkgs.fetchurl {
      url = "https://github.com/leanprover/lean4/releases/download/v${leanVersion}/lean-${leanVersion}-linux${leanArchSuffix}.tar.zst";
      hash =
        if pkgs.stdenv.hostPlatform.isx86_64 then
          "sha256-B6YzzI2RUcvAiCXqTN2lDUsCosnLhSwBMbEwRvScrX8="
        else
          "sha256-sb8dPFhrds9KhiEqWV2Lnt2Z9DikHM6F1XgPqTR8gRs=";
    };

    nativeBuildInputs = [ pkgs.autoPatchelfHook pkgs.zstd ];
    buildInputs = with pkgs; [
      stdenv.cc.cc.lib
      gmp
      libuv
      zlib
    ];

    sourceRoot = "lean-${leanVersion}-linux${leanArchSuffix}";

    installPhase = ''
      runHook preInstall
      mkdir -p $out
      cp -a . $out
      runHook postInstall
    '';
  };

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
      bashInteractive
      bun
      bzip2
      cacert
      clang
      claude-code-pkg
      coreutils
      curl
      delta
      diffutils
      elan
      findutils
      fzf
      gawk
      gcc
      gh
      git
      gnupg
      gnugrep
      gnused
      gnutar
      gzip
      htop
      hyperfine
      jq
      just
      katex
      less
      man-db
      mypy
      nano
      nauty
      neovim
      nix
      nodejs
      opencode
      openssh
      poppler-utils
      procps
      (python3.withPackages (ps: [ ps.pygments ]))
      ripgrep
      ruff
      shellcheck
      shfmt
      starship
      sudo
      texlab
      (texliveMedium.withPackages (ps: with ps; [ latexmk pgf csquotes urlbst upquote amsmath thmtools mathtools marginnote cleveref minted ]))
      julia-mono
      time
      tmux
      ty
      typos
      typst
      uv
      unzip
      which
      xz
      zip
      zstd
    ];
    ignoreCollisions = true;
    pathsToLink = [
      "/bin"
      "/lib"
      "/etc"
      "/share/git-core"
      "/share/git"
      "/share/man"
      "/share/fonts"
    ];
  };

  # Libraries exposed to unpatched ELF binaries (e.g. pip/uv wheels) via nix-ld.
  # Mirrors the default set baked into the NixOS `programs.nix-ld` module
  # (nixos/modules/programs/nix-ld.nix). Extend for heavier wheels:
  # torch/scipy → openblas, opencv → libGL, cuda packages, etc.
  nixLdLibraries = with pkgs; [
    zlib
    zstd
    stdenv.cc.cc.lib
    curl
    openssl
    attr
    libssh
    bzip2
    libxml2
    acl
    libsodium
    util-linux
    xz
    systemd
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

in
pkgs.dockerTools.buildLayeredImage {
  name = "agent";

  contents = [
    env
    lean4Toolchain
    pkgs.dockerTools.binSh
    pkgs.dockerTools.usrBinEnv
  ];

  fakeRootCommands = ''
    cp -r ${./root/etc}/. etc/
    install -m 0644 ${passwd} etc/passwd
    install -m 0644 ${group} etc/group

    mkdir -p home/agent tmp
    mkdir -p nix/var/nix/profiles/per-user/agent
    mkdir -p usr/bin
    ln -s ${pkgs.time}/bin/time usr/bin/time

    # Pre-install Lean 4 toolchain into the store-backed elan directory.
    # ${lean4Toolchain}/bin is on PATH as fallback; elan shims in ~/.elan/bin are
    # not pre-created, so per-project toolchain switching requires running
    # `elan default leanprover/lean4:v4.31.0` once to populate the shims.
    mkdir -p home/agent/.elan/toolchains
    ln -s ${lean4Toolchain} home/agent/.elan/toolchains/leanprover--lean4---v4.31.0
    printf 'telemetry = false\nversion = "12"\ndefault_toolchain = "leanprover/lean4:v4.31.0"\n' > home/agent/.elan/settings.toml
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
    Cmd = [
      "claude"
      "--dangerously-skip-permissions"
    ];
    User = "agent";
    Env = [
      "HOME=/home/agent"
      "PATH=${lean4Toolchain}/bin:${env}/bin:/home/agent/.local/bin:/home/agent/.elan/bin"
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
      "OSFONTDIR=${env}/share/fonts"
      "NIX_CONFIG=experimental-features = nix-command flakes"
      "NIX_LD=${pkgs.stdenv.cc.bintools.dynamicLinker}"
      "NIX_LD_LIBRARY_PATH=${lib.makeLibraryPath nixLdLibraries}"
      "LD_LIBRARY_PATH=${lib.makeLibraryPath nixLdLibraries}"
    ];
  };
}
