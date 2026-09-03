#Requires -Version 5.1
<#
.SYNOPSIS
    gpudev windows-setup.ps1 — Phase A: Windows-side host preparation only.

.DESCRIPTION
    This script makes the Windows machine a reliable WSL host and registers the
    distro WITHOUT Ubuntu's interactive first-run (OOBE). It does NOT install
    gpudev components inside WSL — that's Phase B (linux-setup.sh, run from
    inside WSL by the operator).

    Why OOBE-free: the rolling `Ubuntu` image's first-run OOBE hangs
    ("Waiting for OOBE command to complete for distribution...") and never
    reaches the username prompt, which blocked clean reinstalls. The OOBE is
    triggered by `wsl --install` / .wsl registration; `wsl --import` of a rootfs
    tarball does NOT run it. So Phase A imports the rootfs and creates the user
    itself — fully scriptable, no interactive prompt.

    Phase A responsibilities:

      1. Verify Administrator + Windows build >= 19041.
      2. Check that the NVIDIA Windows driver is present (warn if not — WSL GPU
         passthrough requires the Windows driver, not a Linux one).
      3. Run `wsl --update` to keep the WSL kernel current.
      4. Configure Windows power settings (no auto-sleep on AC) and wake-on-LAN
         (wired NIC wakes on a magic packet; every other wake source disarmed).
      5. Write %USERPROFILE%\.wslconfig (instanceIdleTimeout=-1 keeps the
         distro alive; vmIdleTimeout=-1 keeps the shared WSL2 VM alive).
      6. Ensure the WSL2 platform is enabled (--no-distribution; reboots+resumes
         once only if the feature was just turned on), then `wsl --import` the
         distro from an Ubuntu rootfs tarball (no OOBE) and provision the Linux
         user + /etc/wsl.conf.
      7. Register a boot scheduled task that wakes the distro at logon — running
         as the OPERATOR, not SYSTEM (WSL distros are per-user; a SYSTEM task
         can't see/start them — that was the "nothing comes back after reboot"
         bug). Needs no stored password (LogonType Interactive).
      8. Register a periodic keepalive task (Layer 3, also as the operator) that
         re-wakes the distro if it exits between Windows boots (WSL crash,
         background update, etc.).

    Done by Phase A (new — previously deferred to Ubuntu's hung OOBE):
      - Linux user creation — -LinuxUser (default 'gpudev') is created with
        passwordless sudo (SSH into the host is key-only, so no login password).
      - /etc/wsl.conf — [user] default=<user> + [boot] systemd=true, so the very
        first `wsl -d <distro>` lands as that user with systemd already PID 1.
        Phase B's wsl.conf writer is section-aware and preserves both.

    Manual step Phase A canNOT do (it detects + prints instructions instead):
      - Windows AUTOLOGIN. The boot/keepalive tasks fire at LOGON, so for WSL to
        come back after an UNATTENDED reboot, Windows must auto-log-in the
        operator. That needs the account password (and, if it's a Microsoft
        account, an MSA->local conversion first), which the script won't handle.
        Enable it once with Sysinternals Autologon — see the printed handoff.

    Still Phase B (deliberately):
      - Docker, NVIDIA toolkit, cloudflared, gpudev CLI, base image, tunnel.

    Handoff (printed at the end as instructions):

        wsl -d gpudev            # lands as gpudev (no first-run prompt)
        # inside WSL:
        bash <(curl -fsSL https://raw.githubusercontent.com/rleyvasal/gpudev/main/linux-setup.sh)

.PARAMETER DistroName
    Name to register the imported distro under. Default: "gpudev". This becomes
    the default WSL distro that the boot/keepalive tasks wake. Naming it "gpudev"
    (rather than "Ubuntu") sidesteps any collision with a Store-installed Ubuntu
    and the "wrong default distro came back after reboot" problem. The distro
    name is Windows-local — it does NOT affect the tunnel hostname or SSH alias
    (those derive from -LinuxUser).

.PARAMETER LinuxUser
    Unix username to create (passwordless sudo, set as the distro's default
    user). Default: "gpudev" — this IS the cloudflared tunnel name, the DNS
    hostname (<user>.<domain>), and the Mac SSH alias (`ssh <user>`), so keeping
    it stable avoids re-doing your DNS + ~/.ssh/config on every reinstall.

.PARAMETER UbuntuSeries
    Ubuntu LTS series (codename) to import. Default: "noble" (24.04 LTS). Pins
    the compatibility target to a series WITHOUT pinning an exact point-release
    image — `.../releases/<series>/current/` always resolves to Canonical's
    latest refreshed rootfs for that series. Used only to derive -RootfsUrl when
    that isn't given. Set e.g. "jammy" to test 22.04. We deliberately do NOT
    auto-track a global "latest" major release: a surprise 24.04 -> 26.04 jump
    would silently shift Docker/NVIDIA apt repos, systemd behavior, and Python
    defaults out from under gpudev.

.PARAMETER RootfsUrl
    Explicit URL of the Ubuntu WSL rootfs tarball to import. Escape hatch — when
    empty (default) it is derived from -UbuntuSeries:
      https://cloud-images.ubuntu.com/wsl/releases/<series>/current/ubuntu-<series>-wsl-amd64-wsl.rootfs.tar.gz
    Cached under ProgramData\gpudev so a resume-after-reboot or re-run doesn't
    re-download ~340 MB.

.PARAMETER InstallLocation
    Folder for the imported distro's VHD. Default: %LOCALAPPDATA%\WSL\<DistroName>.

.PARAMETER Reinstall
    If the distro is already registered, UNREGISTER it first (this ERASES that
    distro's data) and re-import clean. Use this for a fresh reinstall.

.PARAMETER SkipReboot
    Don't auto-reboot if enabling the WSL2 platform needs one. You'll have to
    reboot yourself and let the resume task pick up at logon (or re-run this
    script with -Resume).

.PARAMETER Resume
    Internal: re-entry point after the platform-enable reboot. Don't pass by hand.

.EXAMPLE
    # Default install (distro gpudev on Ubuntu 24.04 LTS, user gpudev):
    .\windows-setup.ps1

.EXAMPLE
    # Fresh reinstall, wiping any existing gpudev distro first:
    .\windows-setup.ps1 -Reinstall

.EXAMPLE
    # Test a different LTS series (22.04):
    .\windows-setup.ps1 -UbuntuSeries jammy -Reinstall
#>
[CmdletBinding()]
param(
    [string]$DistroName = 'gpudev',
    [string]$LinuxUser = 'gpudev',
    [string]$UbuntuSeries = 'noble',
    [string]$RootfsUrl = '',
    [string]$InstallLocation = '',
    [switch]$Reinstall,
    [switch]$SkipReboot,
    [switch]$Resume
)

# Pin the LTS series, NOT an exact point-release: `.../releases/<series>/current/`
# always points at Canonical's latest refreshed rootfs for that series, so we
# track Noble security/point updates but never auto-jump to a new major release.
if (-not $RootfsUrl) {
    $RootfsUrl = "https://cloud-images.ubuntu.com/wsl/releases/$UbuntuSeries/current/ubuntu-$UbuntuSeries-wsl-amd64-wsl.rootfs.tar.gz"
}
if (-not $InstallLocation) {
    $InstallLocation = Join-Path $env:LOCALAPPDATA "WSL\$DistroName"
}

# Promote params into script scope so nested functions see them, and so the
# post-reboot resume (which re-reads them from saved state via Load-State) and
# the first pass agree on the same values.
$script:Distro          = $DistroName
$script:LinuxUser       = $LinuxUser
$script:UbuntuSeries    = $UbuntuSeries
$script:RootfsUrl       = $RootfsUrl
$script:InstallLocation = $InstallLocation
$script:Reinstall       = [bool]$Reinstall

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$StateDir       = Join-Path $env:ProgramData 'gpudev'
$StateFile      = Join-Path $StateDir 'windows-setup-state.json'
$ResumeTaskName = 'gpudev-setup-resume'
$BootTaskName   = 'gpudev-wsl-boot'
$KeepaliveTaskName = 'gpudev-wsl-keepalive'

# ── Logging helpers (mirror linux-setup.sh) ───────────────────────────────────
function Step { param([string]$m) Write-Host "`n=== $m ===" -ForegroundColor Cyan }
function Log  { param([string]$m) Write-Host "  $m" }
function Warn { param([string]$m) Write-Host "Warning: $m" -ForegroundColor Yellow }
function Fail { param([string]$m) Write-Host "Error: $m" -ForegroundColor Red; exit 1 }

# ── Prerequisites ──────────────────────────────────────────────────────────────
function Assert-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($id)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Fail "This script must be run as Administrator (right-click PowerShell -> Run as administrator)."
    }
}

function Assert-WslSupported {
    # `wsl --install` needs Windows 10 build 19041+ or Windows 11.
    $build = [int](Get-CimInstance Win32_OperatingSystem).BuildNumber
    if ($build -lt 19041) {
        Fail "Windows build $build is too old for 'wsl --install' (need 19041+). Update Windows first."
    }
    Log "Windows build $build supports WSL2."
}

function Assert-LinuxUserName {
    # The username is interpolated into useradd / sudoers / wsl.conf, and becomes
    # the tunnel hostname + SSH alias. Restrict to a conventional Linux username
    # so the interpolation can't be subverted and useradd won't reject it.
    if ($script:LinuxUser -notmatch '^[a-z_][a-z0-9_-]{0,31}$') {
        Fail "Invalid -LinuxUser '$script:LinuxUser'. Use lowercase letters/digits/'-'/'_', starting with a letter or '_' (e.g. gpudev)."
    }
}

# ── Resume state (just enough to survive the reboot) ───────────────────────────
function Save-State {
    if (-not (Test-Path $StateDir)) { New-Item -ItemType Directory -Path $StateDir -Force | Out-Null }
    $state = [ordered]@{
        Distro          = $script:Distro
        LinuxUser       = $script:LinuxUser
        UbuntuSeries    = $script:UbuntuSeries
        RootfsUrl       = $script:RootfsUrl
        InstallLocation = $script:InstallLocation
        Reinstall       = $script:Reinstall
        ScriptPath      = $PSCommandPath
    }
    $state | ConvertTo-Json | Set-Content -Path $StateFile -Encoding UTF8
}

function Load-State {
    if (-not (Test-Path $StateFile)) {
        Fail "Resume requested but no saved state at $StateFile. Re-run without -Resume."
    }
    $s = Get-Content $StateFile -Raw | ConvertFrom-Json
    $script:Distro          = $s.Distro
    $script:LinuxUser       = $s.LinuxUser
    $script:UbuntuSeries    = $s.UbuntuSeries
    $script:RootfsUrl       = $s.RootfsUrl
    $script:InstallLocation = $s.InstallLocation
    $script:Reinstall       = [bool]$s.Reinstall
}

function Remove-State {
    if (Test-Path $StateFile) { Remove-Item $StateFile -Force -ErrorAction SilentlyContinue }
}

# ── NVIDIA driver check ────────────────────────────────────────────────────────
function Test-NvidiaDriver {
    $smi = Join-Path $env:SystemRoot 'System32\nvidia-smi.exe'
    if (-not (Test-Path $smi)) {
        Warn ""
        Warn "NVIDIA Windows driver NOT detected (nvidia-smi.exe missing in System32)."
        Warn ""
        Warn "  gpudev needs GPU passthrough into WSL2, which requires the NVIDIA"
        Warn "  driver installed on WINDOWS — not a Linux driver inside WSL."
        Warn ""
        Warn "  Install or update the driver before running Phase B:"
        Warn "    https://www.nvidia.com/Download/index.aspx"
        Warn "  or use the NVIDIA App / GeForce Experience to update in place."
        Warn ""
        Warn "  Phase A will continue, but Phase B's GPU verification will fail"
        Warn "  if the driver isn't present by then."
        return
    }
    # Driver present — print version.
    try {
        $verLine = (& $smi --query-gpu=driver_version --format=csv,noheader 2>$null | Select-Object -First 1)
        if ($verLine) {
            Log "NVIDIA Windows driver detected: $($verLine.Trim())"
        } else {
            Log "NVIDIA Windows driver detected (nvidia-smi.exe present)."
        }
    } catch {
        Log "NVIDIA Windows driver detected (could not query version)."
    }
    Log "  WSL2 GPU passthrough should work once Phase B installs the NVIDIA container toolkit."
    Log "  If GPU verification fails in Phase B, update the driver from"
    Log "  https://www.nvidia.com/Download/index.aspx and re-run linux-setup.sh."
}

# ── WSL kernel update ──────────────────────────────────────────────────────────
function Update-WslKernel {
    Log "Updating WSL kernel (wsl --update)..."
    & wsl.exe --update 2>&1 | Write-Host
    if ($LASTEXITCODE -ne 0) {
        Warn "wsl --update returned exit $LASTEXITCODE — kernel may not be up to date, continuing."
    }
}

# ── Windows power settings ─────────────────────────────────────────────────────

# Every power scheme's GUID. Parsed out of `powercfg /list` because its scheme
# LABELS are localized while the GUIDs are not. Power settings are applied to all
# schemes rather than just the active one, so switching power plans later cannot
# quietly restore a sleep timeout we deliberately cleared.
function Get-PowerSchemeGuids {
    $guids = @(& powercfg /list 2>$null |
        ForEach-Object { if ("$_" -match '([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})') { $Matches[1] } })
    if (-not $guids) { $guids = @('SCHEME_CURRENT') }
    return $guids
}

function Set-PowerSettings {
    # Never auto-sleep / hibernate / spin down on AC — a GPU host must stay up.
    # Hibernate is turned OFF so an explicit `gpudev power sleep` performs S3
    # sleep (and wakes cleanly) rather than hibernating. Turning hibernate off
    # also disables Fast Startup, which matters for wake-on-LAN: a Fast Startup
    # "shutdown" is really a hybrid hibernate that powers the NIC down, so WoL
    # from S5 silently stops working while it is on. Don't re-enable hibernate
    # without re-testing the wake path.
    Log "Configuring Windows power plan (host stays awake unless told to sleep)..."

    $sleepSub     = '238c9fa8-0aad-41ed-83f4-97be242c8f20'   # Sleep subgroup
    $unattendGuid = '7bc4a2f9-d8fc-4469-b07b-33eb785aaca0'   # System unattended sleep timeout

    # The System Unattended Sleep Timeout is HIDDEN from both the Settings UI and
    # `powercfg /query` (its Attributes value is 1) and defaults to 120 seconds.
    # It replaces the normal idle timeout whenever the machine wakes from a DEVICE
    # rather than from user input — which is exactly what a wake-on-LAN magic
    # packet is. A WoL-woken host therefore sleeps again two minutes later while
    # "Make my device sleep after" still reads Never, with nothing in the UI to
    # explain it. Unhide it so the value is visible to whoever debugs this next;
    # it is zeroed with the other idle timeouts below.
    try {
        Set-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\Power\PowerSettings\$sleepSub\$unattendGuid" `
            -Name Attributes -Value 2 -Type DWord -ErrorAction Stop
        Log "  Unhid the system unattended sleep timeout so it shows in the power UI."
    } catch {
        Warn "  Could not unhide the unattended sleep timeout (it is still set to Never below)."
    }

    # Hibernate off also disables Fast Startup, whose hybrid shutdown powers the
    # NIC down and silently breaks wake-on-LAN from S5.
    & powercfg /hibernate off 2>$null | Out-Null

    # Applied to EVERY scheme, on both AC and DC. DC is included even though a
    # desktop never runs on battery, so the same script behaves identically on a
    # UPS-backed or laptop host that briefly reports DC.
    $idleSettings = @(
        @{ Sub = $sleepSub; Id = '29f6c1db-86da-48c5-9fdb-f2b67b1f44da'; Value = 0; Name = 'Sleep after -> Never' }
        @{ Sub = $sleepSub; Id = '9d7815a6-7ee4-497e-8888-515a05f02364'; Value = 0; Name = 'Hibernate after -> Never' }
        @{ Sub = $sleepSub; Id = $unattendGuid;                          Value = 0; Name = 'Unattended sleep -> Never' }
        @{ Sub = $sleepSub; Id = '94ac6d29-73ce-41a6-809f-6363ba21b47e'; Value = 0; Name = 'Hybrid sleep -> Off' }
        @{ Sub = '0012ee47-9041-4b5d-9b77-535fba8b1442'
           Id  = '6738e2c4-e8a5-4a42-b16a-e040e769756e'; Value = 0; Name = 'Disk timeout -> Never' }
    )

    $schemes = Get-PowerSchemeGuids
    foreach ($g in $schemes) {
        foreach ($s in $idleSettings) {
            & powercfg /setacvalueindex $g $s.Sub $s.Id $s.Value 2>$null | Out-Null
            & powercfg /setdcvalueindex $g $s.Sub $s.Id $s.Value 2>$null | Out-Null
        }
        # The monitor may blank; only the SYSTEM is barred from sleeping.
        & powercfg /setacvalueindex $g '7516b95f-f776-4464-8c53-06167f40cc99' `
            '3c0bc021-c8a8-4e07-a973-6b14cbcb2b7e' 600 2>$null | Out-Null
    }
    foreach ($s in $idleSettings) { Log "  $($s.Name)" }
    Log "  Applied across $($schemes.Count) power scheme(s); monitor blanks after 10 min."

    # Activate LAST. powercfg only commits edits to the ACTIVE scheme when that
    # scheme is (re)activated, and the values above must already be written when
    # it happens. The previous version had this backwards: it applied `/change` to
    # whatever plan was active and only then switched to High performance, so on a
    # machine that started on Balanced the timeouts landed on the wrong plan and
    # High performance kept its own defaults.
    & powercfg /setactive SCHEME_MIN 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Log "  Active plan: High performance."
    } else {
        & powercfg /setactive SCHEME_CURRENT 2>$null | Out-Null
        Warn "  Could not select High performance; kept the current plan (values still applied)."
    }
}

# ── Wake-on-LAN (sleep on demand, wake on a magic packet) ─────────────────────

# Keywords that arm a wake on ORDINARY traffic rather than on a magic packet.
# Each is listed in both spellings on purpose: the '*'-prefixed names are the
# standard NDIS keywords, and the bare names are the vendor keywords some drivers
# implement instead. Device Manager's "Only allow a magic packet to wake the
# computer" checkbox writes only the standard spelling, so on a driver that uses
# the vendor one (MediaTek's Wi-Fi parts, for instance) the box is ticked while
# pattern-match wake is still armed underneath — the host then wakes about a
# second after it sleeps, forever. Clearing both spellings is the actual fix.
$script:WakeNoiseKeywords = @(
    '*WakeOnPattern', 'WakeOnPattern',
    '*PMARPOffload',  'PMARPOffload',
    '*PMNSOffload',   'PMNSOffload'
)

# Link-power features that renegotiate or drop the PHY in low-power states, which
# silently breaks WoL on several Realtek parts.
$script:LinkPowerKeywords = @('EnableGreenEthernet', 'PowerSavingMode', '*EEE', 'AdvancedEEE')

# Ethernet is identified POSITIVELY (media type 802.3) rather than as "not
# wireless". Inverting it would classify WWAN/cellular and other exotic media as
# wired and arm them, which is both wrong and a spurious-wake risk.
function Test-WiredNic {
    param($Adapter)
    return ($Adapter.PhysicalMediaType -eq '802.3') -and
           ($Adapter.InterfaceDescription -notmatch 'Wi-?Fi|Wireless|WLAN|Bluetooth|Virtual')
}

# True on a Modern Standby (S0 low power idle) platform, where classic S3 usually
# does not exist and WoL is gated by a SEPARATE keyword. Read the platform flag
# instead of parsing `powercfg /a`, whose text is localized.
function Test-ModernStandby {
    try {
        $p = Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Power' -ErrorAction Stop
        return ([int]$p.CsEnabled -eq 1)
    } catch {
        return $false
    }
}

function Set-NicKeyword {
    param([string]$Adapter, [string]$Keyword, [int]$Value)
    try {
        Set-NetAdapterAdvancedProperty -Name $Adapter -RegistryKeyword $Keyword `
            -RegistryValue $Value -NoRestart -ErrorAction Stop
        return $true
    } catch {
        # Driver doesn't expose this keyword. Expected across vendors, not an error.
        return $false
    }
}

function Set-WakeOnLan {
    Log "Configuring wake-on-LAN..."

    $nics = @(Get-NetAdapter -Physical -ErrorAction SilentlyContinue |
        Where-Object { $_.InterfaceDescription -notmatch 'Bluetooth|Virtual' })
    if (-not $nics) {
        Warn "  No physical network adapters found — skipping wake-on-LAN configuration."
        return
    }

    $modernStandby = Test-ModernStandby
    if ($modernStandby) {
        Log "  Platform sleep: Modern Standby (S0 low power idle)."
    } else {
        Log "  Platform sleep: classic S3."
    }

    # Ethernet is the supported wake path: WoWLAN cannot be relied on to hold AP
    # association across sleep. Wi-Fi is armed ONLY as a fallback on a machine
    # with no Ethernet at all — otherwise such a host would sleep with no way to
    # wake it. That fallback is safe with respect to the sleep/wake loop, which is
    # caused by pattern-match wake (cleared below), never by magic packet.
    $wired = @($nics | Where-Object { Test-WiredNic $_ })
    if ($wired) {
        $wakeNics = $wired
    } else {
        $wakeNics = $nics
        Warn "  No Ethernet NIC found — falling back to Wi-Fi magic-packet wake (best effort)."
        Warn "  WoWLAN is driver-dependent; prefer a wired connection for a host you must wake."
    }
    $wakeNames = @($wakeNics | ForEach-Object { $_.InterfaceDescription })

    if (@($nics | Where-Object { $_.Status -eq 'Up' }).Count -gt 0) {
        Log "  Adapters will be reset to apply these settings — expect a brief network drop."
    }

    foreach ($nic in $nics) {
        # Clear every wake-on-ordinary-traffic keyword in both spellings. This is
        # the fix for the sleep/wake loop, and it applies to EVERY NIC, including
        # ones that are not a wake source, because a disarmed-in-powercfg NIC with
        # pattern match still set can be re-armed by a driver update.
        foreach ($kw in $script:WakeNoiseKeywords) { Set-NicKeyword $nic.Name $kw 0 | Out-Null }

        if ($wakeNames -contains $nic.InterfaceDescription) {
            foreach ($kw in $script:LinkPowerKeywords) { Set-NicKeyword $nic.Name $kw 0 | Out-Null }
            Set-NicKeyword $nic.Name '*WakeOnMagicPacket' 1 | Out-Null
            Set-NicKeyword $nic.Name 'S5WakeOnLan' 1 | Out-Null
            # On Modern Standby the classic magic-packet keyword is not enough:
            # wake from S0ix is gated by its own keyword, which ships disabled.
            if ($modernStandby) {
                Set-NicKeyword $nic.Name '*ModernStandbyWoLMagicPacket' 1 | Out-Null
            }
            Log "  $($nic.Name): magic-packet wake armed (MAC $($nic.MacAddress))."
        } else {
            Set-NicKeyword $nic.Name '*WakeOnMagicPacket' 0 | Out-Null
            Log "  $($nic.Name): wake disarmed."
        }

        try { Restart-NetAdapter -Name $nic.Name -ErrorAction Stop } catch { }
    }
    Start-Sleep -Seconds 3

    # The NDIS keywords above decide WHAT a NIC wakes on; powercfg decides WHETHER
    # a device may wake the system at all. Both have to agree.
    foreach ($nic in $nics) {
        if ($wakeNames -contains $nic.InterfaceDescription) {
            & powercfg /deviceenablewake  "$($nic.InterfaceDescription)" 2>$null | Out-Null
        } else {
            & powercfg /devicedisablewake "$($nic.InterfaceDescription)" 2>$null | Out-Null
        }
    }

    # Disarm by WHITELIST, not by name pattern: anything still armed that is not a
    # NIC we deliberately chose gets turned off. This catches HID devices, USB
    # hubs, touchpads and Bluetooth radios without depending on English device
    # names — powercfg reports localized names, so a 'mouse|keyboard' regex would
    # silently miss them on a non-English Windows and leave the loop in place.
    foreach ($d in @(& powercfg /devicequery wake_armed 2>$null)) {
        $name = "$d".Trim()
        if (-not $name) { continue }
        if ($wakeNames -notcontains $name) {
            & powercfg /devicedisablewake "$name" 2>$null | Out-Null
            Log "  Disarmed wake source: $name"
        }
    }

    # Wake timers let Windows Update and scheduled maintenance wake the host on
    # their own schedule. The operator's magic packet should be the only trigger.
    # Applied to EVERY scheme, so switching power plans can't quietly restore them.
    $schemes = Get-PowerSchemeGuids
    foreach ($g in $schemes) {
        & powercfg /setacvalueindex $g SUB_SLEEP RTCWAKE 0 2>$null | Out-Null
        & powercfg /setdcvalueindex $g SUB_SLEEP RTCWAKE 0 2>$null | Out-Null
    }
    & powercfg /setactive SCHEME_CURRENT 2>$null | Out-Null
    Log "  Wake timers disabled across $($schemes.Count) power scheme(s)."

    # The one part of WoL no script can configure. On many desktops the NIC loses
    # standby power until these are changed, and WoL fails no matter what Windows
    # is told — so name them rather than leaving the operator guessing.
    if (@($wakeNics | Where-Object { $_.Status -eq 'Up' }).Count -eq 0) {
        Warn "  No wake-capable NIC currently has a link. Connect the cable, then re-run."
    }
    Log "  If the host still won't wake, check firmware setup (names vary by vendor):"
    Log "    'Wake on LAN' / 'Power On By PCI-E' / 'PME Event Wake'  -> Enabled"
    Log "    'ErP' / 'EuP Ready'                                     -> Disabled"
    Log "    'Deep Sleep' / 'Deep Sx'                                -> Disabled"
}

# ── Keep WSL2 alive (Layer 1: distro + VM don't auto-idle-shutdown) ───────────
function Set-WslGlobalConfig {
    $wslconfig = Join-Path $env:USERPROFILE '.wslconfig'
    $instanceWant = 'instanceIdleTimeout=-1'
    $vmWant = 'vmIdleTimeout=-1'

    if (-not (Test-Path $wslconfig)) {
        $content = "[general]`r`n$instanceWant`r`n`r`n[wsl2]`r`n$vmWant`r`n"
        Set-Content -Path $wslconfig -Value $content -Encoding ASCII -NoNewline
        Log "Wrote $wslconfig"
        return
    }

    # Normalize idempotently: drop every existing instance/VM timeout line,
    # ensure both sections exist, then insert one unambiguous value under each.
    # Keep comments on their own lines: malformed .wslconfig files are silently
    # ignored by WSL, so generated values intentionally have no inline comments.
    $lines = @(Get-Content $wslconfig | Where-Object {
        $_ -notmatch '^[ \t]*(?:instanceIdleTimeout|vmIdleTimeout)[ \t]*='
    })
    if (-not ($lines -match '^[ \t]*\[general\][ \t]*$')) { $lines = @('[general]') + $lines }
    if (-not ($lines -match '^[ \t]*\[wsl2\][ \t]*$')) { $lines = @('[wsl2]') + $lines }
    $out = New-Object System.Collections.Generic.List[string]
    $instanceInserted = $false
    $vmInserted = $false
    foreach ($l in $lines) {
        $out.Add($l)
        if (-not $instanceInserted -and $l -match '^[ \t]*\[general\][ \t]*$') {
            $out.Add($instanceWant)
            $instanceInserted = $true
        }
        if (-not $vmInserted -and $l -match '^[ \t]*\[wsl2\][ \t]*$') {
            $out.Add($vmWant)
            $vmInserted = $true
        }
    }
    [System.IO.File]::WriteAllText($wslconfig, ($out -join "`r`n") + "`r`n", [System.Text.Encoding]::ASCII)
    Log "Normalized .wslconfig (distro + VM idle shutdown disabled)."
}

# ── Install WSL2 + distro (OOBE-free via wsl --import) ─────────────────────────
function Test-DistroInstalled {
    # `wsl -l -q` lists installed distros (UTF-16 output; normalize).
    try {
        $list = & wsl.exe -l -q 2>$null
        if ($LASTEXITCODE -ne 0) { return $false }
        $names = ($list -join "`n") -replace "`0", '' -split "`r?`n" | ForEach-Object { $_.Trim() }
        return ($names -contains $script:Distro)
    } catch {
        return $false
    }
}

function Test-WslPlatformReady {
    # True when the WSL2 platform itself is installed and usable, independent of
    # whether any distro is registered. `wsl --status` exits 0 once the optional
    # Windows components + kernel are present and errors on a machine where WSL
    # was never enabled — the discriminator we use to decide whether we still owe
    # a feature-enable + reboot before `wsl --import` can work.
    try {
        & wsl.exe --status *> $null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

function Enable-WslPlatform {
    # Turn on the WSL2 platform WITHOUT installing a distro and WITHOUT any
    # interactive first-run. On a machine that already has WSL (the reinstall
    # case) this is a fast no-op; on a fresh machine it enables the optional
    # Windows features and typically needs one reboot before `wsl --import` works.
    # Exit code is intentionally not treated as fatal — on an already-enabled
    # host this can return non-zero ("nothing to do") yet WSL is fine; the caller
    # re-checks Test-WslPlatformReady to decide what actually happened.
    Log "Ensuring the WSL2 platform is enabled (wsl --install --no-distribution)..."
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & wsl.exe --install --no-distribution 2>&1 | ForEach-Object { Write-Host $_ }
    } finally {
        $ErrorActionPreference = $prevEAP
    }
}

function Import-Distro {
    # Register the distro from an Ubuntu rootfs tarball. `wsl --import` performs
    # NO OOBE (unlike `wsl --install` / .wsl registration), so there is no hung
    # "Waiting for OOBE command..." and no interactive username prompt — we land
    # as root and create the user ourselves in Initialize-LinuxUser.
    if (Test-DistroInstalled) {
        if ($script:Reinstall) {
            Warn "-Reinstall: unregistering existing '$script:Distro' — this ERASES that distro's data."
            & wsl.exe --unregister $script:Distro 2>&1 | ForEach-Object { Write-Host $_ }
            if ($LASTEXITCODE -ne 0) { Fail "Could not unregister existing '$script:Distro' (exit $LASTEXITCODE)." }
        } else {
            Log "$script:Distro already registered — skipping import. (Pass -Reinstall to wipe and recreate.)"
            return
        }
    }

    New-Item -ItemType Directory -Path $script:InstallLocation -Force | Out-Null

    # Cache the rootfs under ProgramData so a resume-after-reboot (or a re-run)
    # doesn't re-download ~340 MB.
    if (-not (Test-Path $StateDir)) { New-Item -ItemType Directory -Path $StateDir -Force | Out-Null }
    $tarball = Join-Path $StateDir 'ubuntu-wsl-rootfs.tar.gz'
    if (Test-Path $tarball) {
        Log "Using cached rootfs at $tarball"
    } else {
        Log "Downloading Ubuntu WSL rootfs (~340 MB):"
        Log "  $script:RootfsUrl"
        $oldProgress = $ProgressPreference
        $ProgressPreference = 'SilentlyContinue'   # Invoke-WebRequest's per-byte progress bar is glacial
        try {
            Invoke-WebRequest -Uri $script:RootfsUrl -OutFile $tarball -UseBasicParsing
        } catch {
            if (Test-Path $tarball) { Remove-Item $tarball -Force -ErrorAction SilentlyContinue }  # don't cache a partial file
            Fail "Failed to download rootfs from $script:RootfsUrl`n  $_"
        } finally {
            $ProgressPreference = $oldProgress
        }
        Log "  Saved to $tarball"
    }

    Log "Importing '$script:Distro' into $script:InstallLocation (WSL2, no OOBE)..."
    & wsl.exe --import $script:Distro $script:InstallLocation $tarball --version 2 2>&1 | ForEach-Object { Write-Host $_ }
    if ($LASTEXITCODE -ne 0) {
        Fail "'wsl --import' failed (exit $LASTEXITCODE). Check the rootfs file and that WSL2 is enabled (wsl --status)."
    }
    & wsl.exe --set-default $script:Distro 2>&1 | Out-Null
    Log "Imported and set '$script:Distro' as the default WSL distro."

    Initialize-LinuxUser
}

function Initialize-LinuxUser {
    # Create the non-root Linux user that Phase B (linux-setup.sh) requires, with
    # passwordless sudo (SSH into the host is key-only, so no login password is
    # needed and Phase B's `sudo -v` succeeds non-interactively). Also pre-write
    # /etc/wsl.conf:
    #   [user] default=<user>  → `wsl -d <distro>` lands as that user, not root
    #   [boot] systemd=true     → systemd is PID 1 from the first boot, so Phase B
    #                             skips its enable-systemd-then-restart dance.
    # Phase B's ensure_wsl_systemd_enabled is section-aware and preserves [user].
    $u = $script:LinuxUser
    Log "Provisioning Linux user '$u' (passwordless sudo) + /etc/wsl.conf inside '$script:Distro'..."

    # Build the provisioning script as an LF-joined array. A fresh useradd leaves
    # the account with a disabled ('!') password, so password login is already
    # impossible — we add passwordless sudo on top. Single-quoted PS strings keep
    # `$u` / `''` literal for bash; only the line injecting the username is
    # double-quoted.
    $bashScript = @(
        'set -eu'
        "u='$u'"
        'if ! id -u "$u" >/dev/null 2>&1; then'
        '    useradd -m -s /bin/bash "$u"'
        'fi'
        'usermod -aG sudo "$u"'
        'printf ''%s ALL=(ALL) NOPASSWD:ALL\n'' "$u" > /etc/sudoers.d/90-gpudev'
        'chmod 0440 /etc/sudoers.d/90-gpudev'
        'printf ''[user]\ndefault=%s\n[boot]\nsystemd=true\n'' "$u" > /etc/wsl.conf'
    ) -join "`n"

    # Hand the script to root's bash as base64 rather than via stdin or a quoted
    # argument: this preserves the exact LF bytes (no PowerShell WriteLine \r\n
    # tacked on, no CRLF-checkout corruption) and the base64 alphabet has no
    # shell-special characters, so there's nothing for PowerShell->wsl.exe->bash
    # quoting to mangle.
    $b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($bashScript))
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & wsl.exe -d $script:Distro -u root -- bash -c "echo $b64 | base64 -d | bash" 2>&1 | ForEach-Object { Write-Host $_ }
    } finally {
        $ErrorActionPreference = $prevEAP
    }
    if ($LASTEXITCODE -ne 0) {
        Fail "Provisioning user '$u' inside '$script:Distro' failed (exit $LASTEXITCODE)."
    }

    # Terminate so [user]/default + [boot]/systemd take effect — the next launch
    # lands as '$u' with systemd as PID 1.
    & wsl.exe --terminate $script:Distro 2>&1 | Out-Null
    Log "User '$u' created (sudo), /etc/wsl.conf written, distro terminated to apply."
}

