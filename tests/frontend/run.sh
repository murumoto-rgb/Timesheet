#!/usr/bin/env bash
# Frontend regression suite: loads index.html in headless Chromium with mocked
# /api/*, and asserts the values the dashboard/report/week views render.
#
# Uses this repository's pinned Playwright install. Run npm ci and
# npx playwright install chromium first; PLAYWRIGHT_CHROMIUM is an override.
set -euo pipefail
cd "$(dirname "$0")/../.."
exec node --test --test-concurrency=2 tests/frontend/*.test.mjs
