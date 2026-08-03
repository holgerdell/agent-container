#!/usr/bin/env python3
"""A controlled stand-in for the `docker` CLI, used as the engine boundary in
command-level tests.

It is driven by a JSON scenario file (`FAKE_DOCKER_STATE`) that it also *mutates*,
so a test can assert on durable state — which images exist, which containers were
created or removed, what the extras state file ends up containing — rather than on
Docker flag order. Every invocation is appended to `FAKE_DOCKER_LOG` as one JSON
line, so tests can also assert which operations happened.

Scenario keys (all optional):
  images        {tag: {"id": ..., "apt_fingerprint": ..., "created_at": ...}}
  containers    {name: {"running": bool, "image": id, "fingerprint": ..., "mounts": [[src, dst], ...]}}
  volumes       [name, ...]
  extras_state  JSON text of the in-volume managed-extras state file
  fail          {"build": rc, "scripts": [substring, ...], "commands": [name, ...]}
  volume_sizes  {name: "1.5GB"}
  versions      [{pkg: "--version output"}, ...] — one entry per version probe,
                the last one repeating for every further probe
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import sys
import time
from pathlib import Path

STATE_PATH = Path(os.environ["FAKE_DOCKER_STATE"])
LOG_PATH = Path(os.environ["FAKE_DOCKER_LOG"])
EXTRAS_STATE_PATH = "/home/agent/.local/state/agent-container/extras.json"
VERSION_PROBE_MARKER = "# agent: report extra-package versions"
OP_MARK = "@@AGENT-OP@@"
VER_MARK = "@@AGENT-VER@@"


def load() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_PATH)


@contextlib.contextmanager
def state_lock():
    """Real Docker serializes its own state; `agent doctor` fans out concurrent
    invocations, so the scenario file needs the same guarantee or a read can race
    a write and lose the scenario."""
    with STATE_PATH.with_suffix(".lock").open("w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def log(entry: dict) -> None:
    entry["at"] = time.time()
    with LOG_PATH.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def flag_value(argv: list[str], flag: str) -> str | None:
    for i, a in enumerate(argv):
        if a == flag and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(flag + "="):
            return a.split("=", 1)[1]
    return None


def all_flag_values(argv: list[str], flag: str) -> list[str]:
    out = []
    for i, a in enumerate(argv):
        if a == flag and i + 1 < len(argv):
            out.append(argv[i + 1])
        elif a.startswith(flag + "="):
            out.append(a.split("=", 1)[1])
    return out


def render(fmt: str, obj: dict) -> str:
    """Resolve the handful of Go template fields the script actually asks for."""
    out = fmt
    out = out.replace("{{range .Mounts}}{{.Source}}={{.Destination}};{{end}}", obj.get("mounts_s", ""))
    out = re.sub(r'\{\{index \.Config\.Labels "([^"]+)"\}\}', lambda m: obj.get("labels", {}).get(m.group(1), ""), out)
    replacements = {
        "{{.Id}}": obj.get("id", ""),
        "{{.ID}}": obj.get("short_id", ""),
        "{{.State.Running}}": "true" if obj.get("running") else "false",
        "{{.Image}}": obj.get("image", ""),
        "{{.Names}}": obj.get("name", ""),
        "{{.Created}}": obj.get("created_at", ""),
        "{{.Repository}}:{{.Tag}}": obj.get("tag", ""),
        "{{.Server.Version}}": "29.0.0-fake",
    }
    for token, value in replacements.items():
        out = out.replace(token, value)
    if "{{" in out:
        # Real Docker would resolve it; leaving it verbatim would let a typo in a
        # `--format` string pass as an ordinary empty/garbage value.
        raise AssertionError(f"unhandled Go template field in fake docker format: {fmt!r}")
    return out


def image_of(state: dict, ref: str) -> tuple[str, dict] | None:
    images = state.setdefault("images", {})
    if ref in images:
        return ref, images[ref]
    for tag, data in images.items():
        if data.get("id", "").removeprefix("sha256:").startswith(ref.removeprefix("sha256:")):
            return tag, data
    return None


def positional(argv: list[str], known_flags_with_values: set[str]) -> list[str]:
    out: list[str] = []
    skip = False
    for a in argv:
        if skip:
            skip = False
            continue
        if a in known_flags_with_values:
            skip = True
            continue
        if a.startswith("-"):
            continue
        out.append(a)
    return out


VALUE_FLAGS = {"--format", "--filter", "--tag", "-t", "--build-arg", "--name", "--user", "-v", "-e", "--label", "--ulimit", "-w"}


def cmd_build(state: dict, argv: list[str]) -> int:
    context = Path(argv[-1])
    build_args = dict(kv.split("=", 1) for kv in all_flag_values(argv, "--build-arg") if "=" in kv)
    packages = (context / "apt-packages.txt").read_text().split()
    metadata = json.loads((context / "image-metadata.json").read_text())
    log(
        {
            "cmd": "build",
            "pull": "--pull" in argv,
            "no_cache": "--no-cache" in argv,
            "tag": flag_value(argv, "--tag"),
            "build_args": build_args,
            "packages": packages,
            "metadata": metadata,
            "dockerfile_exists": (context / "Dockerfile").is_file(),
            "profile_script": (context / "root/etc/profile.d/agent.sh").read_text()
            if (context / "root/etc/profile.d/agent.sh").is_file()
            else None,
        }
    )
    fail = state.get("fail", {})
    if "build" in fail:
        print("E: Unable to locate package bogus-package", file=sys.stderr)
        return int(fail["build"])
    tag = flag_value(argv, "--tag") or "agent:unknown"
    counter = state.get("build_counter", 0) + 1
    state["build_counter"] = counter
    state.setdefault("images", {})[tag] = {
        "id": f"sha256:{counter:064x}",
        "labels": {"agent.apt.fingerprint": build_args.get("AGENT_APT_FINGERPRINT", "")},
        "created_at": "2026-07-27T10:00:00Z",
    }
    return 0


def cmd_run(state: dict, argv: list[str]) -> int:
    mounts = all_flag_values(argv, "-v")
    envs = all_flag_values(argv, "-e")
    detached = "-d" in argv
    script = argv[-1] if len(argv) >= 2 and argv[-2] == "-lc" else None
    entry = {
        "cmd": "run",
        "detached": detached,
        "mounts": mounts,
        "env": envs,
        "user": flag_value(argv, "--user"),
        "name": flag_value(argv, "--name"),
        "pull": flag_value(argv, "--pull") or ("--pull=never" if "--pull=never" in argv else None),
        "shell": argv[-3:-1] if script is not None else None,
        "script": script,
        "network": "none" if "--network=none" in argv else "default",
    }
    log(entry)

    if detached:
        name = flag_value(argv, "--name") or ""
        image_ref = argv[argv.index("sleep") - 1] if "sleep" in argv else ""
        found = image_of(state, image_ref)
        labels = dict(kv.split("=", 1) for kv in all_flag_values(argv, "--label") if "=" in kv)
        state.setdefault("containers", {})[name] = {
            "running": True,
            "image": found[1]["id"] if found else image_ref,
            "labels": labels,
            "mounts": [m.split(":")[:2] for m in mounts],
        }
        print(name)
        return 0

    if script is None:
        return 0

    # The two state-file operations are modelled directly, so reconciliation's
    # durable effect is observable without executing any real shell.
    if script.strip().startswith(f'cat "{EXTRAS_STATE_PATH}"'):
        sys.stdout.write(state.get("extras_state", ""))
        return 0
    if f'mv "{EXTRAS_STATE_PATH}.tmp"' in script:
        state["extras_state"] = sys.stdin.read()
        return 0

    # Version reporting: each probe consumes the next scenario entry, so a test
    # can make an update move a version by supplying a before and an after.
    if script.lstrip().startswith(VERSION_PROBE_MARKER):
        reports = state.get("versions") or [{}]
        for pkg, raw in reports[0].items():
            print(f"{pkg}\t{raw}")
        if len(reports) > 1:
            state["versions"] = reports[1:]
        return 0

    if OP_MARK in script:
        return cmd_reconcile(state, script)

    for needle in state.get("fail", {}).get("scripts", []):
        if needle in script:
            print(f"fake installer failure: {needle}", file=sys.stderr)
            return 1
    return 0


def _next_report(state: dict) -> dict:
    reports = state.get("versions") or [{}]
    if len(reports) > 1:
        state["versions"] = reports[1:]
    return reports[0]


def cmd_reconcile(state: dict, script: str) -> int:
    """Simulate `build_reconcile_script`: one container run covering every
    planned operation. Parses the generated script back into its before-probe
    ids and per-operation chunks — the same shape `agent.py` built it in — and
    fabricates the tagged output a real container would have printed. Always
    exits 0: a real combined script never fails as a whole, since each
    operation's own failure is captured into its own `@@AGENT-OP@@` line, not
    the container's exit status."""
    before_part, _, rest = script.partition("# op:")
    if "for spec in" in before_part:
        for pkg, raw in _next_report(state).items():
            if f'"{pkg}:' in before_part:
                print(f"{VER_MARK}\tbefore\t{pkg}\t{raw}")

    for chunk in re.split(r"(?=^# op: )", "# op:" + rest, flags=re.M):
        header = re.match(r"# op: (\S+) (\S+)", chunk)
        if not header:
            continue
        pkg, action = header.group(1), header.group(2)
        failing = next((n for n in state.get("fail", {}).get("scripts", []) if n in chunk), None)
        op_rc = 1 if failing else 0
        print(f"{OP_MARK}\t{pkg}\t{action}\t{op_rc}")
        if failing:
            print(f"fake installer failure: {failing}")
        if op_rc == 0 and action != "remove" and f"{VER_MARK}\\tafter" in chunk:
            for pkg2, raw in _next_report(state).items():
                print(f"{VER_MARK}\tafter\t{pkg2}\t{raw}")
    return 0


