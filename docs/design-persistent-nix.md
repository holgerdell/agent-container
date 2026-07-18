# Design: persistent, writeable, in-place-upgraded `/nix`

> **Implementation note (deviation from this design).** Lean is **not** baked into
> the toolchain or seeded into `~/.elan`, and `agent update` has **no Lean
> reconcile step** — the user manages Lean with `elan` (elan-downloaded toolchains
> under `~/.elan/toolchains/`, which run via nix-ld). Everything else below is
> implemented as written. The sections about `lean4Toolchain` in the store, the
> `~/.elan` symlink, and the Lean-bump reconcile therefore do not apply.

**Status: locked design, ready for implementation.** This document captures the end-state
architecture for reworking the agent container so `/nix` is a persistent, writeable, shared
volume that is upgraded *inside the running container* — no stop / rebuild / restart to update
the toolchain. It folds together six resolved design decisions into one narrative. No decision
is left open; an implementation effort should be able to work from this without re-deciding
anything.

It is a design, not code. Implementation (edits to `agent`, `agent-image/image.nix`,
`agent-image/flake.nix`) is a separate follow-up effort.

## Goal and the one invariant

Today the toolchain (a `buildEnv` called `env`, plus `lean4Toolchain`, plus `claude-code`) is
baked into the image's read-only `/nix` layers at **fixed store paths**, and `/nix` is not
volume-mounted at runtime — the container runs against the image's built-in `/nix`. Updating
the toolchain therefore means rebuild the image and recreate the container.

The destination replaces that with a **persistent, writeable, shared `/nix` volume** whose
toolchain is **upgraded in place**. One invariant governs the whole design and must survive every
implementation choice:

> **The flake stays the single source of truth.** `/nix` is never a hand-mutated pet. The
> toolchain is a declarative nix *profile*; every upgrade is `flake.lock` → build `.#toolchain`
> → atomic profile swap; rollback is `git revert` of `flake.lock` + re-run the upgrade. There is
> **one shared `/nix` volume per user**, not per-repo.

## The store / volume / profile model

### Volumes

- **`agent-nix-<username>`** (new) — the shared, persistent `/nix` volume. Named from `whoami`,
  so it is **per-user**, shared across all of that user's repos, and **stable across image
  rebuilds** (a rebuilt image neither spawns a fresh store nor reseeds an existing one — this
  stability is what makes "upgrade in place" reachable). It is **root-owned**.
- **`agent-home-u<uid>g<gid>`** (unchanged) — the per-uid home volume. Holds `~/.elan`'s symlink
  into the store, `~/.local/bin`, caches, etc. Kept on a named volume (not a bind mount) to avoid
  FUSE/VirtioFS open-file limits during Lean builds.
- **`agent-nix-store`** (kept, repurposed name only) — the build-cache volume used *inside the
  `nixos/nix` builder*. Its real job is keeping the toolchain closure warm across shared-volume
  resets and image rebuilds (the shared volume is empty exactly when a seed build runs, so it
  cannot serve as its own cache). Never mounted by the run container.

### Ownership and the read-only-consumer rule

The shared store is **root-owned**, with **exactly one writer**: a transient root-privileged step
driven by the `agent` tooling. Interactive sessions run as the **non-root** agent user and mount
the volume **read-write**, but **ownership**, not a read-only mount, is what makes them read-only
consumers — the session can execute binaries via `PATH` and Nix's own read-path locking under
`/nix/var` still works, but the session cannot mutate the store. An in-session `nix build` *into
the toolchain store is unsupported*, which is consistent with "the flake is the single source of
truth." Because exactly one context (root) ever touches the Nix database, single-user Nix stays
coherent — **no nix daemon is required** (see Concurrency).

Consequence that simplifies seeding: since sessions never write the store, the seed needs **no
chown** — the volume keeps whatever ownership the image baked (root).

### The profile

The toolchain is installed as **generation 1 of the system default profile**
`/nix/var/nix/profiles/default`, which lives in the shared volume. The classic symlink chain
applies:

