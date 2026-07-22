{
  pkgs,
  claude-code-pkg,
}:
# The agent toolchain, as one uid-agnostic `buildEnv`. This is the single
# source of the store contents that seed and upgrade the shared /nix volume:
# `.#image` bakes it as generation 1 of the default profile; `agent update`
# rebuilds it and swaps it in. It is pure (no getEnv, no --impure), so the same
# store path serves every user and every per-user volume dedups against it.
let
  lib = pkgs.lib;

  # Lean is intentionally *not* baked here: the user manages Lean with elan
  # (`~/.elan` holds elan-downloaded toolchains). elan's binaries are unpatched
  # ELFs that resolve through nix-ld and NIX_LD_LIBRARY_PATH (see nixLdLib), so
  # they run without a store-resident Lean. `elan` itself is on PATH below.

  # Libraries exposed to unpatched ELF binaries (e.g. pip/uv wheels, elan's Lean)
  # via nix-ld.
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

  # A deterministic `/lib` holding the real glibc loader plus the nix-ld
  # libraries, folded into the toolchain so both live at *profile-stable* paths:
  # NIX_LD → `<profile>/lib/ld-linux-*` and NIX_LD_LIBRARY_PATH → `<profile>/lib`,
  # which an in-place upgrade repoints. Built explicitly rather than by adding
  # these packages to the buildEnv, because buildEnv honours each package's
  # `meta.outputsToInstall` — which for glibc/openssl is `bin`/`dev`, *not* the
  # `out` output that actually carries the loader and the shared objects.
  # `makeLibraryPath` resolves each to its `getLib` output, so the loop sees the
  # real `/lib`. glibc is last and first-writer-wins, so its loader/libc are the
  # ones exposed.
  nixLdLib = pkgs.runCommand "agent-nix-ld-lib" { } ''
    mkdir -p $out/lib
    for d in ${lib.concatMapStringsSep " " (p: "${lib.getLib p}/lib") (nixLdLibraries ++ [ pkgs.glibc ])}; do
      [ -d "$d" ] || continue
      for f in "$d"/*; do
        name=$(basename "$f")
        [ -e "$out/lib/$name" ] || ln -s "$f" "$out/lib/$name"
      done
    done
  '';

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

  toolchain = pkgs.buildEnv {
    name = "agent-toolchain";
    paths =
      (with pkgs; [
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
        pandoc
        poppler-utils
        procps
        psutils
        (python3.withPackages (ps: [ ps.pygments ]))
        ripgrep
        ruff
        shellcheck
        shfmt
        starship
        sudo
        texlab
        (texliveMedium.withPackages (
          ps: with ps; [
            latexmk
            pgf
            csquotes
            urlbst
            upquote
            amsmath
            thmtools
            mathtools
            marginnote
            cleveref
            minted
          ]
        ))
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
        # Store-resident members reached solely by a root-fs symlink — in a
        # mutable, GC'd store only paths inside the rooted generation's closure
        # survive, so each must be a buildEnv member:
        #   - nix-ld: reached by the FHS loader shim (/lib64|/lib ld-linux-*);
        #   - nixLdLib: the loader + wheel libraries at a profile-stable /lib
        #     (see above), keeping glibc and the nix-ld set in the rooted closure.
        nix-ld
        nixLdLib
      ]);
    ignoreCollisions = true;
    pathsToLink = [
      "/bin"
      "/lib"
      "/libexec"
      "/etc"
      "/etc/ssl"
      "/share/git-core"
      "/share/git"
      "/share/man"
      "/share/fonts"
    ];
  };
in
{
  inherit
    toolchain
    fhsDynamicLinker
    ;
}
