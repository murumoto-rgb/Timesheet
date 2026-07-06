#!/usr/bin/env bash
# Frontend regression suite: loads index.html in headless Chromium with mocked
# /api/*, and asserts the values the dashboard/report/week views render.
#
# Uses the globally-installed playwright via NODE_PATH (no local npm install
# needed in this environment). Override the browser with PLAYWRIGHT_CHROMIUM.
set -euo pipefail
cd "$(dirname "$0")/../.."
export NODE_PATH="${NODE_PATH:-$(npm root -g)}"
exec node --test tests/frontend/*.test.mjs