# ── Resume scheduling (across the WSL-install reboot) ──────────────────────────
function Register-ResumeTask {
    # The resume task needs an on-disk script to invoke after the reboot. We
    # always materialize a stable copy at C:\ProgramData\gpudev\windows-setup.ps1,
    # regardless of how the script was launched:
    #   - `iex (irm URL)`  → $PSCommandPath is $null → download fresh
    #   - `iwr -OutFile`+`.\` → $PSCommandPath is set → copy that file
    if (-not (Test-Path $StateDir)) { New-Item -ItemType Directory -Path $StateDir -Force | Out-Null }
    $stableCopy = Join-Path $StateDir 'windows-setup.ps1'

    if ($PSCommandPath -and (Test-Path $PSCommandPath)) {
        $here = (Resolve-Path $PSCommandPath).Path
        if ($here -ne $stableCopy) {
            Copy-Item -Path $here -Destination $stableCopy -Force
            Log "Copied script to stable path: $stableCopy"
        }
    } else {
        # Running via `iex (irm ...)` — no local file. Download a stable copy.
        $url = 'https://raw.githubusercontent.com/rleyvasal/gpudev/main/windows-setup.ps1'
        Invoke-WebRequest -Uri $url -OutFile $stableCopy -UseBasicParsing
        Log "Downloaded stable copy of script to: $stableCopy"
    }

    $ps = (Get-Command powershell.exe).Source
    $action = New-ScheduledTaskAction -Execute $ps `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$stableCopy`" -Resume"
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable
    Register-ScheduledTask -TaskName $ResumeTaskName -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings -Force | Out-Null
    Log "Registered resume task '$ResumeTaskName' (runs at next logon)."
}