def cmd_ps(state: dict, argv: list[str]) -> int:
    containers = state.get("containers", {})
    running_only = "-a" not in argv and "-aq" not in argv
    name_filter = ""
    for f in all_flag_values(argv, "--filter"):
        if f.startswith("name="):
            name_filter = f.split("=", 1)[1]
    fmt = flag_value(argv, "--format")
    quiet = "-q" in argv or "-aq" in argv
    for name, c in containers.items():
        if name_filter and name_filter not in name:
            continue
        if running_only and not c.get("running"):
            continue
        if quiet:
            print(name)
        elif fmt:
            print(render(fmt, {**c, "name": name}))
    return 0


def cmd_inspect(state: dict, argv: list[str]) -> int:
    fmt = flag_value(argv, "--format") or ""
    names = positional(argv, VALUE_FLAGS)[1:]
    rc = 0
    for name in names:
        c = state.get("containers", {}).get(name)
        if c is None:
            found = image_of(state, name)
            if found is None:
                print(f"Error: No such object: {name}", file=sys.stderr)
                rc = 1
                continue
            print(render(fmt, found[1]))
            continue
        mounts_s = "".join(f"{src}={dst};" for src, dst in c.get("mounts", []))
        print(render(fmt, {**c, "name": name, "mounts_s": mounts_s}))
    return rc


