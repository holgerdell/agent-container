# Docker container issues — Lean builds (macOS host)

Diagnosis + fixes for two problems hit during heavy multi-agent Lean work in this
container (Claude Code running inside Docker Desktop on a macOS host), 2026-06-02.

Two separate issues that compound:
1. **`lake build` → "Too many open files"** (fd exhaustion) — root cause: FUSE bind mount + concurrent builds.
2. **`<defunct>` (zombie) `lean` processes accumulate** — root cause: Claude is PID 1 and doesn't reap orphans.

---

## Issue 1 — "Too many open files" during `lake build`

### Symptom
```
error: .../Cases.lean:1:0: failed to open file
  '/home/agent/.elan/toolchains/.../<Module>.olean': Too many open files
error: Lean exited with code 1 ; build failed
```
Transient: fails during heavy concurrent builds, succeeds when the system is quiet.

### Evidence gathered
The container's own limits are NOT the bottleneck:
- `ulimit -Sn` / `-Hn` = **1048576** (soft & hard).
- `/proc/sys/vm/max_map_count` = **262144**; `/proc/sys/fs/inotify/max_user_instances` = **8192** (only 1 in use).
- Total fds open across ALL processes at idle = **118**. No persistent fd leak.

The real signal — everything the build reads is on a **FUSE** mount:
```
stat -f -c '%T' .lake    → fuse        (project build artifacts / oleans)
stat -f -c '%T' ~/.elan  → fuse        (Lean toolchain + Mathlib oleans, ~5000 files)
/proc/sys/fs/file-nr     → 65440 0 1016828   (FUSE holding ~65k file refs)
```
Live `lean` workers each hold ~**16,339 mmap regions** (`wc -l /proc/<pid>/maps`) —
i.e. thousands of mmap'd oleans per worker. Default lake parallelism = `nproc` = **14**
workers. During the incident ~**10 background agents** each ran `lake build` +
a `lean-lsp` server concurrently.

