#!/bin/sh
# examples/statusline.sh — reference status-line integration for relay.
#
# Point Claude Code's `statusLine.command` (in ~/.claude/settings.json) at a copy of this file,
# or copy the relevant bits into your own status-line script. See README.md's "Status line
# integration" section for the full picture.
#
# POSIX sh, no dependencies beyond what the README already assumes (relay itself + coreutils).

# Claude Code pipes a JSON payload (session_id, transcript_path, ...) to this script's stdin.
# stdin can only be read ONCE — capture it here so both relay's segment and any other bits you
# add below can use it.
input=$(cat)

# --- your other status-line segments go here, reading from "$input" (git branch, context %, etc.) ---

# Resolve relay's binary VERSION-AGNOSTICALLY. A marketplace install lands at a versioned path
# (~/.claude/plugins/cache/claude-relay/relay/<version>/bin/relay) with no `latest`/`current`
# symlink, and <version> changes on every `/plugin update` — a hardcoded path silently goes stale
# the next time you update. `sort -V | tail -1` picks the highest installed version's binary.
relay_bin=$(ls -d "$HOME/.claude/plugins/cache/claude-relay/relay"/*/bin/relay 2>/dev/null \
            | sort -V | tail -1)

if [ -z "$relay_bin" ]; then
    # relay's binary could not be resolved (not installed, moved, or this script is stale). Say so
    # ONCE, visibly — a broken resolver and a session relay has nothing to report on must not look
    # identical, or you'll just conclude "relay's status line stopped working" and never find out
    # why. Remove this branch (or make it silent) if you'd rather not show anything here.
    echo "🚦:? relay not found"
else
    relay_line=$(echo "$input" | "$relay_bin" status --statusline 2>/dev/null)
    if [ -n "$relay_line" ]; then
        # Option A — plain pass-through (simplest; relay already formats its own segment):
        echo "$relay_line"

        # Option B — wrapped to match a segmented status line's style: strip relay's own leading
        # "🚦 " marker and re-wrap as "🚦:(...)". Styling only, not required — use whichever fits
        # your status line. To use it, comment out Option A above and uncomment this line instead:
        # echo "🚦:(${relay_line#🚦 })"
    fi
    # relay_line empty means this session isn't a relay lead or executor — correctly silent, same
    # as any other unrelated status line.
fi
