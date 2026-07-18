# Spec: Registry-based incremental image shipping (nix2container)

Status: **ready-for-agent** · Refactor of `agent build` / `agent clean` shipping pipeline
Glossary: see [`CONTEXT.md`](../CONTEXT.md) (warm rebuild, cold rebuild, shipping, manifest, registry, gcroot, toolchain closure).

## Problem Statement

`agent build` on a **cold rebuild (small-delta)** — the common maintenance case, e.g. bumping the claude-code flake input — takes ~128s wall-clock even though almost nothing changed. The cost is structural: **shipping** is O(image size), not O(change size). Today the image is one monolithic `buildLayeredImage` tarball (~2.9G zstd, ~20G uncompressed); `nix build` re-tars and re-compresses the whole closure (~62s) and `docker load` streams and decompresses the whole tarball across the Docker Desktop VM boundary (~66s) — both re-process all 20G to move a few hundred MB of actual change. The user wants small changes to rebuild fast, but not at the cost of a tangled pipeline: a **maintainable, elegant design is the priority; total wall-clock is secondary**.

The **warm rebuild** path is already solved (~2.9s via a marker short-circuit) and is not the problem here.

## Solution

Replace the monolithic-tarball pipeline with per-layer, content-addressed images (`nix2container`) shipped through a **persistent local registry**, so that shipping becomes O(changed layers):

- `nix build` produces a small **manifest** (JSON) in the store instead of a multi-GB tarball — unchanged layers are never re-tarred or re-compressed.
- The patched `skopeo-nix2container` pushes the manifest to a persistent local **registry**; unchanged layers already present are skipped (HEAD checks).
- The host `docker pull`s from that registry, fetching only the layers it doesn't already have, then tags the result as the **user image** `agent:u<uid>g<gid>`.

On a claude-code bump only the changed layers are built, pushed, and pulled; the large **toolchain closure** stays put on both sides. The pipeline collapses to a single phase (no probe/build split, no marker), and drops the podman engine abstraction entirely (docker-only).

This is a **full replacement** developed on a branch. If the rootfs port turns out inelegant or the payoff doesn't materialize, we **abort back to the current committed state** (monolithic `buildLayeredImage` + zstd + `docker load`); git is the only rollback. The abort/payoff judgment is the user's, made after seeing the working prototype and its measurements — no pre-committed numeric threshold.

## User Stories

1. As a maintainer, I want a claude-code version bump to rebuild and be usable in well under the current ~128s, so that routine upkeep isn't a coffee break.
2. As a maintainer, I want an unchanged rebuild (**warm rebuild**) to still complete in a few seconds, so that re-running `agent build` is cheap even without the marker.
3. As a maintainer, I want only the layers that actually changed to be built, compressed, pushed, and pulled, so that wall-clock tracks the size of my change, not the size of the image.
4. As a maintainer, I want the build pipeline to read as a single linear flow (build → push → pull → tag), so that I can understand and modify it without tracing a probe/marker/re-tag branch tree.
5. As a maintainer, I want the produced image to launch claude with `--dangerously-skip-permissions` exactly as today, so that `agent run` behavior is unchanged.
6. As a user of the container, I want to run as the `agent` user with `/home/agent` owned by my uid/gid, so that files I create have the right ownership.
7. As a user of the container, I want nix-ld to keep working (unpatched manylinux wheels installed by `uv`/`pip` resolve their loader), so that Python tooling doesn't break.
8. As a user of the container, I want the Lean 4 toolchain, TeX Live, and all other tools present and on `PATH` exactly as today, so that nothing regresses functionally.
9. As a maintainer, I want the registry to persist across builds, so that each build can dedup against previously shipped layers.
10. As a maintainer, I want the registry started automatically by `agent build` if it isn't running, so that I never have to manage it by hand.
11. As a maintainer, I want `agent build` to not require editing docker `daemon.json`, so that setup stays turnkey (pull over loopback is insecure-by-default).
12. As a maintainer, I want the current image and its whole toolchain closure pinned by a gcroot, so that `nix-collect-garbage` can never evict the toolchain and force a slow from-scratch rebuild.
13. As a maintainer, I want `agent clean` to tear down the registry container and its blob volume alongside agent images, so that I can reclaim space with one command.
14. As a maintainer, I want `agent clean` to run a `nix store gc` that is safe because of the gcroot, so that old layer content is reclaimed while the toolchain survives.
15. As a maintainer, I want the obsolete tarball-reclaim logic removed when the tarball pipeline goes away, so that dead code doesn't linger.
16. As a maintainer, I want the podman engine abstraction removed, so that there is a single, straightforward docker code path to maintain.
17. As a maintainer, I want concurrent `agent build` invocations to still be guarded, so that two builds don't corrupt shared state.
18. As a maintainer running a from-scratch build on a fresh machine, I accept it stays slow (empty registry, full first pull), so that we can keep the design simple and optimize only the common case.
19. As a maintainer, I want to abort the whole refactor cleanly if the rootfs port is ugly or the speedup doesn't appear, so that I'm never trapped in a half-migrated state.
20. As a maintainer, I want the image definition to remain readable after losing `fakeRootCommands`, so that the rootfs setup (etc files, passwd/group, symlinks, ownership) is still followable.
21. As a maintainer, I want layer boundaries chosen automatically (popularity heuristic), so that I don't hand-maintain a package→layer mapping unless measurement proves I must.
22. As a maintainer, I accept gzip-compressed layers (losing zstd), so that we avoid patching the compressor for a now-second-order gain.
23. As a maintainer, I want the user image tag scheme `agent:u<uid>g<gid>` unchanged, so that `agent run` and existing runtime-volume derivation keep working.

