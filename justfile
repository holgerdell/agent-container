lint:
    shellcheck agent install-system install-tools

format:
    shfmt -i 4 -w agent install-system install-tools guest-root/etc/bash.bashrc

fix:
    shellcheck --format=diff agent install-system install-tools | git apply --allow-empty
    shfmt -i 4 -w agent install-system install-tools guest-root/etc/bash.bashrc

# Copy guest-root/ files into place in the guest (mirrors lima.yaml provisions).
install:
    #!/usr/bin/env bash
    set -euo pipefail
    instance="${AGENT_LIMA_INSTANCE:-agent-sandbox}"
    find guest-root -type f | sort | while IFS= read -r src; do
        dest="/${src#guest-root/}"
        printf 'installing %s -> %s:%s\n' "${src}" "${instance}" "${dest}"
        limactl shell --start "${instance}" -- sudo sh -c 'cat > "$1" && chmod 644 "$1"' sh "${dest}" <"${src}"
    done
