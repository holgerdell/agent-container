# SPEC

All specs above the fold have a unique ID (S1, S2, ...). IDs are assigned monotonically. When a spec is demoted to Ideas, its ID is freed and may be reused by the next new spec; an ID is never active on two specs at once.

## Ideas

_Ideas below are just ideas, not yet spec:_

- Per-project isolated home dir vs. global shared home (current default) — open question is the isolation scope (full `/home/agent`, only `~/.claude/`, allowlist, or split with shared tool caches)
- Read-only project mount option (review/audit tasks)
- Network isolation toggle (air-gap mode)
- Resource limits: CPU and memory caps via `--cpus`/`--memory`
- Pre/post session hooks: scripts that run before/after the container starts
- SSH key forwarding (opt-in)
- GPG key forwarding (opt-in)
- `.env` file passthrough (explicit allowlist of vars)
- Version-pin Claude Code inside the image (lock-pinned by `agent-image/flake.lock`; bump with `nix flake update`)
- Persistent bash history: bind-mount `$XDG_STATE_HOME/agent-container/bash_history` to `/home/agent/.bash_history` so shell history survives `--rm`
- `agent doctor` subcommand: diagnose podman machine state, memory, volume health, host config visibility
- Per-project image overlay: if `.agent/extra.nix` exists in project root, rebuild with those extra packages layered onto the base set
- Pull prebuilt image fallback: optional `AGENT_IMAGE_PULL=ghcr.io/...` to skip first-run Nix build
- Container name per project: name containers based on `$PWD` hash so `podman ps` / `docker ps` is readable and concurrent sessions are distinguishable
- Support multiple agent backends (Claude Code default; opencode, etc. as opt-in modules)
- Optionally support connection pass-through to IDE on host