function Unregister-ResumeTask {
    if (Get-ScheduledTask -TaskName $ResumeTaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $ResumeTaskName -Confirm:$false
        Log "Removed resume task."
    }
}

# ── Boot task (Layer 2: wake WSL at logon) ─────────────────────────────────────
function Register-BootTask {
    # CRITICAL: this task runs as the OPERATOR, not SYSTEM. WSL distros are
    # registered per Windows user (in HKCU), so a SYSTEM task has its own empty
    # WSL registry and literally cannot start the operator's distro — that was the
    # "nothing comes back after a reboot" bug (the SYSTEM task returned exit -1).
    # Running as the operator with LogonType Interactive is the same context in
    # which `wsl` works by hand, and needs NO stored password to register.
    #
    # It triggers AtLogon, so for unattended reboot recovery Windows must be set to
    # AUTOLOGIN this operator (a manual step Phase A can't do — it needs the
    # password; see the handoff + Test-Autologon). Once the VM is up, Phase B's
    # systemd ([boot] systemd=true) auto-starts docker / ssh / gpudev-tunnel.
    $user = $env:USERNAME
    $action = New-ScheduledTaskAction -Execute 'wsl.exe' -Argument "-d $script:Distro --exec /bin/true"
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
        -RestartInterval (New-TimeSpan -Minutes 1) -RestartCount 3
    try {
        Register-ScheduledTask -TaskName $BootTaskName -Action $action -Trigger $trigger `
            -Principal $principal -Settings $settings -Force | Out-Null
        Log "Registered boot task '$BootTaskName' (wakes WSL as '$user' at logon)."
    } catch {
        Warn "Could not register boot task: $_"
        Warn "WSL won't auto-start on reboot; bring it up manually with: wsl -d $script:Distro"
    }
}

# ── Keepalive task (Layer 3: re-wake WSL if it exits mid-session) ──────────────
function Register-KeepaliveTask {
    # Belt-and-suspenders for failure modes Layers 1+2 don't catch:
    #   - WSL VM crash
    #   - Background Windows update restarting wslservice
    #   - Memory pressure killing the WSL VM
    # Runs every 5 min and invokes /bin/true in the distro. When WSL is healthy
    # this is a cheap no-op; when it is down, wsl.exe starts it. Using wsl.exe
    # directly avoids a fragile nested PowerShell -Command action (whose quoting
    # previously failed with LastTaskResult=1).
    # Runs as the OPERATOR (LogonType Interactive) for the same per-user reason as
    # the boot task — a SYSTEM instance can't see or wake the operator's distro.
    # With autologin the operator is logged on after boot, so this fires; the
    # AtLogon boot task covers the brief pre-logon window.
    $action = New-ScheduledTaskAction -Execute 'wsl.exe' `
        -Argument "-d $script:Distro --exec /bin/true"
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(5) `
        -RepetitionInterval (New-TimeSpan -Minutes 5)
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
        -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 2)
    try {
        Register-ScheduledTask -TaskName $KeepaliveTaskName -Action $action -Trigger $trigger `
            -Principal $principal -Settings $settings -Force | Out-Null
        Start-ScheduledTask -TaskName $KeepaliveTaskName
        # Give the first run a moment to finish so Phase A can report a real
        # action result instead of merely confirming that a task object exists.
        $deadline = (Get-Date).AddSeconds(30)
        do {
            Start-Sleep -Milliseconds 250
            $task = Get-ScheduledTask -TaskName $KeepaliveTaskName
        } while ($task.State -eq 'Running' -and (Get-Date) -lt $deadline)

        $info = Get-ScheduledTaskInfo -TaskName $KeepaliveTaskName
        if ($task.State -eq 'Running') {
            Log "Registered keepalive task '$KeepaliveTaskName'; first wake is still starting."
        } elseif ($info.LastTaskResult -eq 0) {
            Log "Registered and verified keepalive task '$KeepaliveTaskName' (re-wakes WSL every 5 min if it exits)."
        } else {
            Warn "Keepalive task registered but its first run failed (result $($info.LastTaskResult))."
        }
    } catch {
        Warn "Could not register keepalive task: $_"
        Warn "WSL won't be auto-re-woken on mid-session crash; impact is the gpudev tunnel goes down until next Windows reboot."
    }
}

