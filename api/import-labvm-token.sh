#!/usr/bin/env bash
# Renamed: import-shared-tokens.sh (it now also shares the capture token).
# Kept so the command in older notes still works.
exec bash "$(dirname "${BASH_SOURCE[0]}")/import-shared-tokens.sh" "$@"
