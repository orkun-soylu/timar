"""The PowerShell that timar runs on a Windows host, and how it is sent.

**How it travels.** Windows' OpenSSH hands every command to the login shell, which the README
asks to be Windows PowerShell. A script is sent as base64 inside a one-line script block,
`& ([scriptblock]::Create(...))`: base64 has no character any shell would reinterpret, so no
quoting layer can damage the script, and it runs in the login shell's own process. Measured on
Windows 11 (build 26300) over OpenSSH:

- `exit 3` inside the block comes back as SSH exit status 3. Starting a second
  `powershell -EncodedCommand` instead returned 1 and wrote CLIXML progress records to stderr.
- A 29,784-character command line was accepted; the limit is CreateProcess's 32,767. UTF-8
  rather than UTF-16 keeps the update script well inside it.

**Why the update runs as scheduled tasks.** A login over SSH can *search* Windows Update but not
download or install: `CreateUpdateDownloader` and `Microsoft.Update.SystemInfo` both fail with
E_ACCESSDENIED in that session, and work from a task running as SYSTEM. Steam is the other half:
it needs the user's profile (its saved sign-in lives there) but not a desktop — started by an S4U
task (the user's identity, no stored password) on a machine nobody has signed in to, it signed in
with its saved token and loaded its library, and `steam.exe -shutdown` from the SSH session
stopped it. So both run as tasks that exist only for the length of the run: registered, started,
waited for, removed. Nothing is installed on the machine and nothing outlives the run.
"""
from __future__ import annotations

import base64
import re


def command(script: str) -> str:
    """`script` as a one-line command for a Windows host whose login shell is PowerShell."""
    # Comment-only lines and indentation cost command-line length and nothing else.
    lines = [line.strip() for line in script.splitlines()]
    body = "\n".join(line for line in lines if line and not line.startswith("#"))
    encoded = base64.b64encode(body.encode()).decode()
    return ("& ([scriptblock]::Create([Text.Encoding]::UTF8.GetString("
            f"[Convert]::FromBase64String('{encoded}'))))")


def decode(cmd: str) -> str:
    """The script inside a `command()` — for tests, and for reading what a host was sent."""
    match = re.search(r"FromBase64String\('([A-Za-z0-9+/=]+)'\)", cmd)
    return base64.b64decode(match.group(1)).decode() if match else ""


# -- health -----------------------------------------------------------------------------------

def journal(hours: int) -> str:
    # System and Application, levels critical (1) and error (2). Measured over a day on a gaming
    # PC: six entries, all one DCOM timeout — quiet enough to read every line of.
    return command(f"""
        Get-WinEvent -FilterHashtable @{{LogName='System','Application'; Level=1,2;
          StartTime=(Get-Date).AddHours(-{int(hours)})}} -MaxEvents 50 -ErrorAction SilentlyContinue |
          ForEach-Object {{ '{{0:s}} {{1}}[{{2}}]: {{3}}' -f $_.TimeCreated, $_.ProviderName, $_.Id,
            (("" + $_.Message) -split "`r?`n")[0] }}
    """)


# One line per fixed drive: its letter and how full it is, in percent.
DISK = command("""
    Get-Volume | Where-Object { $_.DriveLetter -and $_.DriveType -eq 'Fixed' -and $_.Size -gt 0 } |
      ForEach-Object { '{0} {1}' -f $_.DriveLetter, [int](100 - 100 * $_.SizeRemaining / $_.Size) }
""")

# Two lines for `osinfo`: the logo, then the name for the servers table — "Windows 11 Pro 26300".
DETECT = command("""
    $os = Get-CimInstance Win32_OperatingSystem
    'windows'
    'label:' + ($os.Caption -replace '^Microsoft ', '') + ' ' + $os.BuildNumber
""")

# Someone is playing: a process running out of a Steam library. Not "someone is signed in" — a
# machine left on overnight at a locked desktop is fine to update, one in a match is not.
BUSY = command("""
    $games = Get-Process -ErrorAction SilentlyContinue |
      Where-Object { $_.Path -like '*\\steamapps\\common\\*' } | Select-Object -ExpandProperty Name -Unique
    if ($games) { 'a game is running: ' + ($games -join ', ') }
""")

# Empty when no restart is pending; otherwise `restart`, or `signed-in` when a person is signed
# in and a restart would end their session. Read from the registry: the update API that would
# say so directly refuses an SSH session (see the module docstring).
RESTART_NEEDED = command("""
    $pending = (Test-Path 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\WindowsUpdate\\Auto Update\\RebootRequired') -or
               (Test-Path 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Component Based Servicing\\RebootPending')
    if ($pending) {
      if (Get-CimInstance Win32_Process -Filter "Name='explorer.exe'") { 'signed-in' } else { 'restart' }
    }
""")


# -- the update ---------------------------------------------------------------------------------

