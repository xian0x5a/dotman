#!/bin/sh
set -eu

# Example custom Editor. Dotman passes the staged, editable repo sources as
# "$@" and the read-only repo/live review through DOTMAN_EDITOR_REVIEW_PATH.
# Saved edits come back to the Sync Deck as a Proposal for review.
exec nvim -o "$DOTMAN_EDITOR_REVIEW_PATH" "$@"
