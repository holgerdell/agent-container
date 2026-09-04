# System-wide bashrc for the agent VM, shipped from guest-root. Every
# interactive bash reads it, so nothing depends on files in the /home/agent volume.
# PATH is handled by the image (ENV PATH, and the generated profile snippet for
# login shells), not here.

# Do not apply interactive settings to scripts and `bash -lc` commands.
[[ $- != *i* ]] && return

# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

HISTSIZE=50000
HISTFILESIZE=100000

# Ignore commands starting with a space, consecutive duplicates, and older
# duplicate copies of a command.
HISTCONTROL=ignoreboth:erasedups

# Do not clutter history with these common commands.
HISTIGNORE='ls:ll:la:cd:pwd:exit:clear:history'

# Keep multiline commands together and append history rather than overwriting.
shopt -s cmdhist
shopt -s lithist
shopt -s histappend

# Synchronize history across concurrent sessions, preserving any PROMPT_COMMAND.
PROMPT_COMMAND="${PROMPT_COMMAND:+${PROMPT_COMMAND}; }history -a; history -n"

# ---------------------------------------------------------------------------
# Prompt: starship if available, otherwise user@host, current folder, git branch.
# ---------------------------------------------------------------------------

git_branch() {
    git branch --show-current 2>/dev/null | sed 's/.*/(&)/'
}
PS1='\[\e[32m\]\u@\h\[\e[0m\] \[\e[34m\]\w\[\e[0m\] \[\e[33m\]$(git_branch)\[\e[0m\]\$ '

if command -v starship >/dev/null 2>&1; then
    eval "$(starship init bash)"
    # Keep history synchronized across concurrent sessions when starship
    # manages PROMPT_COMMAND.
    if [[ "${PROMPT_COMMAND:-}" != *'history -a'* ]]; then
        PROMPT_COMMAND="${PROMPT_COMMAND:+${PROMPT_COMMAND}; }history -a; history -n"
    fi
fi

# ---------------------------------------------------------------------------
# Fish-like shell behavior
# ---------------------------------------------------------------------------

# Enter a directory just by typing its name.
# Example: typing `project` enters ./project if it exists.
shopt -s autocd

# Correct small spelling errors when changing directories.
shopt -s cdspell
shopt -s dirspell

# Enable recursive globbing: **/*.py
shopt -s globstar

# Detect terminal-window size changes.
shopt -s checkwinsize

# Extended glob patterns, e.g. `!(*.log)`.
shopt -s extglob

# ---------------------------------------------------------------------------
# Bash completion
# ---------------------------------------------------------------------------

# Load the system-installed bash-completion package.
if [ -r /usr/share/bash-completion/bash_completion ]; then
    source /usr/share/bash-completion/bash_completion
elif [ -r /etc/bash_completion ]; then
    source /etc/bash_completion
fi

# Case-insensitive completion.
bind 'set completion-ignore-case on'

# Show completion choices immediately when there is more than one match.
bind 'set show-all-if-ambiguous on'

# Show the shared completion prefix before listing choices.
bind 'set menu-complete-display-prefix on'

# Add useful markers in completion results: `/` directories, `*` executables.
bind 'set visible-stats on'

# Add a slash after completed symlinked directories.
bind 'set mark-symlinked-directories on'

# Pressing Tab repeatedly cycles through candidates, like fish menus.
bind '"\t": menu-complete'
bind '"\e[Z": menu-complete-backward'

# ---------------------------------------------------------------------------
# History searching and line editing
# ---------------------------------------------------------------------------

# Type a prefix, then press Up/Down to search matching history entries.
#
# Example:
#   type: squeue
#   press Up
#   Bash recalls previous commands beginning with "squeue".
bind '"\e[A": history-search-backward'
bind '"\e[B": history-search-forward'

# Ensure both common Backspace encodings work.
bind '"\C-?": backward-delete-char'
bind '"\C-h": backward-delete-char'

# Ctrl-Left / Ctrl-Right: move by one word.
bind '"\e[1;5D": backward-word'
bind '"\e[1;5C": forward-word'

# Alt-Backspace: delete the preceding word.
bind '"\e\C-?": backward-kill-word'

# ---------------------------------------------------------------------------
# Useful aliases
# ---------------------------------------------------------------------------

alias ..='cd ..'
alias ...='cd ../..'
alias ....='cd ../../..'
alias .....='cd ../../../..'

alias cls='clear'
alias h='history'

# Print one PATH entry per line.
alias path='printf "%s\n" "${PATH//:/\\n}"'

# Colorized standard utilities.
if command -v dircolors >/dev/null 2>&1; then
    eval "$(dircolors -b)"
fi

alias ls='ls --color=auto'
alias grep='grep --color=auto'
alias egrep='egrep --color=auto'
alias fgrep='fgrep --color=auto'
alias diff='diff --color=auto'

# Safer interactive file operations.
alias rm='rm -I'
alias cp='cp -i'
alias mv='mv -i'