# ── Host clock (detected and reported; Phase A does not change it) ────────────

# A GPU host that sleeps between sessions free-runs on the CMOS oscillator while
# it is down. If the time service never syncs, the clock drifts until SSH, TLS
# and the Cloudflare tunnel start failing with errors that name none of those
# things. Phase A reports this rather than fixing it: the time zone is the
# operator's call, and silently re-pointing a host's clock is not our business.
#
# Every signal here is language-independent — a DWORD, a service enum, or a
# registry string. Deliberately NOT parsed from `w32tm /query /status`, whose
# labels are localized and would make this check pass on a broken non-English
# host (the same trap that made the wake-source check unreliable).
function Get-ClockIssues {
    $issues = @()

    try {
        $tz = Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\TimeZoneInformation' -ErrorAction Stop
        if ([int]$tz.DynamicDaylightTimeDisabled -eq 1) {
            $issues += "daylight saving is disabled for '$($tz.TimeZoneKeyName)' — local time is wrong for half the year"
        }
    } catch {
        $issues += "the time zone configuration could not be read"
    }

    try {
        $svc = Get-Service W32Time -ErrorAction Stop
        if ($svc.Status -ne 'Running') {
            $issues += "the Windows Time service is $($svc.Status) — the clock is not being synchronised"
        }
        if ($svc.StartType -ne 'Automatic') {
            $issues += "the Windows Time service start type is $($svc.StartType) — it may not run after a reboot"
        }
    } catch {
        $issues += "the Windows Time service could not be queried"
    }

    try {
        $type = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Services\W32Time\Parameters' `
            -Name Type -ErrorAction Stop).Type
        if ($type -eq 'NoSync') {
            $issues += "time synchronisation is set to NoSync — the clock free-runs on the CMOS oscillator"
        }
    } catch { }

    # With the 0x9 (SpecialInterval) peer flag that Show-ClockHelp recommends —
    # and that most published w32tm recipes use — W32Time ignores Min/MaxPoll-
    # Interval and polls on SpecialPollInterval ALONE. Windows ships 16384 s
    # (4.5 h) here, which is long enough that the service goes stale between
    # polls: the peers sit in 'Pending', root dispersion freezes, and the status
    # reads 'not synchronized' even while the clock is perfectly accurate. That
    # looks like a broken sync and sends you chasing the wrong thing.
    try {
        $spi = [int](Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Services\W32Time\TimeProviders\NtpClient' `
            -Name SpecialPollInterval -ErrorAction Stop).SpecialPollInterval
        if ($spi -gt 3600) {
            $issues += "the NTP poll interval is $spi s ($([math]::Round($spi / 3600, 1)) h) — too long to hold a synchronised clock"
        }
    } catch { }

    return $issues
}