# Runs as SYSTEM. Software only: drivers come from their vendor's tool, where the operator picks
# the version — a GPU driver swapped underneath a gaming machine overnight is not an update.
# `BrowseOnly=0` leaves out what Windows itself lists as optional.
_WINDOWS_UPDATE = r"""
$ErrorActionPreference = 'Stop'
$out = Join-Path $env:ProgramData 'timar\windows-update.json'
$result = @{ installed = @(); failed = @(); restart = $false; error = $null }
try {
  $session = New-Object -ComObject Microsoft.Update.Session
  $session.ClientApplicationID = 'timar'
  $found = $session.CreateUpdateSearcher().Search("IsInstalled=0 and IsHidden=0 and Type='Software' and BrowseOnly=0").Updates
  $todo = New-Object -ComObject Microsoft.Update.UpdateColl
  foreach ($u in $found) { if (-not $u.EulaAccepted) { $u.AcceptEula() }; [void]$todo.Add($u) }
  if ($todo.Count -gt 0) {
    $downloader = $session.CreateUpdateDownloader(); $downloader.Updates = $todo; [void]$downloader.Download()
    $ready = New-Object -ComObject Microsoft.Update.UpdateColl
    foreach ($u in $todo) { if ($u.IsDownloaded) { [void]$ready.Add($u) } else { $result.failed += "$($u.Title) (not downloaded)" } }
    if ($ready.Count -gt 0) {
      $installer = $session.CreateUpdateInstaller(); $installer.Updates = $ready
      $r = $installer.Install()
      for ($i = 0; $i -lt $ready.Count; $i++) {
        $code = $r.GetUpdateResult($i).ResultCode
        if ($code -eq 2 -or $code -eq 3) { $result.installed += $ready.Item($i).Title }
        else { $result.failed += "$($ready.Item($i).Title) (result $code)" }
      }
      $result.restart = [bool]$r.RebootRequired
    }
  }
} catch { $result.error = $_.Exception.Message }
$result | ConvertTo-Json -Depth 3 | Set-Content -Path "$out.tmp" -Encoding UTF8
Move-Item -Force "$out.tmp" $out
"""

# The script the update run sends. Prints what it does as it goes, ends with one `timar:` line
# that the report shows after the machine's name, and exits 1 if anything failed.
UPDATE_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$dir = Join-Path $env:ProgramData 'timar'
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$said = @(); $failed = $false

function Start-RunTask($name, $exe, $arguments, $principal, $hours) {
  $action = New-ScheduledTaskAction -Execute $exe -Argument $arguments
  $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours $hours) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
  Register-ScheduledTask -TaskName "timar-$name" -Action $action -Principal $principal -Settings $settings -Force | Out-Null
  Start-ScheduledTask -TaskName "timar-$name"
}
function Remove-RunTask($name) {
  Stop-ScheduledTask -TaskName "timar-$name" -ErrorAction SilentlyContinue
  Unregister-ScheduledTask -TaskName "timar-$name" -Confirm:$false -ErrorAction SilentlyContinue
}

# --- Windows Update, as SYSTEM ---
$wu = Join-Path $dir 'windows-update.ps1'
$wuOut = Join-Path $dir 'windows-update.json'
Set-Content -Path $wu -Value @'
__WINDOWS_UPDATE__
'@ -Encoding UTF8
Remove-Item $wuOut -ErrorAction SilentlyContinue
$system = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
Start-RunTask 'windows-update' 'powershell.exe' "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$wu`"" $system 3
$deadline = (Get-Date).AddHours(3)
do { Start-Sleep 10; $state = (Get-ScheduledTask -TaskName 'timar-windows-update').State } while (-not (Test-Path $wuOut) -and $state -in 'Running', 'Queued' -and (Get-Date) -lt $deadline)
Remove-RunTask 'windows-update'
Remove-Item $wu -ErrorAction SilentlyContinue
if (-not (Test-Path $wuOut)) {
  "Windows Update: no result (task $state)"; $said += 'Windows Update gave no result'; $failed = $true
} else {
  $r = Get-Content $wuOut -Raw | ConvertFrom-Json
  Remove-Item $wuOut
  foreach ($t in $r.installed) { "installed: $t" }
  foreach ($t in $r.failed) { "FAILED: $t" }
  if ($r.error) { "Windows Update error: $($r.error)"; $failed = $true; $said += 'Windows Update failed' }
  elseif (@($r.failed).Count) { $failed = $true; $said += "Windows: $(@($r.installed).Count) installed, $(@($r.failed).Count) failed" }
  elseif (@($r.installed).Count) { $said += "Windows: $(@($r.installed).Count) installed" }
  else { $said += 'Windows up to date' }
}

