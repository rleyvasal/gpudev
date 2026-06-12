#Requires -Version 5.1
<#
.SYNOPSIS
    gpudev windows-setup.ps1 — Phase A: Windows-side host preparation only.

.DESCRIPTION
    This script makes the Windows machine a reliable WSL host. It does NOT
    create the Linux user, does NOT configure /etc/wsl.conf inside the distro,
    and does NOT install gpudev components inside WSL. All of that is Phase B
    (linux-setup.sh, run from inside WSL by the operator).

    Phase A responsibilities:

      1. Verify Administrator + Windows build >= 19041.
      2. Check that the NVIDIA Windows driver is present (warn if not — WSL GPU
         passthrough requires the Windows driver, not a Linux one).
      3. Run `wsl --update` to keep the WSL kernel current.
      4. Configure Windows power settings (no auto-sleep on AC).
      5. Write %USERPROFILE%\.wslconfig (vmIdleTimeout=-1 so the WSL2 VM stays up).
      6. Install WSL2 + the chosen distro with --no-launch (auto-reboots once
         and resumes itself if the WSL feature was just enabled).
      7. Register a Windows boot scheduled task that wakes the distro at startup.
      8. Register a periodic keepalive task (Layer 3) that re-wakes the distro
         if it exits between Windows boots (WSL crash, background update, etc.).

    What's NOT done here (deliberately):
      - Linux user creation — happens on first `wsl -d <distro>` (Ubuntu's
        first-run prompt asks the operator). Pick any username; Phase B adapts.
      - /etc/wsl.conf inside the distro — Phase B writes systemd=true and
        default user, since at that point linux-setup.sh knows whoami.
      - Docker, NVIDIA toolkit, cloudflared, gpudev CLI, base image — all Phase B.

    Handoff (printed at the end as instructions):

        wsl -d Ubuntu-24.04                 # Ubuntu first-run prompts user/pass
        # inside WSL:
        bash <(curl -fsSL https://raw.githubusercontent.com/rleyvasal/gpudev/main/linux-setup.sh)

.PARAMETER Distro
    WSL distro to install. Default: "Ubuntu". Anything `wsl --install -d` accepts.

.PARAMETER SkipReboot
    Don't auto-reboot after enabling WSL2 / installing the distro. You'll have
    to reboot yourself and let the resume task pick up at logon (or re-run this
    script with -Resume).

.PARAMETER Resume
    Internal: re-entry point after the post-WSL-install reboot. Don't pass by hand.

.EXAMPLE
    # Default install (distro: Ubuntu):
    .\windows-setup.ps1

.EXAMPLE
    # Specific Ubuntu release:
    .\windows-setup.ps1 -Distro Ubuntu-24.04
#>
[CmdletBinding()]
param(
    [string]$Distro = 'Ubuntu',
    [switch]$SkipReboot,
    [switch]$Resume
)

$script:Distro = $Distro   # promote param into script scope so nested functions and scriptblock invocation both see it

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

# ── Resume state (just enough to survive the reboot) ───────────────────────────
function Save-State {
    if (-not (Test-Path $StateDir)) { New-Item -ItemType Directory -Path $StateDir -Force | Out-Null }
    $state = [ordered]@{
        Distro     = $script:Distro
        ScriptPath = $PSCommandPath
    }
    $state | ConvertTo-Json | Set-Content -Path $StateFile -Encoding UTF8
}

function Load-State {
    if (-not (Test-Path $StateFile)) {
        Fail "Resume requested but no saved state at $StateFile. Re-run without -Resume."
    }
    $s = Get-Content $StateFile -Raw | ConvertFrom-Json
    $script:Distro = $s.Distro
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

# ── Install WSL2 + distro ──────────────────────────────────────────────────────
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

function Install-Wsl {
    Log "Installing WSL2 + $script:Distro with --no-launch (no interactive first-run)..."
    # --no-launch is required: it skips Ubuntu's interactive user-creation
    # prompt, which is what Phase A is supposed to defer to the operator.
    # If --no-launch is unsupported on this Windows version we FAIL clearly
    # rather than fall back to the interactive `wsl --install`, which would
    # drop the operator into a prompt mid-script.
    # Capture stderr+stdout together and inspect $LASTEXITCODE ourselves. wsl.exe
    # writes NON-fatal warnings (e.g. a duplicate .wslconfig key) to stderr; under
    # `-ErrorActionPreference Stop` PowerShell turns any native-command stderr into
    # a terminating NativeCommandError, which aborted the install on a harmless
    # warning. Drop to Continue just for this call so only a real non-zero exit fails.
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $wslOut = & wsl.exe --install -d $script:Distro --no-launch 2>&1
    } finally {
        $ErrorActionPreference = $prevEAP
    }
    $wslOut | ForEach-Object { Write-Host $_ }
    if ($LASTEXITCODE -ne 0) {
        Fail @"
'wsl --install -d $script:Distro --no-launch' failed (exit $LASTEXITCODE).

This usually means either:
  - The --no-launch flag isn't supported on this Windows build (need recent WSL).
    Try: wsl --update   (Phase A already runs this; check the output above.)
  - The distro name is invalid. Check available distros: wsl -l --online

Do NOT work around this by running plain 'wsl --install' — that triggers
Ubuntu's interactive first-run prompt, which is exactly what Phase A defers
to you (the operator) so you can pick your own Linux username later.
"@
    }
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
    Write-Host ""

    if (Test-Path (Join-Path $env:SystemRoot 'System32\nvidia-smi.exe')) {
        Log "  NVIDIA driver (Windows):   OK"
    } else { Warn "  NVIDIA driver (Windows):   MISSING (Phase B GPU verification will fail)" }

    if (Test-DistroInstalled) {
        Log "  WSL2 distro installed:     OK ($script:Distro)"
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
    Write-Host " NEXT: Phase B — provision Linux user, then run linux-setup.sh" -ForegroundColor Cyan
    Write-Host "═══════════════════════════════════════════════════════════════════" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  1. Open WSL:"
    Write-Host "       wsl -d $script:Distro"
    Write-Host ""
    Write-Host "     Ubuntu's first-run prompt will ask you to create a Unix"
    Write-Host "     username and password. Pick any username you like — Phase B"
    Write-Host "     adapts (it uses whoami)."
    Write-Host ""
    Write-Host "  2. Once you're at the WSL shell prompt, run the bootstrap:"
    Write-Host "       bash <(curl -fsSL https://raw.githubusercontent.com/rleyvasal/gpudev/main/linux-setup.sh)"
    Write-Host ""
    Write-Host "  3. linux-setup.sh will prompt you for:"
    Write-Host "       - your Cloudflare domain  (e.g. example.com)"
    Write-Host "       - your admin SSH public key"
    Write-Host ""
    Write-Host "  4. When 'cloudflared' prints an authorization URL, open it in"
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
        Log "Distro:                  $Distro"
        Save-State

        Step "Step 2: NVIDIA driver check"
        Test-NvidiaDriver

        Step "Step 3: WSL kernel update"
        Update-WslKernel

        Step "Step 4: Windows power settings"
        Set-PowerSettings

        Step "Step 5: WSL2 global config"
        Set-WslGlobalConfig

        Step "Step 6: Install WSL2 + $script:Distro"
        if (Test-DistroInstalled) {
            Log "$script:Distro already installed — skipping install + reboot."
        } else {
            Install-Wsl
            Register-ResumeTask
            Write-Host ""
            Write-Host "WSL2 installed. A reboot is required to finish." -ForegroundColor Yellow
            Write-Host "After you log back in, Phase A resumes automatically." -ForegroundColor Yellow
            if ($SkipReboot) {
                Warn "SkipReboot set — reboot manually, then Phase A resumes at logon (or run -Resume)."
                return
            }
            Write-Host "Rebooting in 10 seconds (Ctrl+C to cancel)..."
            Start-Sleep -Seconds 10
            Restart-Computer -Force
            return
        }
    } else {
        Load-State
        Write-Host "Resuming Phase A after reboot..." -ForegroundColor Cyan
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