function Show-ClockHelp {
    $zone = "$(& tzutil /g 2>$null)".Trim() -replace '_dstoff$', ''
    if (-not $zone) { $zone = '<your time zone>' }
    Write-Host ""
    Write-Host "IMPORTANT — fix the host clock (run as Administrator):" -ForegroundColor Yellow
    Write-Host "  This host sleeps between sessions and free-runs on the CMOS oscillator"
    Write-Host "  while it is down. Once the clock drifts far enough, SSH, TLS and the"
    Write-Host "  Cloudflare tunnel fail with errors that mention none of those things."
    Write-Host ""
    Write-Host "    1. Re-enable daylight saving for the current zone. Selecting the same"
    Write-Host "       zone WITHOUT the '_dstoff' suffix clears it — that variant pins the"
    Write-Host "       host to standard time all year, so it runs an hour off in summer:"
    Write-Host "         tzutil /s `"$zone`""
    Write-Host ""
    Write-Host "    2. Make the time service start automatically and poll real NTP servers."
    Write-Host "       Set SpecialPollInterval too: the '0x9' flag means SpecialInterval, so"
    Write-Host "       W32Time polls on THAT value alone and ignores Min/MaxPollInterval."
    Write-Host "       It ships at 16384 s (4.5 h), long enough that the service goes stale"
    Write-Host "       between polls and reports 'not synchronized' while the clock is fine:"
    Write-Host "         Set-Service W32Time -StartupType Automatic"
    Write-Host "         Start-Service W32Time"
    Write-Host "         w32tm /config /manualpeerlist:`"time.windows.com,0x9 pool.ntp.org,0x9`" /syncfromflags:manual /update"
    Write-Host "         Set-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Services\W32Time\TimeProviders\NtpClient' ``"
    Write-Host "             -Name SpecialPollInterval -Value 3600 -Type DWord"
    Write-Host "         Restart-Service W32Time"
    Write-Host "         w32tm /resync /force"
    Write-Host ""
    Write-Host "  Verify — offset well under a second, and the service vouching for itself:"
    Write-Host "         w32tm /stripchart /computer:time.windows.com /samples:2 /dataonly"
    Write-Host "         w32tm /query /status   -> Leap Indicator: 0(no warning), Stratum: 2-4"
    Write-Host "         w32tm /query /peers    -> every peer 'State: Active', not 'Pending'"
    Write-Host ""
    Write-Host "  'Last Successful Sync Time: unspecified' means the host has NEVER synced."
    Write-Host "  A sync time that stops advancing means the poll interval is too long."
    Write-Host ""
    Write-Host "  Unrelated log noise on a WSL2 host: 'VMICTimeProvider ... not supported'"
    Write-Host "  (event 158) every ~17 min is Hyper-V's GUEST time provider, registered"
    Write-Host "  because WSL2 enables the hypervisor. It is harmless on a physical host but"
    Write-Host "  buries real errors. Silence it with:"
    Write-Host "         Set-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Services\W32Time\TimeProviders\VMICTimeProvider' ``"
    Write-Host "             -Name Enabled -Value 0 -Type DWord; Restart-Service W32Time"
}