# --- Steam games, as the SSH user ---
# Steam's saved sign-in is encrypted with the user's Windows password (DPAPI), so a task that
# runs without it (S4U) signs in only while that user is also at the desktop. The task the README
# has the owner register once, with the password stored by Task Scheduler, works at any time; it
# is started and stopped, never removed. Without it, a task lasting the run is the fallback.
$steamDir = (Get-ItemProperty 'HKLM:\SOFTWARE\WOW6432Node\Valve\Steam' -ErrorAction SilentlyContinue).InstallPath
if ($steamDir -and (Test-Path (Join-Path $steamDir 'steam.exe'))) {
  $steamExe = Join-Path $steamDir 'steam.exe'
  $libraries = @($steamDir)
  $vdf = Join-Path $steamDir 'steamapps\libraryfolders.vdf'
  if (Test-Path $vdf) {
    $libraries = @(Select-String -Path $vdf -Pattern '"path"\s+"([^"]+)"' | ForEach-Object { $_.Matches[0].Groups[1].Value -replace '\\\\', '\' })
  }
  function Get-SteamApps {
    foreach ($lib in $libraries) {
      Get-ChildItem (Join-Path $lib 'steamapps\appmanifest_*.acf') -ErrorAction SilentlyContinue | ForEach-Object {
        $text = Get-Content $_.FullName -Raw
        $field = { param($k) if ($text -match ('"' + $k + '"\s+"([^"]*)"')) { $Matches[1] } }
        [pscustomobject]@{ Id = & $field 'appid'; Name = & $field 'name'; State = [int](& $field 'StateFlags'); Build = & $field 'buildid' }
      }
    }
  }
  $before = @{}; foreach ($a in Get-SteamApps) { $before[$a.Id] = $a.Build }
  $log = Join-Path $steamDir 'logs\connection_log.txt'
  $seen = if (Test-Path $log) { @(Get-Content $log).Count } else { 0 }
  $ours = -not (Get-Process steam -ErrorAction SilentlyContinue)
  $saved = [bool](Get-ScheduledTask -TaskName 'timar-steam-user' -ErrorAction SilentlyContinue)
  if ($ours -and $saved) {
    Start-ScheduledTask -TaskName 'timar-steam-user'
  } elseif ($ours) {
    $me = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U -RunLevel Limited
    Start-RunTask 'steam' $steamExe '-silent' $me 3
  }
  # Signed in, then a minute for it to learn which games have an update: until then every
  # manifest still says "fully installed" and a finished-looking library proves nothing.
  $deadline = (Get-Date).AddMinutes(3); $signedIn = -not $ours
  while (-not $signedIn -and (Get-Date) -lt $deadline) {
    Start-Sleep 5
    if (Test-Path $log) { $signedIn = [bool](Get-Content $log | Select-Object -Skip $seen | Select-String "RecvMsgClientLogOnResponse\(\) : \S+ 'OK'") }
  }
  if (-not $signedIn) {
    if ($saved) { 'Steam did not sign in within 3 minutes - sign in to Steam on the desktop once; after a Windows password change, register the timar-steam-user task again' }
    else { 'Steam did not sign in within 3 minutes - with nobody at the desktop it needs the timar-steam-user task (README, Windows)' }
    $said += 'Steam could not sign in'; $failed = $true
  } else {
    Start-Sleep 60
    $deadline = (Get-Date).AddHours(2)
    while ((@(Get-SteamApps | Where-Object { $_.State -ne 4 }).Count) -and (Get-Date) -lt $deadline) { Start-Sleep 20 }
    $apps = @(Get-SteamApps)
    $updated = @($apps | Where-Object { $before.ContainsKey($_.Id) -and $before[$_.Id] -ne $_.Build })
    $pending = @($apps | Where-Object { $_.State -ne 4 })
    foreach ($a in $updated) { "Steam: $($a.Name) updated, build $($before[$a.Id]) -> $($a.Build)" }
    foreach ($a in $pending) { "Steam: $($a.Name) not finished (StateFlags $($a.State))" }
    if ($pending.Count) { $said += "Steam: $($pending.Count) not finished"; $failed = $true }
    elseif ($updated.Count) { $said += 'Steam: ' + (($updated | ForEach-Object Name) -join ', ') + ' updated' }
    else { $said += 'Steam games up to date' }
  }
  if ($ours) {
    & $steamExe -shutdown
    $until = (Get-Date).AddSeconds(60)
    while ((Get-Process steam -ErrorAction SilentlyContinue) -and (Get-Date) -lt $until) { Start-Sleep 3 }
    if ($saved) { Stop-ScheduledTask -TaskName 'timar-steam-user' -ErrorAction SilentlyContinue }
    else { Remove-RunTask 'steam' }
  }
}

'timar: ' + ($said -join '; ')
if ($failed) { exit 1 }
"""

UPDATE = command(UPDATE_SCRIPT.replace("__WINDOWS_UPDATE__", _WINDOWS_UPDATE.strip()))
