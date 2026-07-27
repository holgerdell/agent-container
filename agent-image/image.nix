{
  pkgs,
  claude-code-pkg,
  uid ? 1000,
  gid ? 1000,
}:
let
  lib = pkgs.lib;

  tc = import ./toolchain.nix { inherit pkgs claude-code-pkg; };
  toolchain = tc.toolchain;

  # Basename of the FHS loader (e.g. ld-linux-x86-64.so.2), reused for NIX_LD.
  loaderBasename = baseNameOf tc.fhsDynamicLinker;

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

  # Register the Nix database for the toolchain closure so the seeded store is
  # *operable*: the root tooling can `nix-env --profile … --set` (upgrade) and
  # `nix-collect-garbage` (clean) against it. This replicates dockerTools'
  # `mkDbExtraCommand` rather than using `buildLayeredImageWithNixDb`, because
  # that builder registers the closure of `contents` — but `contents` must be
  # *empty* here (see below), so we root the DB at the toolchain explicitly. The
  # payoff: the toolchain is never symlinked into the rootfs, so every surviving
  # root-fs symlink is profile-stable, per the broken-symlink guard.
  registerDb = ''
    echo "Registering the Nix database for the toolchain closure..."
    export NIX_REMOTE=local?root=$PWD
    export USER=nobody
    ${pkgs.buildPackages.nix}/bin/nix-store --load-db < ${
      pkgs.closureInfo { rootPaths = [ toolchain ]; }
    }/registration
    ${pkgs.buildPackages.sqlite}/bin/sqlite3 nix/var/nix/db/db.sqlite \
      "UPDATE ValidPaths SET registrationTime = ''${SOURCE_DATE_EPOCH}"
  '';
in
pkgs.dockerTools.buildLayeredImage {
  name = "agent";
  # zstd over the default gzip: the tarball is only ever streamed to a local
  # `docker load`, so compression is pure CPU tax on both ends — zstd
  # decompresses several times faster, cutting cold-build load time.
  compressor = "zstd";

  # No `contents`: the toolchain is pulled into the image by the store-path
  # references in `extraCommands`/`fakeRootCommands` below, so it is *not*
  # symlinked into the rootfs. This keeps the rootfs free of seed-generation
  # store paths — every baked symlink points at a profile-stable path that an
  # in-place upgrade leaves valid.
  extraCommands = registerDb;

  fakeRootCommands = ''
    cp -r ${./root/etc}/. etc/
    install -m 0644 ${passwd} etc/passwd
    install -m 0644 ${group} etc/group

    mkdir -p home/agent tmp bin usr/bin
    mkdir -p nix/var/nix/profiles/per-user/agent
    mkdir -p nix/var/nix/gcroots

    # Generation 1 of the system default profile → the seed toolchain. Plain
    # symlinks, not `nix-env` (no writable Nix DB under fakeroot); `agent
    # update` adds generation 2+ via `nix-env --profile … --set` at runtime.
    ln -s ${toolchain} nix/var/nix/profiles/default-1-link
    ln -s default-1-link nix/var/nix/profiles/default
    # Make the profiles dir a real GC root so nix-collect-garbage protects the
    # current generation's whole closure just by it being the profile.
    ln -s /nix/var/nix/profiles nix/var/nix/gcroots/profiles

    # Root-filesystem symlinks — every target is a *profile-stable* path so it
    # survives an in-place upgrade + GC (the broken-symlink guard). /bin/sh and
    # /usr/bin/env replace dockerTools.binSh/usrBinEnv, which would bake
    # seed-generation store paths that dangle once GC trims the seed toolchain.
    ln -s /nix/var/nix/profiles/default/bin/bash bin/sh
    ln -s /nix/var/nix/profiles/default/bin/env usr/bin/env
    ln -s /nix/var/nix/profiles/default/bin/time usr/bin/time
    # nix-ld: place the shim at the FHS loader path so unpatched ELFs (e.g.
    # manylinux wheels installed by uv) resolve their interpreter to a shim
    # that honors NIX_LD and NIX_LD_LIBRARY_PATH. Nix-built binaries are
    # unaffected — they carry their own RUNPATH.
    mkdir -p .$(dirname ${tc.fhsDynamicLinker})
    ln -s /nix/var/nix/profiles/default/libexec/nix-ld .${tc.fhsDynamicLinker}

    # Lean is not baked: the user manages it with elan. `elan` is on PATH and
    # writes its shims to ~/.elan/bin (also on PATH); its downloaded toolchains
    # are unpatched ELFs that run via nix-ld. Nothing to seed here.

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
    # Every store-referencing var points through the profile, not a fixed seed
    # store path, so an in-place `agent update` (profile swap) upgrades the whole
    # environment — not just PATH — with no container restart. A new shell
    # resolves the profile symlink freshly and sees the new toolchain.
    Env = [
      "HOME=/home/agent"
      "PATH=/nix/var/nix/profiles/default/bin:/home/agent/.local/bin:/home/agent/.elan/bin"
      "SSL_CERT_FILE=/nix/var/nix/profiles/default/etc/ssl/certs/ca-bundle.crt"
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
      "OSFONTDIR=/nix/var/nix/profiles/default/share/fonts"
      "NIX_CONFIG=experimental-features = nix-command flakes"
      "NIX_LD=/nix/var/nix/profiles/default/lib/${loaderBasename}"
      "NIX_LD_LIBRARY_PATH=/nix/var/nix/profiles/default/lib"
      "LD_LIBRARY_PATH=/nix/var/nix/profiles/default/lib"
      # Profile-stable locale archive: glibc reads generated locales (e.g.
      # en_US.UTF-8, which mistral-vibe's bash tool sets on every subprocess)
      # from here, avoiding `setlocale` warnings polluting tool stderr.
      "LOCALE_ARCHIVE=/nix/var/nix/profiles/default/lib/locale/locale-archive"
    ];
  };
}
