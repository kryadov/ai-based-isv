"""Stand LAN exposure (M12): the firewall script narrows Rancher Desktop's
host-switch allow rules to loopback, can be checked without admin and reverted,
and platform.sh status reports drift."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "infra" / "rancher" / "restrict-firewall.ps1"


def test_script_limits_host_switch_rules_to_loopback() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "host-switch.exe" in text
    assert "'127.0.0.1', '::1'" in text
    assert "Set-NetFirewallRule -RemoteAddress" in text
    for switch in ("[switch]$Check", "[switch]$Revert"):
        assert switch in text
    assert "WindowsBuiltInRole]::Administrator" in text


def test_platform_status_reports_firewall_drift() -> None:
    text = (ROOT / "infra" / "scripts" / "platform.sh").read_text(encoding="utf-8")
    assert "restrict-firewall.ps1" in text and "-Check" in text