# ── Autologin (manual prerequisite for unattended reboot recovery) ─────────────
function Test-Autologon {
    # The boot/keepalive tasks run as the operator and trigger at LOGON, so WSL
    # only auto-starts after a reboot if Windows auto-logs-in the operator. Phase A
    # can't configure that safely (it needs the account password / an MSA->local
    # conversion), so it DETECTS it and prints instructions if missing.
    try {
        $w = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon' -ErrorAction Stop
        return ("$($w.AutoAdminLogon)" -eq '1')
    } catch {
        return $false
    }
}

function Show-AutologinHelp {
    $u = $env:USERNAME
    Write-Host ""
    Write-Host "IMPORTANT — enable Windows AUTOLOGIN so WSL comes back after an unattended reboot:" -ForegroundColor Yellow
    Write-Host "  The boot/keepalive tasks run as '$u' (SYSTEM can't see your WSL distro) and"
    Write-Host "  fire at LOGON. Without autologin, nothing starts until someone signs in."
    Write-Host ""
    Write-Host "    1. If '$u' is a Microsoft account, convert it to a LOCAL account first"
    Write-Host "       (so you're not storing your Microsoft password):"
    Write-Host "         Settings > Accounts > Your info > 'Sign in with a local account instead'"
    Write-Host "         Keep the same username; set a simple local password."
    Write-Host "    2. Enable autologin with Sysinternals Autologon (stores the password as an"
    Write-Host "       encrypted LSA secret, not plaintext):"
    Write-Host "         https://learn.microsoft.com/sysinternals/downloads/autologon"
    Write-Host "         Autologon.exe -accepteula $u . <local-password>"
    Write-Host ""
    Write-Host "  Verify:  (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon').AutoAdminLogon  → 1"
}

