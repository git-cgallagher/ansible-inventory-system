#!/usr/bin/env bash
# Throwaway AIS server for the Playwright smoke: the seed tree in a fresh SQLite file and
# one editor with a placeholder password (example-not-secret, test data only). Uses the
# repo's .venv when it has `ais`, else `ais` from PATH (CI). Serves the UI from
# server/ais/static, so run `npm run build` first.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
if [ -x "$repo/.venv/bin/ais" ]; then ais="$repo/.venv/bin/ais"; else ais="$(command -v ais)"; fi
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
export AIS_DATABASE_URL="sqlite:///$tmp/e2e.db"
export AIS_WEB_DIR="$repo/server/ais/static"
export AIS_ETAG_STORE=""
"$ais" import "$repo/examples/seed.yml" >/dev/null
printf '%s\n' "${E2E_PASSWORD:-example-not-secret}" | "$ais" user create "${E2E_EMAIL:-e2e@example.com}" --role editor --password-stdin
"$ais" serve --port "${E2E_PORT:-8799}"
