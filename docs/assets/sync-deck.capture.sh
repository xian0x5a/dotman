#!/usr/bin/env bash
# Regenerate docs/assets/sync-deck.svg, the README's Command Deck screenshot.
#
# Builds a throwaway demo dotfiles home, drifts a few targets, and saves the
# Deck through Textual. Linux only: bubblewrap mounts the demo home at
# /home/demo so paths in the image never leak the real home directory.
#
# Usage: docs/assets/sync-deck.capture.sh   (from anywhere in the checkout)
set -euo pipefail

PROJECT=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
OUTPUT=$PROJECT/docs/assets/sync-deck.svg
DEMO_HOME=/home/demo
TERMINAL_SIZE=96x21
# Select ghostty and git, leaving the cursor on nvim so the detail pane shows a Merge row.
KEYS=space,down,space,down

if [[ ${CAPTURE_STAGE:-} != sandbox ]]; then
    scratch=$(mktemp -d)
    trap 'rm -rf "$scratch"' EXIT
    # Hide /home behind a tmpfs, then bring back the real home (project, uv cache) and the demo.
    bwrap --dev-bind / / --tmpfs /home --bind "$HOME" "$HOME" --bind "$scratch" "$DEMO_HOME" \
        --setenv HOME "$DEMO_HOME" \
        --setenv XDG_CONFIG_HOME "$DEMO_HOME/.config" \
        --setenv XDG_STATE_HOME "$DEMO_HOME/.local/state" \
        --setenv XDG_DATA_HOME "$DEMO_HOME/.local/share" \
        --setenv UV_CACHE_DIR "$(uv cache dir)" \
        --setenv CAPTURE_STAGE sandbox \
        "$0"
    echo "wrote $OUTPUT"
    exit
fi

cd "$PROJECT"
repo=$HOME/dotfiles
mkdir -p "$HOME/.config/dotman" "$repo/profiles"
printf '[vars]\n' > "$repo/profiles/laptop.toml"

add_target() {  # package target source live_path policy content
    local package=$1 target=$2 source=$3 live_path=$4 policy=$5 content=$6
    mkdir -p "$repo/packages/$package/files/$(dirname "$source")"
    printf '%b' "$content" > "$repo/packages/$package/files/$source"
    printf '\n[targets.%s]\nsource = "files/%s"\npath = "%s"\nsync_policy = "%s"\n' \
        "$target" "$source" "$live_path" "$policy" >> "$repo/packages/$package/package.toml"
}

add_target git gitconfig gitconfig "~/.gitconfig" both '[user]\n    name = Demo\n[core]\n    editor = nvim\n[pull]\n    rebase = true\n'
add_target zsh zshrc zshrc "~/.zshrc" both 'export EDITOR=nvim\nalias ll="ls -la"\nsource ~/.config/zsh/plugins.zsh\n'
add_target zsh plugins config/zsh/plugins.zsh "~/.config/zsh/plugins.zsh" push-only 'zinit light zsh-users/zsh-autosuggestions\n'
add_target nvim init config/nvim/init.lua "~/.config/nvim/init.lua" both 'vim.g.mapleader = " "\nvim.opt.number = true\n'
add_target starship config config/starship.toml "~/.config/starship.toml" push-only 'add_newline = false\n'
add_target ghostty config config/ghostty/config "~/.config/ghostty/config" pull-only 'theme = catppuccin-mocha\nfont-size = 13\n'
add_target tmux conf tmux.conf "~/.tmux.conf" both 'set -g mouse on\n'
printf '[repos.dot]\npath = "~/dotfiles"\norder = 10\n' > "$HOME/.config/dotman/config.toml"
git -C "$repo" init -q
git -C "$repo" add -A
git -C "$repo" -c user.email=demo@example.com -c user.name=Demo commit -qm init

# Establish Sync Bases so later edits on both sides show as Merge.
for package in git zsh nvim starship ghostty tmux; do uv run -q dotman track "dot:$package@laptop" >/dev/null; done
mkdir -p "$HOME/.config/ghostty"
cp "$repo/packages/ghostty/files/config/ghostty/config" "$HOME/.config/ghostty/config"
uv run -q dotman --no-confirm push >/dev/null
uv run -q dotman --no-confirm pull >/dev/null

# Drift: live-only, repo-only, and both-sided edits.
printf '[user]\n    name = Demo\n    email = demo@example.com\n[core]\n    editor = nvim\n[pull]\n    rebase = true\n' > "$HOME/.gitconfig"
printf '[init]\n    defaultBranch = main\n' >> "$repo/packages/git/files/gitconfig"
printf 'alias gs="git status"\n' >> "$HOME/.zshrc"
printf 'vim.opt.relativenumber = true\n' >> "$repo/packages/nvim/files/config/nvim/init.lua"
printf 'zinit light zsh-users/zsh-syntax-highlighting\n' >> "$repo/packages/zsh/files/config/zsh/plugins.zsh"
sed -i 's/13/14/' "$HOME/.config/ghostty/config"
printf '[character]\nsuccess_symbol = "[>](bold green)"\n' >> "$repo/packages/starship/files/config/starship.toml"

uv run -q python - "$OUTPUT" "$KEYS" "$TERMINAL_SIZE" <<'PYTHON'
import asyncio
import sys
from pathlib import Path

from dotman.engine import DotmanEngine
from dotman.sync_deck import CommandDeck, SyncDeckApp
from dotman.ui_context import ui_config_scope

output, keys, size = Path(sys.argv[1]), sys.argv[2].split(","), tuple(map(int, sys.argv[3].split("x")))
engine = DotmanEngine.from_config_path(None)


async def capture(app):
    async with app.run_test(size=size) as pilot:
        await pilot.pause(0.3)
        for key in keys:
            await pilot.press(key)
            await pilot.pause(0.2)
        await pilot.pause(0.5)
        app.save_screenshot(filename=output.name, path=str(output.parent))


with ui_config_scope(engine.config.ui):
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        app = SyncDeckApp(CommandDeck(session, use_color=True))
        app.title = "dotman sync"
        asyncio.run(capture(app))
PYTHON