## Implementation Decisions

**Architecture (topology A — chosen over daemon-only, base/thin-split, and status-quo):**

- Image is built with `nix2container.buildImage`, producing a **manifest** derivation (not a tarball). Added as a flake input (`nix2container`, `inputs.nixpkgs.follows`), evaluated for the Linux build system (matching the `nixos/nix` container, independent of the macOS host).
- Shipping goes **manifest → persistent registry → `docker pull` → tag**. The `docker-daemon:` skopeo transport is explicitly rejected: it streams the whole image with no blob dedup (verified against `containers/image` source); only the registry transport gives incremental transfer.

**Registry (persistent, lazily started):**

- A long-lived `registry:2` container backed by a named blob volume, started by `agent build` if not already running (same "ensure exists" pattern as the current build-cache volume).
- Published on `127.0.0.1:5000`. The **host pulls from `127.0.0.1:5000`** — loopback is insecure-by-default in docker, so no `daemon.json` edit.

**Network topology (Docker-Desktop-specific, accepted):**

- No user-defined docker network. The **build container reaches the registry via `host.docker.internal:5000`** (the published loopback port), pushing with `skopeo copy nix:<manifest> docker://host.docker.internal:5000/agent:<tag> --dest-tls-verify=false`.
- This ties the fast path to Docker Desktop; that dependency is accepted.

**Pipeline shape (single-phase, marker removed):**

- One `nix build path:/build#image --out-link <gcroot> --print-out-paths` yields the manifest path and registers the gcroot in the same step. Then `skopeo` push, `docker pull`, `docker tag` → user image. No probe/build two-phase split, no `agent:src-<hash>` marker, no re-tag branch.
- On an unchanged **warm rebuild** the registry HEAD checks and the daemon layer-existence checks make push and pull near-no-ops; estimated ~5s (accepted regression from ~2.9s).

**Layering:**

- Automatic popularity heuristic with `maxLayers = 120`; **no hand-carved layers** initially. Escalate to an explicit `buildLayer` for the toolchain closure **only if** a measured claude-code-bump rebuild transfers too much.

**Image definition port (`fakeRootCommands` → declarative):**

- `nix2container` has no scripted-fakeroot hook. The rootfs (static `/etc`, `passwd`/`group`, `/home/agent`, `nix/var/nix/profiles/per-user/agent`, `usr/bin/time` symlink, elan toolchain symlinks + `settings.toml`, and the nix-ld shim at the FHS loader path) is assembled in a `runCommand` derivation passed via `copyToRoot` (alongside `dockerTools.binSh`/`usrBinEnv` and the env).
- Ownership and modes that were `chown -R uid:gid` / `chmod 0777 tmp` become declarative **`perms`** entries applied at layer assembly (regex-scoped to `/home/agent`, the per-user profile dir, and `/tmp`). Only the home/profile trees are chowned — never `/nix/store` (avoids the known nix2container non-root store-ownership gap).
- `config` (Cmd, User=agent, the full Env list) carries over unchanged.
- `initializeNixDatabase` stays **false** to match current behavior; whether in-container `nix` needs a populated store db is a **parity item to verify**, not a planned change.

