# agent-container

Yet another script for running a coding agent in an isolated container. Here's why I built it anyway:

- 🛠️ Agents need broad shell access
- 🔓 Built-in sandboxes exist, but I don't trust them: Their configuration is scattered across many settings files
- ⏸️ Permission prompts are annoying

**What `agent-container` does instead:**

- 🐳 Runs the agent inside an isolated container
- 📁 Only the current workspace is mounted into the container
- 🔒 Selected config files (`~/.gitconfig`, `~/.config/gh/`, etc.) can optionally be mounted read-only
- 🗂️ The container's home directory is persisted in an isolated location (`~/.local/share/agent-container/home/`)
- ❄️ The container uses Nix instead of a Dockerfile, which makes it easy to customize to your needs

## Requirements

- [Docker](https://docs.docker.com/get-docker/) or [Podman](https://podman.io/)
- Python 3

## Install

Clone the repo and symlink `agent` into your PATH:

```sh
git clone https://github.com/holgerdell/agent-container
ln -s "agent-container/agent" ~/.local/bin/agent
```

Then run `agent setup` to configure the container engine and workspace options. The image is built automatically on first run — no host Nix required.

## Usage

```sh
agent              # run claude in the current directory
agent shell        # open bash instead
agent setup        # configure container engine, workspace sharing, read-only host mounts
agent build        # build the image and exit
agent check        # validate Nix expression without building
agent doctor       # run environment health checks
agent clean        # remove container image and Nix store cache
agent help         # show help
```

Extra arguments after `--` are forwarded to `claude` (or `bash` with `shell`):

```sh
agent -- --print "hello"
agent shell -- -c 'echo hi'
```

`agent setup` configures the container engine, workspace root (current directory or git repo root), and read-only host mounts (`~/.gitconfig`, `~/.config/gh/`, `~/.claude/`, etc.). Settings are stored at `~/.config/agent-container/settings.json`; re-run any time to change them. For custom mounts beyond the built-in shortcuts, add objects of the form `{"host": "~/path", "container": "/path"}` to `shared_host_paths` in that file.

The agent's home directory (`~/.local/share/agent-container/home/`) and Nix store (per-image volume) are persisted across runs. Old Nix volumes accumulate until `agent clean`.

## MacOS and Podman

I had issues getting the container to work reliably with Podman on macOS. Docker Desktop seems to be more stable here.

The default Podman machine has 2 GB RAM, which is too little for memory-intensive build tasks. Increase it:

```sh
podman machine stop
podman machine set --memory 8192
podman machine start
```

The default Podman machine also caps open file descriptors at 1024 (a systemd default, kept for `select(2)` safety). Heavy parallel builds — e.g. `lake build` for Lean projects — can blow past it, surfacing as transient `Too many open files` warnings. Run `agent doctor` to check and get a copy-pasteable fix.

## SSH agent forwarding

The agent script automatically forwards your SSH agent into the container — no setup required. Private keys never enter the container; the agent handles all signing and authentication operations on the host.

- **macOS + Docker Desktop**: Docker Desktop exposes the macOS SSH agent at `/run/host-services/ssh-auth.sock` inside every container. The script sets `SSH_AUTH_SOCK` to that path automatically.
- **Linux**: The script bind-mounts `$SSH_AUTH_SOCK` if the socket exists.
- **macOS + Podman**: Not supported (the Podman Machine VM sits between host and container).

This enables SSH-signed git commits and SSH-authenticated git operations inside the container.

## Git credentials (HTTPS)

To let the in-container agent push and pull over HTTPS without storing credentials in a file:

**One-time host setup:**

```sh
git config --global credential.helper 'cache --timeout=0'
# Populate the cache by doing any authenticated git operation:
git -C /path/to/repo fetch
```

`--timeout=0` keeps the cache alive until the daemon is killed or the machine reboots. The socket is created at `~/.cache/git/credential/socket` (respects `XDG_CACHE_HOME`).

When you run `agent`, the container automatically bind-mounts `~/.cache/git/credential/` so the in-container git connects to the host-side cache daemon over the socket. The container image is pre-configured with `credential.helper = cache`.

To refresh credentials after a reboot, run a `git fetch` (or any authenticated operation) on the host once before starting the agent.

## Environment variables

| Variable      | Effect                                                                                     |
| ------------- | ------------------------------------------------------------------------------------------ |
| `AGENT_IMAGE` | Override the container image tag — skip the build step and use an existing image directly. |
| `NO_COLOR`    | Disable color output ([standard spec](https://no-color.org/)).                             |
| `FORCE_COLOR` | Force color output even when stdout is not a TTY.                                          |