# ── Health check ───────────────────────────────────────────────────────────────
function Invoke-HealthCheck {
    Step "Phase A health check (Windows-side only)"
    Log "  Distro:                    $script:Distro"
    Log "  Linux user:                $script:LinuxUser"
    Write-Host ""

    if (Test-Path (Join-Path $env:SystemRoot 'System32\nvidia-smi.exe')) {
        Log "  NVIDIA driver (Windows):   OK"
    } else { Warn "  NVIDIA driver (Windows):   MISSING (Phase B GPU verification will fail)" }

    if (Test-DistroInstalled) {
        Log "  WSL2 distro installed:     OK ($script:Distro)"
        # Confirm the user we provisioned actually exists (cheap; relaunches the
        # VM if it was terminated, which is fine).
        & wsl.exe -d $script:Distro -u root -- id -u $script:LinuxUser *> $null
        if ($LASTEXITCODE -eq 0) {
            Log "  Linux user ($script:LinuxUser): OK (default user, sudo)"
        } else {
            Warn "  Linux user ($script:LinuxUser): NOT found — re-run with -Reinstall"
        }
    } else { Warn "  WSL2 distro installed:     MISSING" }

    if (Get-ScheduledTask -TaskName $BootTaskName -ErrorAction SilentlyContinue) {
        Log "  Boot task (wake on boot):  OK ($BootTaskName)"
    } else { Warn "  Boot task (wake on boot):  not registered" }

    $keepaliveTask = Get-ScheduledTask -TaskName $KeepaliveTaskName -ErrorAction SilentlyContinue
    if ($keepaliveTask) {
        $keepaliveInfo = Get-ScheduledTaskInfo -TaskName $KeepaliveTaskName
        if ($keepaliveTask.State -eq 'Disabled') {
            Warn "  Keepalive task (5 min):    DISABLED"
        } elseif ($keepaliveTask.State -eq 'Running') {
            Log "  Keepalive task (5 min):    OK (wake in progress)"
        } elseif ($keepaliveInfo.LastRunTime.Year -gt 1900 -and $keepaliveInfo.LastTaskResult -ne 0) {
            Warn "  Keepalive task (5 min):    FAILED (result $($keepaliveInfo.LastTaskResult))"
        } else {
            Log "  Keepalive task (5 min):    OK ($KeepaliveTaskName)"
        }
    } else { Warn "  Keepalive task (5 min):    not registered" }

    $autologin = Test-Autologon
    if ($autologin) {
        Log "  Autologin (unattended boot): OK"
    } else { Warn "  Autologin (unattended boot): NOT set — WSL won't auto-start after a reboot (manual step below)" }

    $wslconfig = Join-Path $env:USERPROFILE '.wslconfig'
    if (Test-Path $wslconfig) {
        $wslconfigText = Get-Content $wslconfig -Raw
        $hasInstanceTimeout = $wslconfigText -match '(?m)^[ \t]*instanceIdleTimeout[ \t]*=[ \t]*-1[ \t]*$'
        $hasVmTimeout = $wslconfigText -match '(?m)^[ \t]*vmIdleTimeout[ \t]*=[ \t]*-1[ \t]*$'
        if ($hasInstanceTimeout -and $hasVmTimeout) {
            Log "  .wslconfig (distro + VM idle): OK"
        } else {
            Warn "  .wslconfig (distro + VM idle): INCOMPLETE"
        }
    } else { Warn "  .wslconfig (distro + VM idle): MISSING" }

    $clockIssues = @(Get-ClockIssues)
    if ($clockIssues) {
        Warn "  Host clock:                NEEDS ATTENTION"
        foreach ($i in $clockIssues) { Warn "                             - $i" }
    } else {
        Log "  Host clock:                OK (zone honours DST; time service automatic)"
    }

    # Idle sleep. Report BOTH the visible timeout and the hidden unattended one:
    # a host that sleeps ~2 min after every remote wake looks impossible from the
    # Settings UI, which only ever shows the first of these.
    $sleepSub = '238c9fa8-0aad-41ed-83f4-97be242c8f20'
    $activeScheme = ((& powercfg /getactivescheme) -replace '.*GUID: ' -replace ' .*').Trim()
    $schemeKey = "HKLM:\SYSTEM\CurrentControlSet\Control\Power\User\PowerSchemes\$activeScheme\$sleepSub"
    $standby  = (Get-ItemProperty "$schemeKey\29f6c1db-86da-48c5-9fdb-f2b67b1f44da" -ErrorAction SilentlyContinue).ACSettingIndex
    $unattend = (Get-ItemProperty "$schemeKey\7bc4a2f9-d8fc-4469-b07b-33eb785aaca0" -ErrorAction SilentlyContinue).ACSettingIndex
    if ($standby -eq 0) {
        Log "  Idle sleep (AC):           OK (never)"
    } else { Warn "  Idle sleep (AC):           $standby s — the host will sleep on its own" }
    if ($unattend -eq 0) {
        Log "  Unattended sleep (AC):     OK (never)"
    } elseif ($null -eq $unattend) {
        Warn "  Unattended sleep (AC):     NOT SET — defaults to 120 s, so the host will"
        Warn "                             sleep ~2 min after every wake-on-LAN wake."
    } else { Warn "  Unattended sleep (AC):     $unattend s — the host will sleep after a remote wake" }

    # Wake-on-LAN. A wake source that isn't a NIC is not a cosmetic problem: it
    # wakes the host within seconds of `gpudev power sleep`, so report it as a
    # failure. Classify by comparing against the real adapter list rather than by
    # matching device-name text, which is localized on non-English Windows.
    $nicNames = @(Get-NetAdapter -Physical -ErrorAction SilentlyContinue |
        Where-Object { $_.InterfaceDescription -notmatch 'Bluetooth|Virtual' } |
        ForEach-Object { $_.InterfaceDescription })
    $armed = @(& powercfg /devicequery wake_armed 2>$null |
        ForEach-Object { "$_".Trim() } | Where-Object { $_ })
    $spurious = @($armed | Where-Object { $nicNames -notcontains $_ })
    $armedNics = @($armed | Where-Object { $nicNames -contains $_ })

    if ($spurious) {
        Warn "  Wake-on-LAN:               SPURIOUS SOURCES ARMED — $($spurious -join ', ')"
        Warn "                             The host will wake seconds after it sleeps."
    } elseif ($armedNics) {
        Log "  Wake-on-LAN:               OK ($($armedNics -join ', '))"
        foreach ($nic in @(Get-NetAdapter -Physical -ErrorAction SilentlyContinue |
                Where-Object { $armedNics -contains $_.InterfaceDescription })) {
            if ($nic.Status -eq 'Up') {
                Log "    Send the magic packet to MAC $($nic.MacAddress) ($($nic.Name))"
            } else {
                Warn "    $($nic.Name) is $($nic.Status) — WoL needs an active link."
            }
        }
    } else {
        Warn "  Wake-on-LAN:               no wake source armed — the host cannot be woken remotely."
    }

    if (-not $autologin) { Show-AutologinHelp }
    if ($clockIssues) { Show-ClockHelp }

    Write-Host ""
    Write-Host "Phase A complete — Windows is ready for gpudev." -ForegroundColor Green
    Write-Host ""
    Write-Host "═══════════════════════════════════════════════════════════════════" -ForegroundColor Cyan
    Write-Host " NEXT: Phase B — run linux-setup.sh inside WSL" -ForegroundColor Cyan
    Write-Host "═══════════════════════════════════════════════════════════════════" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  1. (Recommended) Set a login password for '$script:LinuxUser'."
    Write-Host "     The account is created with passwordless sudo, so Phase B works"
    Write-Host "     without this — but '$script:LinuxUser' has no password yet, and a"
    Write-Host "     locked account can't set its own, so do it AS ROOT:"
    Write-Host "       wsl -d $script:Distro -u root -- passwd $script:LinuxUser"
    Write-Host ""
    Write-Host "  2. Open WSL — it lands straight at a shell as '$script:LinuxUser'"
    Write-Host "     (no first-run prompt; the user is already created with sudo):"
    Write-Host "       wsl -d $script:Distro"
    Write-Host ""
    Write-Host "  3. Run the bootstrap:"
    Write-Host "       bash <(curl -fsSL https://raw.githubusercontent.com/rleyvasal/gpudev/main/linux-setup.sh)"
    Write-Host ""
    Write-Host "  4. linux-setup.sh will prompt you for:"
    Write-Host "       - your Cloudflare domain  (e.g. example.com)"
    Write-Host "       - your admin SSH public key"
    Write-Host ""
    Write-Host "  5. When 'cloudflared' prints an authorization URL, open it in"
    Write-Host "     a browser and authorize the tunnel for your domain."
    Write-Host ""
    Write-Host "  Phase B installs Docker + cloudflared + the gpudev base image"
    Write-Host "  + the systemd units for sshd / docker / tunnel."
    Write-Host "  Approximate runtime: 15-25 min on first run (most of it the"
    Write-Host "  base image build)."
    Write-Host ""
}