```
/nix/var/nix/profiles/default -> default-N-link -> <toolchain store path>
```

Everything that used to be a fixed store path on `config.Env` is repointed at profile-stable
paths so the environment survives an upgrade unchanged (see Env repointing). An upgrade builds a
new toolchain closure and **atomically repoints** the `default` symlink to a new generation; a
**new** process in a running session resolves that symlink freshly and sees the new toolchain
with no restart. Long-lived processes keep their already-resolved store paths, which stay present
until their generation is later trimmed.

`/nix/var/nix/profiles` is itself a GC root, so the current generation's whole closure is always
protected simply by being the profile — no separate `nix-store --add-root` for it.

## The flake output: `.#toolchain`

The flake gains **one plain `buildEnv`, `packages.<system>.toolchain`**: today's `env` package
set merged with `lean4Toolchain` (and claude-code) into a single derivation. Both consumers
reference it **by name**:

- `.#image` installs `${toolchain}` as generation 1 of the default profile.
- The upgrade tooling runs `nix build .#toolchain` and swaps the result in.

(Rejected: a derivation that wraps the profile/manifest itself — it conflates the package with
the mutable profile state that `nix-env --profile … --set` already owns.)

**`.#toolchain` is pure — no `getEnv`, no `--impure`.** The toolchain closure has nothing
uid-specific and the default profile is root-owned, so the **same toolchain store path serves
every user** (real dedup across the per-user volumes, which all seed from one closure), and the
upgrade tooling builds it with **no uid env** — a clean root step. Only **`.#image` stays
impure** (`AGENT_UID` / `AGENT_GID`), carrying `passwd`/`group`, the home chown, and the per-user
profile dir.

### GC-rooting boundary — the buildEnv-membership rule

In a mutable, GC'd store only paths inside a rooted generation's closure survive. Therefore:

> **Any store path that a surviving root-filesystem symlink points into must be a member of the
> `.#toolchain` buildEnv.**

Concretely, `nix-ld` is reached solely via the FHS loader symlink and is in no package today, so
it **folds into `.#toolchain`**. `binSh` / `usrBinEnv` targets (bash, coreutils) and
`/usr/bin/time` are already buildEnv members, so they are already safe. (Rejected: a separate
dedicated gcroot for nix-ld — two rooting mechanisms to maintain.)

### Env-var repointing — the whole environment upgrades, not just PATH

`config.Env` is applied only at container creation, and an in-place upgrade never recreates the
container, so PID 1's environment is frozen and every new shell inherits it. `PATH` already
survives because its *string* is a stable profile path that resolves freshly. **Every other var
that embeds a store hash must get the same treatment**, or it pins to the seed generation forever
and breaks once GC trims generation 1:

| Var | Today (seed-pinned) | End-state (profile-stable) |
| --- | --- | --- |
| `PATH` | `${lean4Toolchain}/bin:${env}/bin:…` | `/nix/var/nix/profiles/default/bin:/home/agent/.local/bin:/home/agent/.elan/bin` |
| `SSL_CERT_FILE` | `${cacert}/etc/ssl/…` | `/nix/var/nix/profiles/default/etc/ssl/certs/ca-bundle.crt` |
| `OSFONTDIR` | `${env}/share/fonts` | `/nix/var/nix/profiles/default/share/fonts` |
| `NIX_LD`, `NIX_LD_LIBRARY_PATH`, `LD_LIBRARY_PATH` | fixed store paths | profile-relative `/nix/var/nix/profiles/default/lib` paths |

**Mechanism (design note for implementation):** extend the buildEnv `pathsToLink` to include
`/etc/ssl` and `/lib`, and fold `glibc`/libc into the buildEnv so the real loader is exposed at a
stable `/nix/var/nix/profiles/default/lib/ld-linux-*`. The FHS shim symlink stays put and keeps
reading `NIX_LD`, which now points at that stable path. Repointing `SSL_CERT_FILE` also means
CA-bundle updates take effect on upgrade rather than freezing at seed time.

## The image: bake an operable store, not loose paths