**Compression:** gzip (nix2container default); zstd dropped. No compressor patching.

**Engine:** **docker-only.** The `engine` parameter and all podman special-casing (`image_exists` podman branch, `localhost/` load-regex, etc.) are removed.

**Cleanup (`agent clean`):**

- Removes all `agent` docker images, the **registry container**, and the **registry blob volume**.
- Replaces the now-obsolete tarball-reclaim step with a `nix store gc` made safe by the gcroot (keeps current image + toolchain closure, reclaims orphaned layer content).
- No in-place `registry garbage-collect` (rely on volume removal; registry bloat between cleans is bounded — YAGNI).

**gcroot:** unchanged in intent — an indirect gcroot per user image (`/nix/var/nix/gcroots/agent-image-u<uid>g<gid>`) pinning the manifest. **Verify** the manifest's closure transitively pins the toolchain (with reproducible layers the manifest references the layer store paths); this is load-bearing for the `nix store gc` safety and must be confirmed in the prototype.

**Concurrency guard:** retained — the existing "another build running → wait" mechanism keyed on the build container name still applies to the single `nix build` container.

**Transition / rollback:** full replacement on a branch; no runtime toggle; git-only rollback to the current committed state (monolithic `buildLayeredImage` + zstd + `docker load`).

## Testing Decisions

- **What a good test checks:** external, observable behavior of the `agent` CLI — not internal helpers. Drive the real subcommands and observe docker/registry state and the running container, mirroring the `verify`-style end-to-end checks used throughout development. No unit-test harness exists for this single-script tool, and none is introduced.
- **Single seam:** the `agent` CLI subcommands (`agent build`, `agent run`, `agent clean`). Highest possible seam; no new seams.
- **Scenarios to verify:**
  1. **Behavioral parity** — after `agent build`, the produced user image launches claude, runs as `agent`, `/home/agent` is owned by the invoking uid/gid, and nix-ld resolves an unpatched wheel's loader (drive a `uv`/`pip` manylinux install or equivalent). This is the primary abort gate.
  2. **Warm rebuild** — a no-change rebuild completes in a few seconds and yields the same user image.
  3. **Cold rebuild (small-delta)** — after a claude-code bump, measure wall-clock and the pull's transferred-layer set; confirm only changed layers move and total time beats the current ~128s. Primary payoff measurement.
  4. **`agent clean`** — tears down agent images + registry container + registry volume; a subsequent build still finds the toolchain (no from-scratch re-download), proving the gcroot-guarded `nix store gc` kept the closure.
- **Prior art:** the session's manual verification cadence — timing `agent build`, inspecting `docker images`/volumes/gcroots, running `--entrypoint claude … --version` against the built image.

## Out of Scope

- **From-scratch (cold build) speed** — first build on an empty store/registry stays slow by design.
- **Warm-path optimization** — already solved; the marker is being removed, not improved.
- **Podman** — support is dropped, not ported. The `containers-storage:` transport is a clean future re-add if needed.
- **zstd / custom layer compression** — not pursued.
- **Explicit hand-carved layers** — deferred; only if the heuristic underperforms on measurement.
- **In-place registry GC** — not implemented; clean removes the whole volume.
- **Cross-platform / native-Linux hosts** — the fast path targets macOS Docker Desktop specifically.

## Further Notes

- **Abort criteria are qualitative and the user's call**, evaluated on the prototype: (a) can the `runCommand` + `perms` rootfs port reproduce current runtime behavior without ugly hacks; (b) does a small-delta rebuild actually get materially faster. Either failing → delete the branch, `main` unchanged.
- **Known third-party caveats to watch** (from research): nix2container has no scripted-fakeroot and no compression knob; skopeo's `docker-daemon:` transport does not dedup (hence the registry); reported flakiness of `docker-daemon:` copies (#2204) — avoided by using the registry transport; non-root `/nix/store` ownership gap (#50) — avoided by only chowning home/profile trees.
- **Load-bearing assumption to confirm first in the prototype:** gcrooting the manifest pins the full toolchain closure. If it does not, the `agent clean` `nix store gc` step and the "toolchain survives clean" story need rework.
- This refactor supersedes the just-committed marker + tarball-reclaim work on the shipping side (commits for the marker short-circuit and tarball reclaim). The gcroot concept survives; the marker and tarball reclaim do not.
</content>