### Root cause
The whole Lean read path (`~/.elan` toolchain + Mathlib + project `.lake`) is on the
**macOS → container Docker file-sharing FUSE layer** (gRPC-FUSE / VirtioFS proxying
from the host). A `lake build` mmaps thousands of small oleans through FUSE. With
14 workers/build × many concurrent agent builds, the number of simultaneously-open
files exceeds the **host-side / FUSE-daemon fd ceiling** (macOS `kern.maxfilesperproc`,
typically ~10k, or the file-sharing daemon's own limit) — which is FAR below the
container's 1M `ulimit`. The container surfaces the host's `EMFILE` as
"Too many open files." It clears when builds stop (hence 118 fds at idle).

Why Mathlib makes it bad: ~5000 tiny `.olean` files, each mmap'd, every build —
exactly the workload FUSE bind mounts are worst at.

**VirtioFS is not enough.** Even with Docker Desktop file sharing = VirtioFS (not
gRPC-FUSE), the host-side daemon caps simultaneous open files at ~**14 K**. A single
`import Mathlib` opens ~**25 K+** files simultaneously — it blows the cap on its own,
before any concurrent agents. Switching to VirtioFS helps but does not fix the problem.

### Fixes (highest impact first)
1. **Replace `import Mathlib` with specific imports.**
   The only fix that works regardless of fs layer. `import Mathlib` alone opens ~25 K
   files simultaneously — above any FUSE/VirtioFS daemon cap. Use the narrowest imports
   that cover the needed lemmas (e.g. `import Mathlib.Algebra.Group.Basic`). Reduces
   simultaneous opens by 10–100×.
2. **Move build artifacts + toolchain OFF the FUSE mount onto native container fs.**
   Keep only `.lean` source on the bind mount (for editing from macOS); put everything
   read thousands of times on native ext4/overlay:
   - `~/.elan` (toolchain + Mathlib) → a native path (e.g. relocate to `/opt/elan`,
     set `ELAN_HOME=/opt/elan`) if `/home/agent` is host-mounted.
   - `.lake` build dir + the Mathlib cache → a Docker **named volume** mounted at
     `<repo>/.lake`. Native fs mmaps thousands of oleans with no daemon ceiling.
3. **Cap parallelism + serialize builds.**
   - `lake build -j 4` (or lower) instead of default `nproc`=14 → fewer concurrent
     olean opens per build.
   - Do NOT run multiple `lake build` / `lean-lsp` agents at once — the proximate
     trigger. One Lean build/agent at a time.
4. **Raise host limits** (buys headroom, not a real fix for `import Mathlib`).
   - macOS host: `sudo sysctl -w kern.maxfilesperproc=524288 kern.maxfiles=1048576`
     (persist via a launchd plist).
   - Docker Desktop → Settings → General: file sharing = **VirtioFS** (better than
     gRPC-FUSE but still caps at ~14 K); give the VM more CPU/RAM.

### Immediate unblock (no infra change)
```
pkill -9 lean lake ; lake build -j 2
```

---

## Issue 2 — `<defunct>` (zombie) `lean` processes accumulate

### Symptom
`ps` shows dozens of `lean <defunct>` entries (32 observed), all with `PPID = 1`,
lingering for hours.

### Evidence
```
ps -p 1 -o args  →  claude --dangerously-skip-permissions --model opus
ps -eo ppid,stat | awk '$2~/Z/'  →  32 zombies, all PPID 1
```

### Root cause
**Claude is PID 1 in this container, and it is not an init/reaper.** When a background
agent dies, its spawned `lean`/`lake` children are orphaned → reparented to PID 1
(claude) → claude never calls `wait()` on them → they stay `<defunct>` forever.
On a real host this never happens: PID 1 is `systemd`/`launchd`-init (or your shell),
which reaps orphans automatically. A container only gets that behavior if you give it
a real init as PID 1.

### Fixes — give the container an init/reaper as PID 1
Any one of these makes a tiny reaper PID 1 that runs claude as a child and `wait()`s
on orphans:
1. **Simplest — `docker run --init`**: Docker injects `tini` as PID 1 (reaps zombies,
   forwards signals). One flag.
2. **docker-compose**: add `init: true` to the service.
3. **Dockerfile**: `ENTRYPOINT ["tini", "--", "<claude launch cmd>"]`
   (or `["dumb-init", "--", ...]`; `apt-get install tini` / `apk add tini` first).

### Clearing the EXISTING zombies
You can't `kill`/`pkill` a zombie — it's already dead; only its parent's `wait()`
removes it, and PID 1 (claude) won't. Options:
- **Restart the container** (PID 1 exits → all reaped on teardown). Cleanest before a
  big build.
- Otherwise nothing, short of claude installing a `SIGCHLD` reaper (its code, not yours).

### Note on impact
Zombies are **cheap** — a PID-table slot + a tiny `task_struct`, **no fds, no memory,
no oleans held**. They are a *symptom* of agent churn, NOT the cause of Issue 1.
They compound it only indirectly: if a build is mid-flight when its agent dies, those
orphaned workers keep their FUSE opens until the kernel tears them down — `--init`
makes that teardown prompt.

---

## Recommended combined setup
- **Use specific Lean imports** — not `import Mathlib`. Biggest single lever; nothing
  else fully compensates for 25 K+ simultaneous opens.
- `docker run --init …` (or compose `init: true`) — kills zombie accumulation.
- Toolchain (`~/.elan`/`ELAN_HOME`) + `.lake` on **native fs / named volumes**, only
  `.lean` source on the bind mount — kills fd exhaustion for non-Mathlib builds.
- Docker Desktop file sharing = **VirtioFS**; raise host `kern.maxfilesperproc`.
- `lake build -j 4`; one Lean build/agent at a time.
- Restart the container once to clear existing zombies.
