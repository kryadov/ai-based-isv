<#
.SYNOPSIS
  Keep the stand's NodePorts off the LAN (M12, owner decision 2026-11-23).

.DESCRIPTION
  Rancher Desktop forwards every NodePort on 0.0.0.0 through host-switch.exe, and
  its installer adds inbound "Rancher Desktop Networking ... Exception" allow rules
  (Private/Domain profiles, any remote address) — so Gitea, OpenBao-backed services,
  the task-source MCP and Prometheus answer anyone on the local network.

  This script limits those allow rules to loopback remote addresses. Traffic from
  this machine to localhost:<port> never passes the Windows Firewall (loopback is
  exempt), and host-switch talks to the VM over hvsocket, so the stand keeps working
  locally while LAN neighbours are refused.

  Idempotent. Rancher Desktop upgrades may recreate the rules: `-Check` reports
  drift (exit 1), and `platform.sh status` runs it.

.PARAMETER Check
  Report only (no admin needed): exit 0 when every rule is loopback-only.

.PARAMETER Revert
  Restore RemoteAddress Any (the installer's default).

.EXAMPLE
  # elevated PowerShell, repo root:
  powershell -ExecutionPolicy Bypass -File infra\rancher\restrict-firewall.ps1
#>
[CmdletBinding()]
param(
    [switch]$Check,
    [switch]$Revert
)

$ErrorActionPreference = 'Stop'
$Loopback = @('127.0.0.1', '::1')

function Get-RancherRules {
    Get-NetFirewallApplicationFilter |
        Where-Object { $_.Program -like '*Rancher Desktop*host-switch.exe' } |
        ForEach-Object { $_ | Get-NetFirewallRule } |
        Where-Object { $_.Direction -eq 'Inbound' -and $_.Action -eq 'Allow' }
}

function Get-Remote($rule) {
    (($rule | Get-NetFirewallAddressFilter).RemoteAddress) -join ','
}

function Test-LoopbackOnly($remote) {
    $parts = $remote -split ','
    foreach ($part in $parts) {
        if ($Loopback -notcontains $part) { return $false }
    }
    return $true
}

$rules = @(Get-RancherRules)
if ($rules.Count -eq 0) {
    Write-Output 'firewall: no Rancher Desktop host-switch allow rules (nothing exposed through them)'
    exit 0
}

if ($Check) {
    $open = 0
    foreach ($rule in $rules) {
        $remote = Get-Remote $rule
        $state = 'LAN-OPEN'
        if (Test-LoopbackOnly $remote) { $state = 'loopback-only' }
        if ($state -eq 'LAN-OPEN' -and $rule.Enabled -eq 'True') { $open++ }
        Write-Output ("firewall: {0} [{1}] remote={2} -> {3}" -f $rule.DisplayName, $rule.Profile, $remote, $state)
    }
    if ($open -gt 0) { exit 1 }
    exit 0
}

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Output 'firewall: run this from an elevated PowerShell (Run as administrator)'
    exit 2
}

$target = $Loopback
if ($Revert) { $target = @('Any') }
foreach ($rule in $rules) {
    $before = Get-Remote $rule
    $rule | Set-NetFirewallRule -RemoteAddress $target
    $after = Get-Remote $rule
    Write-Output ("firewall: {0} [{1}] remote {2} -> {3}" -f $rule.DisplayName, $rule.Profile, $before, $after)
}