def cmd_image(state: dict, argv: list[str]) -> int:
    if argv[1] != "inspect":
        return 0
    fmt = flag_value(argv, "--format") or "{{.Id}}"
    refs = positional(argv, VALUE_FLAGS)[2:]
    rc = 0
    for ref in refs:
        found = image_of(state, ref)
        if found is None:
            print(f"Error: No such image: {ref}", file=sys.stderr)
            rc = 1
            continue
        tag, data = found
        print(render(fmt, {**data, "tag": tag, "short_id": data["id"].removeprefix("sha256:")[:12]}))
    return rc


def cmd_images(state: dict, argv: list[str]) -> int:
    fmt = flag_value(argv, "--format") or "{{.Repository}}:{{.Tag}}"
    repo = next((a for a in argv[1:] if not a.startswith("-") and a != fmt), None)
    for tag, data in state.get("images", {}).items():
        if repo and not tag.startswith(repo + ":") and tag != repo:
            continue
        print(render(fmt, {**data, "tag": tag, "short_id": data["id"].removeprefix("sha256:")[:12]}))
    return 0


def cmd_volume(state: dict, argv: list[str]) -> int:
    volumes = state.setdefault("volumes", [])
    if argv[1] == "inspect":
        name = positional(argv, VALUE_FLAGS)[2]
        if name not in volumes:
            print(f"Error: No such volume: {name}", file=sys.stderr)
            return 1
        return 0
    if argv[1] == "create":
        name = positional(argv, VALUE_FLAGS)[2]
        if name not in volumes:
            volumes.append(name)
        return 0
    if argv[1] == "ls":
        for v in volumes:
            print(v)
        return 0
    if argv[1] == "rm":
        # Never expected: no Agent command may remove a volume.
        for name in positional(argv, VALUE_FLAGS)[2:]:
            if name in volumes:
                volumes.remove(name)
        return 0
    return 0