Because the shared volume is seeded by a **byte-copy of the image's baked `/nix`**, the image
must lay down a store that already works for the root tooling. Two changes to
`agent-image/image.nix`:

1. **Switch the builder** from `dockerTools.buildLayeredImage` + `contents` + fixed `PATH` to a
   **Nix-DB-registering builder** (`buildLayeredImageWithNixDb`, the `nixos/nix docker.nix`
   template) so the root tooling can `nix-env --profile … --set` and `nix-collect-garbage`
   against the seed.
2. **Construct generation 1 of the default profile** in `fakeRootCommands`:
   `/nix/var/nix/profiles/default -> default-1-link -> ${toolchain}`, plus the stable rootfs
   symlinks pointing at **profile-stable paths** (see Regression guards).

The **full toolchain closure is still baked** — the seed must be complete, so nothing shrinks in
closure size. What changes is *structure*: a profile + registered Nix DB instead of loose
`contents` + fixed `PATH`. The image stays per-uid (the agent user is baked into the rootfs) and
is tagged `agent:u<uid>g<gid>`.

## Seeding and bootstrap

- **Seed source — copy from the image on first run.** A fresh volume is filled by copying the
  image's baked `/nix` in via Docker's implicit copy-into-empty-volume. No build or `nix copy`
  inside the container. (Rejected: reuse the build-cache volume — mixes two lifecycles, holds
  multi-GB tarballs, is builder-owned/builder-GC'd. Rejected: populate-on-first-run — imperative,
  slow cold start, extra failure surface.)
- **First fill — Docker's copy-into-empty, wrapped in a first-run flock.** Let Docker's own
  copy-on-empty do the byte copy, but serialize the very first run with a lock so two concurrent
  cold starts can't both seed the shared store (high blast radius — every repo shares this one
  volume). Steady-state cost is an existence check already paid for the home volume; the only
  extra cost is a brief wait for the loser of a simultaneous first-run, at most once per volume
  lifetime.
- **First vs later containers — settled by the lock.** First cold start seeds under the lock;
  every later container finds a populated volume and just mounts it. No separate bootstrap path.
- **The image `/nix` layers are a pure one-time seed.** The mounted volume hides the image's
  `/nix` at runtime; the baked closure exists only to seed a fresh volume (and as the reference
  for what a clean store contains). **No overlay / read-only-base scheme** — rejected as fragile
  (overlayfs + split Nix DB + GC across a read-only lower).

One `ensure_seeded(volume)` helper — *build the image if missing, then copy the baked `/nix`
into the empty shared volume under the first-run flock* — is shared by **both `run` and
`update`**, so seeding logic lives in exactly one place and neither command has an ordering
precondition.

## Concurrency

A nix daemon is **not required** for correctness (it exists for cross-UID privilege separation).
Single-user Nix serializes concurrent processes via `db/big-lock`, `gc.lock` + GC-socket temp
roots (build-during-GC is safe), per-path `.lock` files, and SQLite WAL. This extends to multiple
containers on **one host, same UID, local volume** — which is exactly the per-user scoping here.
The real hazard is **NFS** (broken `fcntl` locking), avoided by keeping the volume strictly
**local**. Residual risk to watch at implementation time: the Nix docs don't *explicitly* certify
"N containers, one volume," so keep the volume local and single-user.

## The command map

| Command | Role |
| --- | --- |
| `agent build` | **Rare seed, on-demand only.** Produces the per-user seed image. |
| `agent run` | Seed-if-needed (`ensure_seeded`) + exec the session. |
| `agent update` | **The in-place upgrade** (seed-then-upgrade on a cold machine). |
| `agent clean` | **Gentle, session-respecting GC** by default; `--force` = kill + GC to current. |
| **reset** | Delete the shared volume + reseed — scorched earth / structural changes. |

### `agent build` — rare seed

