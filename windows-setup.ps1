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
      4. Configure Windows power settings (no auto-sleep on AC).
      5. Write %USERPROFILE%\.wslconfig (vmIdleTimeout=-1 so the WSL2 VM stays up).
      6. Ensure the WSL2 platform is enabled (--no-distribution; reboots+resumes
         once only if the feature was just turned on), then `wsl --import` the
         distro from an Ubuntu rootfs tarball (no OOBE) and provision the Linux
         user + /etc/wsl.conf.
      7. Register a Windows boot scheduled task that wakes the distro at startup.
      8. Register a periodic keepalive task (Layer 3) that re-wakes the distro
         if it exits between Windows boots (WSL crash, background update, etc.).

    Done by Phase A (new — previously deferred to Ubuntu's hung OOBE):
      - Linux user creation — -LinuxUser (default 'gpudev') is created with
        passwordless sudo (SSH into the host is key-only, so no login password).
      - /etc/wsl.conf — [user] default=<user> + [boot] systemd=true, so the very
        first `wsl -d <distro>` lands as that user with systemd already PID 1.
        Phase B's wsl.conf writer is section-aware and preserves both.

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
function Set-PowerSettings {
    # Never auto-sleep / hibernate / spin down on AC — a GPU host must stay up.
    # Hibernate is turned OFF so an explicit `gpudev power sleep` performs S3
    # sleep (and wakes cleanly) rather than hibernating.
    Log "Configuring Windows power plan (no automatic sleep on AC)..."
    & powercfg /change standby-timeout-ac 0   | Out-Null
    & powercfg /change hibernate-timeout-ac 0 | Out-Null
    & powercfg /change disk-timeout-ac 0      | Out-Null
    & powercfg /change monitor-timeout-ac 10  | Out-Null
    & powercfg /hibernate off                 2>$null | Out-Null
    try {
        & powercfg /setactive SCHEME_MIN 2>$null | Out-Null   # High performance
        Log "  Active plan: High performance; sleep/hibernate/disk timeouts disabled."
    } catch {
        Warn "Could not set High performance plan (modern-standby system?); timeouts still disabled."
    }
}

# ── Keep WSL2 alive (Layer 1: VM doesn't auto-idle-shutdown) ──────────────────
function Set-WslGlobalConfig {
    $wslconfig = Join-Path $env:USERPROFILE '.wslconfig'
    $want = 'vmIdleTimeout=-1  # gpudev: keep the WSL2 VM alive between SSH sessions'

    if (-not (Test-Path $wslconfig)) {
        Set-Content -Path $wslconfig -Value "[wsl2]`r`n$want" -Encoding ASCII
        Log "Wrote $wslconfig"
        return
    }

    # Normalize idempotently: drop EVERY existing vmIdleTimeout line (collapsing any
    # duplicates left by earlier runs — the previous version short-circuited on a
    # 'gpudev' marker and never cleaned them, so `wsl` warned about a duplicate key
    # on stderr and aborted the install), ensure a [wsl2] section, then add exactly
    # one vmIdleTimeout right after it. Other settings are preserved.
    $lines = @(Get-Content $wslconfig | Where-Object { $_ -notmatch '^[ \t]*vmIdleTimeout[ \t]*=' })
    if (-not ($lines -match '^\[wsl2\]')) { $lines = @('[wsl2]') + $lines }
    $out = New-Object System.Collections.Generic.List[string]
    $inserted = $false
    foreach ($l in $lines) {
        $out.Add($l)
        if (-not $inserted -and $l -match '^\[wsl2\]') { $out.Add($want); $inserted = $true }
    }
    [System.IO.File]::WriteAllText($wslconfig, ($out -join "`r`n") + "`r`n", [System.Text.Encoding]::ASCII)
    Log "Normalized .wslconfig (single vmIdleTimeout=-1 under [wsl2])."
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

# ── Boot task (Layer 2: wake WSL at every Windows startup) ─────────────────────
function Register-BootTask {
    # Phase B's systemd (enabled via /etc/wsl.conf [boot] systemd=true that
    # Phase B writes) auto-starts docker / ssh / gpudev-tunnel as soon as the
    # WSL VM is up. This task just makes sure the VM is up at Windows boot.
    # `wsl --exec /bin/true` is the cheapest possible "wake it" command.
    $arg = "-d $script:Distro --exec /bin/true"
    $action = New-ScheduledTaskAction -Execute 'wsl.exe' -Argument $arg
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
        -RestartInterval (New-TimeSpan -Minutes 1) -RestartCount 3
    try {
        Register-ScheduledTask -TaskName $BootTaskName -Action $action -Trigger $trigger `
            -Principal $principal -Settings $settings -Force | Out-Null
        Log "Registered boot task '$BootTaskName' (wakes WSL at Windows startup)."
    } catch {
        Warn "Could not register boot task as SYSTEM: $_"
        Warn "WSL won't auto-start on reboot; bring it up manually with: wsl -d $script:Distro"
    }
}

# ── Keepalive task (Layer 3: re-wake WSL if it exits mid-session) ──────────────
function Register-KeepaliveTask {
    # Belt-and-suspenders for failure modes Layers 1+2 don't catch:
    #   - WSL VM crash
    #   - Background Windows update restarting wslservice
    #   - Memory pressure killing the WSL VM
    # Runs every 5 min, checks if the distro is in `wsl -l --running --quiet`,
    # and wakes it if not. No-op when WSL is healthy; cheap (~ms) when it is.
    $cmd = @"
`$running = & wsl.exe -l --running --quiet 2>`$null
`$names = (`$running -join "``n") -replace "``0", "" -split "``r?``n" | ForEach-Object { `$_.Trim() }
if (-not (`$names -contains '$script:Distro')) {
    & wsl.exe -d $script:Distro --exec /bin/true | Out-Null
}
"@
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' `
        -Argument "-NoProfile -WindowStyle Hidden -Command `"$cmd`""
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(5) `
        -RepetitionInterval (New-TimeSpan -Minutes 5)
    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
        -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 2)
    try {
        Register-ScheduledTask -TaskName $KeepaliveTaskName -Action $action -Trigger $trigger `
            -Principal $principal -Settings $settings -Force | Out-Null
        Log "Registered keepalive task '$KeepaliveTaskName' (re-wakes WSL every 5 min if it exits)."
    } catch {
        Warn "Could not register keepalive task as SYSTEM: $_"
        Warn "WSL won't be auto-re-woken on mid-session crash; impact is the gpudev tunnel goes down until next Windows reboot."
    }
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

    if (Get-ScheduledTask -TaskName $KeepaliveTaskName -ErrorAction SilentlyContinue) {
        Log "  Keepalive task (5 min):    OK ($KeepaliveTaskName)"
    } else { Warn "  Keepalive task (5 min):    not registered" }

    $wslconfig = Join-Path $env:USERPROFILE '.wslconfig'
    if ((Test-Path $wslconfig) -and ((Get-Content $wslconfig -Raw) -match 'gpudev')) {
        Log "  .wslconfig (idle=disabled): OK"
    } else { Warn "  .wslconfig (idle=disabled): MISSING" }

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

        Step "Step 4: Windows power settings"
        Set-PowerSettings

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
