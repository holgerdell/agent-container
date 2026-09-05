# agent-container

A small wrapper for running coding agents inside a persistent [Lima](https://lima-vm.io/) virtual machine.

The VM uses Ubuntu 26.04, keeps its home directory on a separate Lima disk, and installs development tools on first start. Lima runs in plain mode, without host-directory mounts, port forwarding, or containerd.

## Requirements

- Lima 2.0 or newer
- macOS or Linux supported by Lima

## Setup

Clone the repository and optionally put `agent` on your `PATH`:

```sh
git clone https://github.com/holgerdell/agent-container
ln -s "$PWD/agent-container/agent" ~/.local/bin/agent
cd agent-container
```

Create and provision the VM:

```sh
agent init
```

This creates a 100 GiB persistent disk named `agent-sandbox-home`, creates the `agent-sandbox` VM from `lima.yaml`, and starts it. Set `AGENT_LIMA_DISK_SIZE` before initialization to choose another disk size.

The first start upgrades the base system, installs the packages from `install-system`, installs the user tools from `install-tools`, and copies the files under `guest-root` into the VM. This can take several minutes.

## Usage

```sh
agent                         # open a shell in the guest copy of the current repo
agent shell                   # open a shell in the VM (in the guest repo)
agent shell -- COMMAND ARG... # run a command in the VM (in the guest repo)
agent install                 # copy the guest-root files into the running VM
agent update                  # update system packages and user tools
agent help                    # show wrapper help
```

If the configured VM does not exist, shell and update commands stop with instructions to run `agent init`.

Commands not handled by the wrapper are passed to `limactl` unchanged:

```sh
agent list
agent stop agent-sandbox
agent start agent-sandbox
```

`agent install` copies the files under `guest-root` into the running VM, without recreating it. Use it after editing files such as `guest-root/etc/bash.bashrc`; the change applies to shells started afterwards.

`agent update` upgrades APT packages in the running VM and then updates the user-installed tools. It does not restart the VM. Only one update may run for an instance at a time; another invocation exits immediately.

## Configuration

| Variable | Purpose | Default |
| --- | --- | --- |
| `AGENT_LIMA_INSTANCE` | Lima instance used by the wrapper | `agent-sandbox` |
| `AGENT_LIMA_DISK_SIZE` | Persistent disk size used by `agent init` | `100GiB` |

```sh
AGENT_LIMA_INSTANCE=my-agent-vm agent shell
```

The persistent disk name and its mount path are fixed in `lima.yaml`. If you rename the disk, update both its `additionalDisks` entry and `/mnt/lima-…` paths, as well as the `disk` value in `agent`.

## Provisioning

- `lima.yaml` defines the VM, persistent home disk, guest files, and provisioning order.
- `install-system` upgrades Ubuntu and installs system packages as root.
- `install-tools` installs and updates tools in the persistent user home.
- `guest-root` contains files copied into the guest filesystem.

User tools include uv, Ruff, ty, typos, Mistral Vibe, Bun, Codex, Typst, Tectonic, Ollama, Claude Code, and OpenCode.

Run `agent update` to apply installer changes to an existing VM, and `agent install` to apply `guest-root` changes. Changes to `lima.yaml` generally apply when a new instance is created; the separately named home disk can be attached again.

## Development

```sh
just lint    # run ShellCheck with all optional checks
just format  # format shell scripts with shfmt
just fix     # apply available ShellCheck fixes, then format
```

## License

See [LICENSE](LICENSE).
