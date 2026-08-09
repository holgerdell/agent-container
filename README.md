# agent-container

Yet another script for running a coding agent in an isolated container. Here's why I built it anyway:

- 🛠️ Agents need broad shell access
- 🔓 Built-in sandboxes exist, but I don't trust them: Their configuration is scattered across many settings files
- ⏸️ Permission prompts are annoying

**What `agent-container` does instead:**

- 🐳 Runs the agent inside an isolated container
- 📁 Only the current workspace is mounted into the container
- 🔒 Selected config files (`~/.gitconfig`, etc.) can optionally be mounted read-only
- 🗂️ The container's home directory is persisted across runs in an isolated, per-user named volume
- 📦 The image is plain Debian Sid plus the APT packages you pick, so it is easy to understand and change

## Requirements

- [Docker](https://docs.docker.com/get-docker/)
- Python 3

## Install

Clone the repo and symlink `agent` into your PATH:

```sh
git clone https://github.com/holgerdell/agent-container
ln -s "agent-container/agent" ~/.local/bin/agent
```

Then run `agent setup` to configure workspace options and pick your packages. The image is built automatically on first run.

## Usage

```sh
agent              # run your default coding agent in the current directory
agent shell        # open bash instead
agent setup        # configure workspace, packages, agent, read-only host mounts
agent build        # rebuild the Debian image, then reconcile extra tools
agent update       # install/update/remove extra tools in the home volume
agent stop         # remove this repo's persistent container (--all for every repo)
agent doctor       # report local environment state
agent clean        # remove stopped containers and unused agent images
agent help         # show help
```

Extra arguments after `--` are forwarded to the coding agent (or `bash` with `shell`):

```sh
agent -- --print "hello"
agent shell -- -c 'echo hi'
```

## Two kinds of packages

The environment is split into an image half and a home-volume half, and each is changed by its own command.

| | **Debian packages** | **Extra tools** |
| --- | --- | --- |
| Where they live | the image, `agent:<username>` | the home volume, `agent-home-<username>` |
| Installed by | `apt-get` during `agent build` | each tool's own upstream installer |
| Changed by | `agent build` | `agent update` (and `agent build`) |
| Configured as | `apt_packages` in settings | `extra_packages` in settings |

`agent build` always pulls a fresh `debian:sid-slim`. When Debian has republished that base image, or your package selection changed, the packages are reinstalled from the current archive; otherwise cached layers are reused and an unchanged rebuild finishes in seconds. `agent build --refresh` skips the cache entirely and reinstalls everything, whatever the base image says. `agent update` never touches APT — system upgrades happen only through `agent build`.

`agent doctor` reports the image's age from when it was actually created, so a cached rebuild does not reset it.

Running sessions survive both commands. A stopped container whose image or configuration has changed is recreated the next time you launch that repository.

### Debian packages

`agent setup` presents them as logical groups (LaTeX, development tools, Python, editors, …) and toggles whole groups at a time. Settings store the resulting flat list of package names, so you can also hand-edit `apt_packages` to select individual packages or add names outside the built-in catalog — setup preserves them, and shows a partly-selected group as `[~]`.

Package names are not validated up front: an unknown or renamed package fails the `apt-get install` step, which is the authoritative answer.

If the image was built from a different selection than your settings currently describe, `agent` warns on launch and keeps going. Run `agent build` when you want to apply it.

### Extra tools

These come from a small curated registry baked into the script — `uv`, `bun`, `ruff`, `ty`, `typos`, `typst`, `tectonic`, `claude-code`, `codex`, `mistral-vibe`, `opencode`, and `ollama`. Each entry defines its own install, update, and removal routine using the tool's official upstream mechanism, plus the exact paths under `/home/agent` it owns.

They are installed into the persistent home volume, which is shared by every repository, so you install and update them once per user. Deselecting a tool removes only the files that tool's registry entry declares; anything you installed yourself is never touched.

`agent update` runs every selected tool even if an earlier one fails, prints a per-package summary, and exits nonzero if anything failed. After each install or update it checks that the tool's executable actually resolves on `PATH`, so an installer that exits 0 without producing a working binary is reported as a failure rather than recorded as installed. Failed operations stay pending, so re-running converges.

The installers themselves need a few ordinary Debian packages — `curl`, `ca-certificates`, and the archivers (`xz-utils`, `zstd`, `tar`) — plus `npm` for `codex`. All of these are in the default selection; if you trim the package groups right down, the affected installer fails with the tool's own error and you can add the package back and rerun.

The coding agents among these (`claude-code`, `codex`, `mistral-vibe`, `opencode`) also carry launch metadata; those are the choices offered for the default agent. Your default-agent choice is independent of package selection — if you pick one whose package isn't installed, the launch simply fails.

## The persistent home volume

`agent-home-<username>` is mounted at `/home/agent` in every session container and every updater container, and it is writable by the interactive agent user. It holds your native tool installations, tool state, caches, credentials, and home-directory configuration.

No `agent` command ever removes, migrates, or reclaims it — `agent clean` only removes stopped containers and unused `agent:*` images. If you genuinely want to delete it, use Docker directly:

```sh
docker volume rm agent-home-$(whoami)
```

## SSH agent forwarding

The agent script automatically forwards your SSH agent into the container — no setup required. Private keys never enter the container; the agent handles all signing and authentication operations on the host.

- **macOS + Docker Desktop**: Docker Desktop exposes the macOS SSH agent at `/run/host-services/ssh-auth.sock` inside every container. The script sets `SSH_AUTH_SOCK` to that path automatically.
- **Linux**: The script bind-mounts `$SSH_AUTH_SOCK` if the socket exists.

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

## Settings

Settings live at `~/.config/agent-container/settings.json`; re-run `agent setup` any time to change them. For custom read-only mounts beyond the built-in shortcuts, add objects of the form `{"host": "~/path", "container": "/path"}` to `shared_host_paths`.

## Environment variables

| Variable      | Effect                                                                                     |
| ------------- | ------------------------------------------------------------------------------------------ |
| `AGENT_IMAGE` | Override the container image tag — skip the build step and use an existing image directly. |
| `NO_COLOR`    | Disable color output ([standard spec](https://no-color.org/)).                             |
| `FORCE_COLOR` | Force color output even when stdout is not a TTY.                                          |