Triggered **only** when there is no seed to work from: (a) the run path auto-builds when the
per-user image is missing (first ever use), and (b) an explicit **reset**. **Never on a version
bump** — those go through `agent update`. A **structural** change (new env var, new baked rootfs
symlink, flake-output restructure — anything `update` can't carry in place) is handled by a
**reset**, not a special build trigger.

Because routine bumps no longer re-ship an image, `build` reverts to a rare one-shot where the
current ~128s ship is acceptable, and the build path collapses to a linear **build → load →
tag**: the `agent:src-<hash>` **marker short-circuit is removed** (it only accelerated repeated
*identical* builds, now a rare path). The build-cache volume is **kept** (warms the closure
across resets/rebuilds).

> **This design supersedes the in-flight nix2container shipping refactor**
> (`docs/spec-nix2container-build.md`). That refactor exists to make cold *rebuilds* fast; here,
> routine bumps reach the store through in-place `agent update` and never re-ship, so the problem
> it solves largely disappears. Treat nix2container as **likely obsoleted** — noted, **not a
> dependency of this design**. Design against the committed `buildLayeredImage` + `docker load`
> baseline.

### `agent run` — seed-if-needed + exec

Mounts the shared `agent-nix-<username>` volume at `/nix` **read-write**, session as the
**non-root** agent user against the **root-owned** store. `ensure_seeded` runs before container
create. The current **don't-mount-`/nix` special case is dropped**, and the `/nix` mount is
**folded into `RunSpec.fingerprint`** (so mount drift forces a recreate like any other spec
change). On container start the **host** writes the per-session gcroot; on `agent stop` it removes
it (see Upgrade lifecycle).

### `agent update` — the in-place upgrade

`agent update` grows from "bump `flake.lock` + commit" into the **whole upgrade**, end-to-end,
with **no image rebuild and no container restart**:

1. `ensure_seeded(volume)` — on a fresh machine this seeds generation 1 from the baked toolchain
   first (no "run first" error).
2. Bump `flake.lock`, commit.
3. Build `.#toolchain` (**pure, no `--impure`**) in a **transient root container** (the `nixos/nix`
   builder) that mounts the shared volume rw and builds **straight into the shared store** — no
   second store, no `nix copy`. Build-time deps land in the shared store and are reclaimed later
   by `agent clean`.
4. **Atomic swap:** `nix-env --profile /nix/var/nix/profiles/default --set <toolchain-store-path>`
   creates a new generation and repoints `default`. The new generation is rooted just by being the
   profile.

**The swap only adds a generation and deletes nothing, so it is safe to run while sessions are
live.** `agent update` **never deletes** — that keeps it fast and side-effect-light; reclamation
is the separate `agent clean` step.

**Lean-version bump — the one time `update` touches home volumes.** `leanVersion` is a hardcoded,
flake-independent pin (Lean is fetched by URL, not from nixpkgs), so a routine `nix flake update`
**never moves Lean's store path**. Only a deliberate `leanVersion` edit does, and it changes both
the store path and the toolchain *name* (`v4.31.0` → `vNEW`). On such a bump, after swapping the
shared store, `agent update` also rewrites the `~/.elan` pointer in **each per-uid home volume**:
the `toolchains/leanprover--lean4---vNEW` symlink into the new store path and `settings.toml`'s
`default_toolchain`. This keeps Lean coherent and stops the old `~/.elan` symlink dangling once
`agent clean` collects the old Lean path. It is small and fires only on a Lean bump. (Rejected:
rule a Lean bump a manual "reset" event — leaks a step whose omission re-creates the broken-symlink
footgun this effort exists to remove.)

### `agent clean` — three tiers of reclaim

Reclamation must respect **other repos' live sessions**, which the per-session gcroots make safe.

- **`agent clean` (gentle default):** reap session roots for **not-running** containers, remove
  not-running run containers (to unpin their images), leave **running** sessions and their images
  untouched, `nix-collect-garbage` the shared store **keeping the current generation + every
  generation ≥ the oldest live session's**, and reclaim orphaned image tarballs from the
  build-cache volume.
- **`agent clean --force` (scorched earth):** force-remove **all** run containers (killing live
  sessions) and GC the shared store down to the **current** generation.
- **reset (deepest):** delete the shared volume + reseed from the image. Owns the "structural
  change" and "corrupt store" cases.

## Upgrade lifecycle: in-use protection and GC across containers

The tricky part of a shared store upgraded in place is protecting a toolchain that a **sibling
container** is still using. Each run container has its own **PID namespace**, so Nix's automatic
runtime-root scan (`/proc/*/maps`, open FDs, env) **cannot see sibling containers** — the
single-host protection that normally shields an in-use toolchain does not reach across the
containers sharing this volume. The design closes this with host-written per-session roots:

- **Per-session gcroots, written by the host.** The **host CLI** — the only actor that sees all
  containers — writes `/nix/var/nix/gcroots/session-<container>` when it brings a container up,
  recording the **generation number** that container started on. The store is root-owned and
  sessions are non-root, so the host creates the root via the transient-root mechanism, not the
  session. Removed on `agent stop`.
- **Crash safety — reap not-running roots.** Before any GC, the host walks
  `/nix/var/nix/gcroots/session-*` and deletes each whose container is **not currently running**
  (`docker ps`). Keying on *not-running* (rather than *doesn't exist*) unifies clean-stop,
  crash-exit, and removal — a stopped container has no live processes to protect, and a crash
  cannot leak a permanent root.
- **Retention across two upgrades.** A long job can span *two* upgrades and run on a generation
  that is neither its session's start-generation nor the current one. So `agent clean` keeps every
  generation **with number ≥ the minimum recorded generation over all live session roots** (if no
  session is live, only the current generation). No generation history is kept for its own sake.
- **Rollback is a flake operation, not a stored generation.** Revert `flake.lock` and re-run
  `agent update`; the old toolchain rebuilds from cache and swaps back. This matches "the flake is
  the single source of truth."

## Regression guards

The original breakage this container was built to avoid: **patched ELFs in mutable `~/.elan`
depending on store paths that GC was free to collect.** A mutable, GC'd `/nix` reintroduces that
exact risk class, so two guards are load-bearing.

### 1. Patched-ELF guard — everything patched stays in the store, in a rooted generation

- Lean stays **store-resident inside `.#toolchain`**, so its store path sits in the current
  generation's closure and GC cannot collect it — the same protection that put Lean in the store
  originally, now via the profile instead of image `contents`.
- With per-session roots protecting all in-use generations and `agent clean` keeping current +
  everything ≥ oldest-live, **no patched binary loses its store deps**: the current toolchain is
  rooted by the profile, live generations are pinned by session roots, and nothing patched lives
  outside the store.

### 2. Broken-symlink guard — every surviving symlink targets a profile-stable path

The env-var repointing rule generalizes from *variables* to *symlinks*:

> **Every baked root-filesystem symlink into the store must target the profile-stable path, not a
> seed-generation store path.**

Concretely: `/usr/bin/time` → `/nix/var/nix/profiles/default/bin/time`; the nix-ld FHS loader
shim `${fhsDynamicLinker}` → the profile-relative `nix-ld` (add its `libexec` to the buildEnv
`pathsToLink`). Neither dangles after an upgrade + clean moves the underlying package. `time` and
`nix-ld` are already buildEnv members (GC-safe); this adds *upgrade*-safety on top.

`~/.elan` is the **deliberate exception** — re-pointed on a Lean bump (above), because elan needs
a *versioned* toolchain dir, not a single stable path.

## Out of scope

- **Mutable-pet workflow** (arbitrary in-container `nix-env` / `nix profile install` as the model).
- **Per-container `/nix` volumes** — one shared volume per user.
- **Implementing the rework** — this document is design-only; the code change to `agent` /
  `agent-image/image.nix` / `agent-image/flake.nix` is a separate follow-up effort.

## Decision trail

The reasoning behind each decision, with rejected alternatives, lives in the wayfinder map and its
tickets under `.scratch/persistent-nix/` (local, uncommitted): best-practices research
(`research/nix-in-container.md`), seeding & bootstrap, toolchain-as-profile + PATH, flake toolchain
output, in-container upgrade & GC lifecycle, and CLI & build-pipeline impact.
