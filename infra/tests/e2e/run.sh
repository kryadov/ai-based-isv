#!/usr/bin/env bash
# Browser e2e of the panel (M11) in the pinned Playwright image (Chromium included).
# Usage (toolbox or host with docker): PANEL_URL=… PANEL_USER=… PANEL_PASSWORD=… \
#   PANEL_CARD_ID=… bash infra/tests/e2e/run.sh
# The password reaches the container through the environment (never argv).
set -euo pipefail
cd "$(dirname "$0")"
: "${PANEL_URL:?}" "${PANEL_USER:?}" "${PANEL_PASSWORD:?}" "${PANEL_CARD_ID:?}"
IMAGE=mcr.microsoft.com/playwright/python:v1.49.0-noble
export PANEL_URL PANEL_USER PANEL_PASSWORD PANEL_CARD_ID
exec docker run --rm --network host --ipc=host \
  -e PANEL_URL -e PANEL_USER -e PANEL_PASSWORD -e PANEL_CARD_ID \
  -v "$PWD:/e2e:ro" -w /e2e "$IMAGE" \
  bash -c 'pip install -q --disable-pip-version-check playwright==1.49.0 pytest==8.3.4 pytest-playwright==0.6.2 \
    && python -m pytest -q -p no:cacheprovider test_panel.py --browser chromium'