def cmd_rm(state: dict, argv: list[str]) -> int:
    names = positional(argv, VALUE_FLAGS)[1:]
    rc = 0
    for name in names:
        if state.get("containers", {}).pop(name, None) is None:
            print(f"Error response from daemon: No such container: {name}", file=sys.stderr)
            rc = 1
    return rc


def cmd_rmi(state: dict, argv: list[str]) -> int:
    for ref in positional(argv, VALUE_FLAGS)[1:]:
        found = image_of(state, ref)
        if found:
            state["images"].pop(found[0], None)
    return 0


def cmd_start(state: dict, argv: list[str]) -> int:
    for name in positional(argv, VALUE_FLAGS)[1:]:
        c = state.get("containers", {}).get(name)
        if c is None:
            return 1
        c["running"] = True
    return 0


def cmd_system(state: dict, argv: list[str]) -> int:
    if argv[1] != "df":
        return 0
    rows = [{"Name": name, "Size": size} for name, size in state.get("volume_sizes", {}).items()]
    print(json.dumps(rows))
    return 0


def main() -> int:
    argv = sys.argv[1:]
    if not argv:
        return 1
    # Deliberate slowness, *outside* the scenario lock, so concurrency tests get a
    # real window in which two invocations could overlap if nothing prevented it.
    delay = load().get("delay", 0)
    if delay and argv[0] == "run" and "-lc" in argv:
        time.sleep(float(delay))
    with state_lock():
        return dispatch(argv)


def dispatch(argv: list[str]) -> int:
    state = load()
    if argv[0] in state.get("fail", {}).get("commands", []):
        log({"cmd": argv[0], "forced_failure": True})
        return 1
    try:
        if argv[0] == "info":
            return 0
        if argv[0] == "version":
            print(render(flag_value(argv, "--format") or "{{.Server.Version}}", {}))
            return 0
        handlers = {
            "build": cmd_build,
            "run": cmd_run,
            "ps": cmd_ps,
            "inspect": cmd_inspect,
            "image": cmd_image,
            "images": cmd_images,
            "volume": cmd_volume,
            "rm": cmd_rm,
            "rmi": cmd_rmi,
            "start": cmd_start,
            "system": cmd_system,
        }
        handler = handlers.get(argv[0])
        if handler is None or argv[0] not in ("build", "run"):
            # build/run log richer entries from their own handlers.
            log({"cmd": argv[0], "argv": argv, "unhandled": handler is None})
        if handler is None:
            return 0
        return handler(state, argv)
    finally:
        save(state)


if __name__ == "__main__":
    sys.exit(main())