# ── Main ────────────────────────────────────────────────────────────────────────
function Main {
    Write-Host ""
    Write-Host "gpudev Windows host setup — Phase A (Windows prep only)" -ForegroundColor Cyan

    Assert-Admin

    if (-not $Resume) {
        Step "Step 1: Prerequisites"
        Assert-WslSupported
        Assert-LinuxUserName
        Log "Distro:                  $script:Distro"
        Log "Linux user:              $script:LinuxUser (passwordless sudo, default user)"
        Save-State

        Step "Step 2: NVIDIA driver check"
        Test-NvidiaDriver

        Step "Step 3: WSL kernel update"
        Update-WslKernel

        Step "Step 4: Windows power settings + wake-on-LAN"
        Set-PowerSettings
        Set-WakeOnLan

        Step "Step 5: WSL2 global config"
        Set-WslGlobalConfig
    } else {
        Load-State
        Write-Host "Resuming Phase A after the platform-enable reboot..." -ForegroundColor Cyan
    }

    # Step 6 runs in BOTH paths: on a fresh machine the import happens AFTER the
    # platform-enable reboot (in the resume pass), since `wsl --import` needs the
    # WSL2 platform ready first.
    Step "Step 6: Ensure WSL2 platform + import $script:Distro (OOBE-free)"
    if (Test-WslPlatformReady) {
        Import-Distro       # handles already-registered / -Reinstall / fresh import + user
    } else {
        Enable-WslPlatform
        if (Test-WslPlatformReady) {
            Import-Distro
        } else {
            Register-ResumeTask
            Write-Host ""
            Write-Host "WSL2 platform enabled. A reboot is required to finish." -ForegroundColor Yellow
            Write-Host "After you log back in, Phase A resumes automatically and imports $script:Distro." -ForegroundColor Yellow
            if ($SkipReboot) {
                Warn "SkipReboot set — reboot manually, then Phase A resumes at logon (or run -Resume)."
                return
            }
            Write-Host "Rebooting in 10 seconds (Ctrl+C to cancel)..."
            Start-Sleep -Seconds 10
            Restart-Computer -Force
            return
        }
    }

    Step "Step 7: Register WSL boot task"
    Register-BootTask

    Step "Step 8: Register WSL keepalive task (Layer 3)"
    Register-KeepaliveTask

    Unregister-ResumeTask
    Remove-State

    Invoke-HealthCheck
}

Main
