lint:
    shellcheck agent install-system install-tools

format:
    shfmt -i 4 -w agent install-system install-tools guest-root/etc/bash.bashrc

fix:
    shellcheck --format=diff agent install-system install-tools | git apply --allow-empty
    shfmt -i 4 -w agent install-system install-tools guest-root/etc/bash.bashrc
