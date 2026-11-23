"""The daemon container (M11) stops gracefully (M12): docker waits long enough
for the daemon's own shutdown grace (60 s) before it kills the container."""

from __future__ import annotations

import re
from pathlib import Path

TOOLBOX = Path(__file__).resolve().parents[3] / "infra" / "scripts" / "toolbox.sh"


def test_detached_toolbox_container_has_a_stop_timeout_above_the_daemon_grace() -> None:
    text = TOOLBOX.read_text(encoding="utf-8")
    line = next(ln for ln in text.splitlines() if "TOOLBOX_DETACH:-" in ln and "run_flags=" in ln)
    match = re.search(r"--stop-timeout (\d+)", line)
    assert match is not None
    assert int(match.group(1)) >= 90


def test_platform_script_stops_the_daemon_gracefully_before_removing_it() -> None:
    text = (TOOLBOX.parent / "platform.sh").read_text(encoding="utf-8")
    assert 'docker rm -f "$NAME"' not in text
    assert 'docker stop "$NAME"' in text
