# Baxter watcher  -  ALWAYS-ON background poller (comms, claude activity, queues).
# The only time it stands aside: while a FULLSCREEN app (a game) owns the screen,
# so it never costs frames mid-match. Everything queues and catches up after.
# Start it with the `baxter-watch` profile command, or run this file directly.
#
# Self-healing: survives a crashed triage (try/catch + log), clears a stale lock
# left by a killed run, and writes its own watcher-heartbeat so the guardian (and
# `baxter-doctor`) can tell the loop is alive. Single-instance via heartbeat
# freshness  -  if another watcher beat <90s ago, this one bows out.

param(
    [int]$IntervalSec = 60,
    [switch]$EmitWatchSet,
    [switch]$ReviveWatchdogOnce,
    # -SelfTest drives the settling gate and Invoke-ChildReload through injected clock/process/
    # kill/spawn stubs, then exits. It launches nothing and kills nothing- see Invoke-WatchSelfTest.
    [switch]$SelfTest,
    # -TriageWaitSelfTest drives Invoke-TriageWaitLoop (the loop that runs WHILE the triage child
    # works) against a stub spawner and, in one case, a real sleeping child. It proves the reaction
    # watcher, /usage poller and fast lane fire on every beat OF THE BLOCK- see Invoke-TriageWaitSelfTest.
    [switch]$TriageWaitSelfTest,
    # A resident is only bounced once its sources have been QUIET for this long. A build burn
    # rewrites baxter_rules/baxter_lanes/baxter_usage seconds apart, and the old guard bounced
    # the listener on the first beat after each one- three kills for one logical change.
    [int]$QuietSec = 120,
    # ...but a burn longer than this still gets exactly one reload, so a genuine fix can never
    # sit unloaded indefinitely behind a source that keeps being touched.
    [int]$MaxDeferMin = 15
)

# Env overrides are the exam's fault-injection channel (same idiom as BAXTER_WATCHGRAPH_FORCE_FAIL).
# An explicit -QuietSec/-MaxDeferMin on the command line still wins over the environment.
if (-not $PSBoundParameters.ContainsKey('QuietSec') -and $env:BAXTER_WATCH_QUIET_SEC) {
    try { $QuietSec = [int]$env:BAXTER_WATCH_QUIET_SEC } catch {}
}
if (-not $PSBoundParameters.ContainsKey('MaxDeferMin') -and $env:BAXTER_WATCH_MAXDEFER_MIN) {
    try { $MaxDeferMin = [int]$env:BAXTER_WATCH_MAXDEFER_MIN } catch {}
}

$ErrorActionPreference = "Continue"
$py        = "C:\Users\you\Documents\Python Scripts\utils\baxter_triage.py"
# Ad-hoc reminder poller (8th July- his "ping me in 10 mins" that never fired). Spawned from
# BOTH loops below, and outside the $gaming/$off gates: a reminder the owner explicitly set is a
# promise, not proactive machinery. Pure code (~50ms, no LLM, no network unless one is due).
$rem       = "C:\Users\you\Documents\Python Scripts\utils\baxter_reminders.py"
# The three short-lived, self-locking one-shots. They used to be declared inside the outer beat,
# below the triage block, and were therefore spawned ONLY by the slice loop- which a triage run
# makes unreachable for its whole duration. Declared here because BOTH loops now fire them.
$fast      = "C:\Users\you\Documents\Python Scripts\utils\baxter_fast.py"
$ucmd      = "C:\Users\you\Documents\Python Scripts\utils\baxter_usage_cmd.py"
$react     = "C:\Users\you\Documents\Python Scripts\utils\baxter_reaction_watch.py"
$vault     = "C:\Users\you\Documents\Baxter"
$lock      = Join-Path $vault ".baxter.lock"
$watchLog  = Join-Path $vault ".baxter_watch.log"
$watchBeat = Join-Path $vault ".baxter_watch_heartbeat.txt"
# The TRIAGE CYCLE-COMPLETION stamp, written by baxter_triage.py run()'s finally. Nothing had
# ever read it. On 10th July a wedged child left it 8h05m stale while the watcher's own
# heartbeat above stayed fresh, so no alarm could exist. Invoke-TriageWaitLoop is its first
# and only reader- for the ALARM, never for a kill decision (see the loop for why).
$script:triageBeat = Join-Path $vault ".baxter_heartbeat.txt"
$enc       = New-Object Text.UTF8Encoding $false

# Paths shared by the supervisor blocks and the child hot-reload guard below.
$utilsDir   = "C:\Users\you\Documents\Python Scripts\utils"
$cocDir     = "C:\Users\you\Documents\Python Scripts\coc_bot"
$waDir      = Join-Path $utilsDir "baxter_whatsapp"
$waBeat     = Join-Path $vault ".baxter_wa_heartbeat.txt"
$waRelink   = Join-Path $vault ".baxter_wa_needs_relink.txt"
$slashBot   = Join-Path $utilsDir "baxter_slash.py"
$childState = Join-Path $vault ".baxter_child_mtimes.json"

Add-Type @"
using System;
using System.Runtime.InteropServices;
public class BxFs {
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern IntPtr MonitorFromWindow(IntPtr h, uint flags);
  [DllImport("user32.dll")] public static extern bool GetMonitorInfo(IntPtr hMon, ref MONITORINFO mi);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
  [StructLayout(LayoutKind.Sequential)] public struct MONITORINFO { public int cbSize; public RECT rcMonitor; public RECT rcWork; public uint dwFlags; }
}
"@

# WINDOWLESS CHILD SPAWN (8th July- the PERMANENT fix for the terminal-flash / stray-console
# clutter the owner kept seeing). Start-Process -WindowStyle Hidden does NOT suppress the console of
# a console-subsystem binary (python.exe / node.exe): ShellExecute allocates a real console and
# only then hides it, which FLASHES a black box and steals keyboard focus (his 14:14 complaint),
# and can leave a stray console sitting there. Spawning through .NET ProcessStartInfo with
# UseShellExecute=$false + CreateNoWindow=$true sets CREATE_NO_WINDOW (0x08000000)- Windows never
# creates a console at all, so there is no flash, no window and no focus steal, on every spawn and
# every respawn. This mirrors the CoC family's nowin.py (same CREATE_NO_WINDOW flag). Children
# inherit this process's std handles, so each script's own file logging is unchanged. NB: this only
# governs how Baxter spawns its OWN children- it never enumerates or hides existing windows, so the
# 2 protected windows ([[keep-two-powershell-windows]]) are launched elsewhere and can never be swept.
function Start-Hidden {
    param(
        [Parameter(Mandatory)] [string]$FilePath,
        [string]$Arguments = "",
        [string]$WorkingDirectory = "",
        [switch]$PassThru
    )
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName        = $FilePath
    $psi.Arguments       = $Arguments
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow  = $true
    $psi.WindowStyle     = [System.Diagnostics.ProcessWindowStyle]::Hidden
    if ($WorkingDirectory) { $psi.WorkingDirectory = $WorkingDirectory }
    # No child of Baxter's may read or write a timestamp-mode .pyc, which CPython validates on
    # (mtime seconds, size) and so can serve a hub file's OLD code to a fresh process. Both vars
    # are load-bearing: DONTWRITEBYTECODE stops the write, PYCACHEPREFIX stops the READ of a
    # beside-source __pycache__ another process already poisoned. UseShellExecute=$false above is
    # what makes EnvironmentVariables writable at all. The literal fallback keeps this function
    # self-contained, so it propagates the vars even when lifted out and run on its own.
    $pycPrefix = if ($script:PycachePrefix) { $script:PycachePrefix } else { "C:\Users\you\Documents\Baxter\.baxter_pycache" }
    $psi.EnvironmentVariables["PYTHONDONTWRITEBYTECODE"] = "1"
    $psi.EnvironmentVariables["PYTHONPYCACHEPREFIX"]     = $pycPrefix
    $p = [System.Diagnostics.Process]::Start($psi)
    if ($PassThru) { return $p }
}

function Test-FullscreenGame {
    # True when the foreground window EXACTLY covers its monitor (borderless or
    # exclusive fullscreen). Maximised normal windows have invisible borders and
    # never match exactly. Desktop/explorer is allowlisted.
    try {
        $h = [BxFs]::GetForegroundWindow()
        if ($h -eq [IntPtr]::Zero) { return $false }
        $r = New-Object BxFs+RECT
        if (-not [BxFs]::GetWindowRect($h, [ref]$r)) { return $false }
        $mi = New-Object BxFs+MONITORINFO
        $mi.cbSize = [Runtime.InteropServices.Marshal]::SizeOf($mi)
        $mon = [BxFs]::MonitorFromWindow($h, 2)   # 2 = MONITOR_DEFAULTTONEAREST
        if (-not [BxFs]::GetMonitorInfo($mon, [ref]$mi)) { return $false }
        $m = $mi.rcMonitor
        if ($r.L -ne $m.L -or $r.T -ne $m.T -or $r.R -ne $m.R -or $r.B -ne $m.B) { return $false }
        $procId = [uint32]0
        [BxFs]::GetWindowThreadProcessId($h, [ref]$procId) | Out-Null
        $name = (Get-Process -Id $procId -ErrorAction SilentlyContinue).ProcessName
        if ($name -in @("explorer", "SearchHost", "ShellExperienceHost", "dwm")) { return $false }
        return $true
    } catch { return $false }
}

function Write-Beat($state) {
    try { [IO.File]::WriteAllText($watchBeat, "$(Get-Date -Format o)`t$state", $enc) } catch {}
}
# Under -SelfTest the log is an in-memory list, never the real .baxter_watch.log. Two reasons:
# the probe must not pollute the file the live drill greps, and the captured lines ARE the
# selftest's assertion channel (it checks a 'settling' line was emitted, not just that no kill ran).
$script:SelfTestMode = $false
$script:SelfTestLog  = New-Object Collections.Generic.List[string]
$script:RealOutwardCalls = 0
function Write-WatchLog($msg) {
    if ($script:SelfTestMode) { [void]$script:SelfTestLog.Add([string]$msg); return }
    try { Add-Content -Path $watchLog -Value "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $msg" -Encoding UTF8 } catch {}
}

# Full singleton set - every long-lived process that must be exactly one. Added the
# WhatsApp bridge and the CoC posters (coc_discord/coc_autopilot) 6 Jul: a doubled
# bridge or CoC poster would double-post just like the live-session duplicate did.
# Each entry: @(process-NAME regex, command-line regex). Scoping by binary NAME
# kills the false positive that fired the 6-Jul "whatsapp bridge x4" alarm: a
# triage worker ran `bash -c "node bridge.mjs"`, so 3 bash.exe wrappers each carried
# "bridge.mjs" in their command line and were counted as bridges. The real bridge is
# a node.exe; the wrappers are bash.exe- so require the right binary AND the cmd match.
# The NAME scope earns its keep twice over: a build worker's own `claude.exe -p "..."`
# prompt can quote a fleet script's filename (a touch-set naming baxter_watch.ps1 does
# exactly that), and only the binary check stops that worker being censused as a watcher.
#
# But the NAME scope is not enough on its own (9 Jul): the false positive that mattered was a
# POWERSHELL process quoting the path- a build worker's shell, a hand-run diagnostic. Same
# binary, so the name check waves it through, and the row reads x2. The powershell rows now
# anchor on `-File <path>`: RUNNING a script, not merely naming it. Every real launch (the
# guardian, the self-restart below, the Startup shortcut) uses `-File "<path>"`. This is not
# cosmetic- that phantom 'watcher x2' is what made the auto-reaper kill the live watcher and
# leave the fleet headless at 03:24:25 on 9 Jul.
$BxSingles = [ordered]@{
    'live session'    = @('claude\.exe',  'channels plugin:discord')
    'channel keeper'  = @('powershell',   '-File\s+"?[^"]*baxter_channel\.ps1')
    'guardian'        = @('powershell',   '-File\s+"?[^"]*baxter_guardian\.ps1')
    # The `-File` anchor is not enough on its own either (9 Jul, again). A ONE-SHOT invocation of
    # THIS VERY SCRIPT- `-ReviveWatchdogOnce`, `-EmitWatchSet`- genuinely runs baxter_watch.ps1 with
    # `-File`, so it satisfies the anchor and censuses as `watcher x2`. The reaper then kills the
    # probe mid-flight: `auto-reaped: watcher PID 21928` at 13:29:10, which made the one-shot exit
    # non-zero and the sealed acceptance test misreport it as a cv2 failure. It is a RACE- a ~7s
    # probe against a 60s beat- so it passes green most runs and bites at random. Exclude the probe
    # switches: a process is the WATCHER only if it runs the script with no one-shot switch on it.
    # Any new one-shot switch MUST be added to this alternation or the fleet will reap it.
    'watcher'         = @('powershell',   '-File\s+"?[^"]*baxter_watch\.ps1"?(?!.*-(?:ReviveWatchdogOnce|EmitWatchSet|TriageWaitSelfTest|SelfTest))')
    'whatsapp bridge' = @('node\.exe',    'bridge\.mjs')
    'coc discord'     = @('python',       'coc_discord\.py')
    'coc autopilot'   = @('python',       'coc_autopilot\.py')
    'slash+listener'  = @('python\.exe',  'baxter_slash\.py')
    'codex bot'       = @('python\.exe',  'baxter_codex_bot\.py')
    'jem bot'         = @('python\.exe',  'baxter_jemini_bot\.py')
}

function Get-SingletonCounts {
    # Take the MAX count across a few reads. A null CommandLine under CIM only ever DROPS
    # a row (under-counts), so a single flaky read can hide a real duplicate - which is
    # exactly why NO alarm fired for the 6-Jul live-session pair that ran ~6h. Max across
    # reads recovers the true count (a real duplicate resolves on at least one read) and
    # can never inflate one, so this net catches persistent duplicates without false alarms.
    $counts = [ordered]@{}
    foreach ($k in $BxSingles.Keys) { $counts[$k] = 0 }
    for ($r = 0; $r -lt 3; $r++) {
        $procs = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -and $_.CommandLine -notmatch 'Get-CimInstance' })
        foreach ($k in $BxSingles.Keys) {
            $nm = $BxSingles[$k][0]; $cl = $BxSingles[$k][1]
            $c = @($procs | Where-Object { $_.Name -match $nm -and $_.CommandLine -match $cl }).Count
            if ($c -gt $counts[$k]) { $counts[$k] = $c }
        }
        Start-Sleep -Milliseconds 400
    }
    return $counts
}

$script:guardianDownBeats = 0

# The standalone ping is the entire point of the auto-reaper (the owner, 8 Jul 23:54: "this sort of
# thing deserves a separate ping"). Both pings used to go out as
#     try { & python "$sayPy" $msg 2>$null } catch {}
# which is three faults on one line. `python` is resolved off the watcher's PATH rather than
# named. A command that cannot LAUNCH does not throw and leaves $LASTEXITCODE holding the
# PREVIOUS command's value, so no caller can tell. And `catch {}` throws the diagnostic away.
# On 10 Jul the resident reaped a duplicate coc autopilot at 09:25:10, sent NOTHING, and the
# watch log could not say why. Silence is the one outcome a ping must never have.
#
# Returns $true only when baxter_say actually RAN and exited 0. Every other outcome is logged
# with its reason. Callers write their 30-min cooldown flag ONLY on $true: a ping that never
# left must not mute the next half hour of them.
function Send-BaxterPing([string]$sayPy, [string]$msg) {
    if (-not (Test-Path $sayPy)) { Write-WatchLog "ping NOT SENT- no baxter_say.py at $sayPy"; return $false }
    $exe = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $exe) { $exe = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe" }
    if (-not $exe -or -not (Test-Path $exe)) { Write-WatchLog "ping NOT SENT- no python interpreter found"; return $false }
    $native = Join-Path (Split-Path $sayPy -Parent) "baxter_native.ps1"
    try {
        if (Test-Path $native) {
            . $native   # Invoke-Native: `Launched = $false` is a state no exit code can impersonate
            $r = Invoke-Native $exe @($sayPy, $msg)
            if (-not $r.Launched)    { Write-WatchLog "ping NOT SENT- $($r.StdErr)"; return $false }
            if ($r.ExitCode -ne 0)   { Write-WatchLog "ping NOT SENT- baxter_say exit $($r.ExitCode): $(([string]$r.StdErr).Trim())"; return $false }
            # rc 0 also covers baxter_say's "deduped (...) - not sent": he already has that line.
            if ($r.StdOut -match 'not sent') { Write-WatchLog "ping suppressed by baxter_say: $(([string]$r.StdOut).Trim())" }
            return $true
        }
        $null = & $exe $sayPy $msg
        if ($LASTEXITCODE -ne 0) { Write-WatchLog "ping NOT SENT- baxter_say exit $LASTEXITCODE"; return $false }
        return $true
    } catch {
        Write-WatchLog "ping NOT SENT- $($_.Exception.Message)"
        return $false
    }
}

function Assert-Guardian($guardianCount) {
    # The guardian is the TOP of the resurrection chain: it revives the watcher, and until now
    # NOTHING revived the guardian. It was found dead on 9 Jul 00:56 and only a re-login would
    # have brought it back. The watcher can't be resurrected by a corpse, so while WE are alive
    # we keep IT alive- the mirror image of its own job. Its immediate Start-Watcher no-ops
    # against our fresh heartbeat, and a doubled guardian is reaped back to the oldest below.
    #
    # TWO consecutive down beats before we act. CIM can read a live process's CommandLine back as
    # EMPTY (it did exactly that to the watcher on 9 Jul, on all three reads), which would census
    # a healthy guardian as absent. Relaunching on a single flake spawns a duplicate, which the
    # reaper then kills- a spawn/kill flap once a minute, for ever. A real death persists; a flake
    # does not. 60s of extra downtime is a cheap price for never flapping.
    if ($guardianCount -ne 0) { $script:guardianDownBeats = 0; return }
    $script:guardianDownBeats++
    if ($script:guardianDownBeats -lt 2) {
        Write-WatchLog "guardian censused as down (beat $($script:guardianDownBeats)/2) - confirming before relaunch"
        return
    }
    $script:guardianDownBeats = 0
    $guardian = Join-Path $utilsDir "baxter_guardian.ps1"
    if (-not (Test-Path $guardian)) { return }
    try {
        Start-Hidden -FilePath "powershell.exe" -Arguments "-WindowStyle Hidden -ExecutionPolicy Bypass -NoProfile -File `"$guardian`""
        Write-WatchLog "guardian was DOWN - relaunched it hidden (top of the resurrection chain)"
        # Its own cooldown flag: a guardian that flaps must not ping once a minute.
        $gFlag = Join-Path $vault ".baxter_guardian_revived.txt"
        $recent = $false
        if (Test-Path $gFlag) { try { $recent = ((((Get-Date) - [datetime]::Parse((Get-Content $gFlag -Raw))).TotalMinutes) -lt 30) } catch {} }
        if (-not $recent) {
            $sayPy = Join-Path $utilsDir "baxter_say.py"
            if (Send-BaxterPing $sayPy "The guardian was down, sir- I've relaunched it. Nothing else revives it, and the watcher stayed up throughout.") {
                [IO.File]::WriteAllText($gFlag, (Get-Date -Format o), $enc)
            }
        }
    } catch { Write-WatchLog "guardian relaunch FAILED: $_" }
}

function Assert-Singletons {
    # DETECTION NET (added 6 Jul, after a duplicate live channel session ran ~6h undetected
    # and double-answered every message) - now an AUTO-REAPER too (the owner, 8 Jul 23:54: "I want
    # you to ALWAYS reap this sort of duplicates").
    #
    # It used to be detection-only, deferring the reaping to "the per-component guards". The
    # CoC daemons and the WhatsApp bridge have no such guard, so their duplicates could ONLY
    # ever be cleared by the owner running the doctor by hand- which is how a `coc autopilot x2`
    # came to sit on the alert for hours. Now: REAP every beat, RE-CENSUS to prove it took,
    # and PING once per episode. The 30-min cooldown gates the PING ONLY, never the reap.
    #
    # The doctor is the single reaper (one definition of "which instance we keep"), invoked with
    # -DupesOnly so it can never zero a row: a lone live session is healthy, and plain -Reap
    # would kill it. The ping names what the doctor ACTUALLY killed, parsed from its REAP
    # lines- not what we assumed it would kill.
    #
    # -SelfPid $PID is the seatbelt: we are ourselves inside the 'watcher' row we are policing,
    # so the reaper must know which watcher is the live one. Without it, "keep the oldest" kills
    # the caller during a self-restart (the oldest watcher is the one exiting). That is not
    # hypothetical- it happened at 03:24:25 on 9 Jul and, with the guardian dead too, nothing
    # brought the fleet back.
    $sayPy    = Join-Path $utilsDir "baxter_say.py"
    $doctor   = Join-Path $utilsDir "baxter_doctor.ps1"
    $dupFlag  = Join-Path $vault ".baxter_dupe_alerted.txt"
    try {
        $counts = Get-SingletonCounts
        Assert-Guardian $counts['guardian']

        $dupes = @()
        foreach ($k in $BxSingles.Keys) {
            if ($counts[$k] -gt 1) { $dupes += ("{0} x{1}" -f $k, $counts[$k]) }
        }
        if ($dupes.Count -eq 0) {
            # Only clear an EXPIRED flag. Clearing a live one would reset the ping cooldown,
            # so a duplicate that respawns every beat would ping the owner once a minute.
            if (Test-Path $dupFlag) {
                $expired = $true
                try { $expired = ((((Get-Date) - [datetime]::Parse((Get-Content $dupFlag -Raw))).TotalMinutes) -ge 30) } catch {}
                if ($expired) { Remove-Item $dupFlag -Force -ErrorAction SilentlyContinue }
            }
            return
        }

        Write-WatchLog ("duplicate process(es) detected: " + ($dupes -join ', ') + " - reaping")
        # Read the cooldown BEFORE reaping: the doctor may clear the flag on a clean census.
        $recent = $false
        if (Test-Path $dupFlag) { try { $recent = ((((Get-Date) - [datetime]::Parse((Get-Content $dupFlag -Raw))).TotalMinutes) -lt 30) } catch {} }

        $kills = @()
        try {
            $out = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$doctor" -Reap -DupesOnly -SelfPid $PID 2>&1
            foreach ($line in $out) {
                if ([string]$line -match "^REAP`t([^`t]+)`t(\d+)`t(.+)$") {
                    $kills += [pscustomobject]@{ Name = $Matches[1]; Pid = $Matches[2] }
                }
            }
        } catch { Write-WatchLog "auto-reap FAILED to run the doctor: $_" }

        # PROVE it took. The reap is worthless if we only assume it worked, so census again
        # from scratch; whatever is still >1 gets reported as SURVIVING, not as reaped.
        $after     = Get-SingletonCounts
        $survivors = @()
        foreach ($k in $BxSingles.Keys) {
            if ($after[$k] -gt 1) { $survivors += ("{0} x{1}" -f $k, $after[$k]) }
        }
        if ($kills.Count -gt 0) {
            Write-WatchLog ("auto-reaped: " + (($kills | ForEach-Object { "$($_.Name) PID $($_.Pid)" }) -join ', '))
        }
        if ($survivors.Count -gt 0) { Write-WatchLog ("STILL duplicated after reap: " + ($survivors -join ', ')) }

        # Killed nothing, and nothing is duplicated any more: the "duplicate" was a transient
        # overlap that resolved itself between our census and the doctor's (a process mid-restart,
        # a shell that exited). There is no incident to report. Log it and stay quiet- pinging
        # here is crying wolf, which is exactly what the 03:24:29 "auto-reap killed nothing"
        # alarm did on 9 Jul. No flag either: a phantom must not burn the real episode's cooldown.
        if ($kills.Count -eq 0 -and $survivors.Count -eq 0) {
            Write-WatchLog ("transient duplicate resolved itself before the reap: " + ($dupes -join ', '))
            return
        }

        # STANDALONE ping, its own message, naming what was reaped (the owner, 8 Jul 23:54: "this
        # sort of thing deserves a separate ping")- never folded into another confirmation.
        if ($recent) { return }
        if ($survivors.Count -gt 0) {
            $msg = "WARN duplicate " + ($survivors -join ', ') + " survived the auto-reap, sir. Run: powershell -File `"$doctor`" -Reap"
        } else {
            $what = ($kills | Group-Object Name | ForEach-Object { "{0} (PID {1})" -f $_.Name, (($_.Group.Pid) -join ', ') }) -join '; '
            $msg  = "🧹 Auto-reaped a duplicate " + $what + ", sir- kept one. Fleet censused clean."
        }
        if (Send-BaxterPing $sayPy $msg) {
            [IO.File]::WriteAllText($dupFlag, (Get-Date -Format o), $enc)
        }
    } catch {}
}

# Set on the WATCHER'S OWN process, so every child inherits it- including the one spawn in this
# file that CANNOT be given an env dict (the coc watchdog goes out under UseShellExecute=$true,
# which forbids ProcessStartInfo.EnvironmentVariables). Start-Hidden sets both explicitly as well.
$script:PycachePrefix = Join-Path $vault ".baxter_pycache"
if (-not (Test-Path $script:PycachePrefix)) { New-Item -ItemType Directory -Force $script:PycachePrefix | Out-Null }
$env:PYTHONDONTWRITEBYTECODE = "1"
$env:PYTHONPYCACHEPREFIX     = $script:PycachePrefix

$py312 = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
if (-not (Test-Path $py312)) { $py312 = "python" }
# The 3.11 store alias the CoC daemons actually run under (same interpreter coc_bot/watchdog.py uses).
$pyCoc = "$env:LOCALAPPDATA\Microsoft\WindowsApps\python.exe"
if (-not (Test-Path $pyCoc)) { $pyCoc = "python" }

# --- coc watchdog --loop: a RESURRECTABLE singleton (9 Jul) ---------------------------------
# `watchdog.py --loop` is the ONLY thing keeping the five CoC daemons alive, and it was started
# solely by Startup/coc_watchdog.vbs at logon. If it crashed, NOTHING revived it and the whole
# farm ran unwatched until the owner next logged in. It deliberately does NOT join $BxSingles: that
# set is a DUPLICATE detector whose reaper only ever trims a row DOWN, and an absent row there
# is merely reported. This one needs the opposite- absent means relaunch- so it gets its own
# census and its own assert, modelled on Assert-Guardian.
$script:watchdogDownBeats = 0
$script:WatchdogSettleSec = 3

function Get-CocWatchdogLoopProcs {
    # Name is matched on the SUBSTRING 'python', never 'python\.exe': the live loop, launched via
    # the versioned WindowsApps alias, reports its Name as `python3.11.exe`, and a pythonw revive
    # reports `pythonw3.11.exe`. Anchoring on `-File <path>` the way the powershell rows do is
    # impossible here- the vbs passes an ABSOLUTE path (`pythonw "C:\...\watchdog.py" --loop`)
    # while the live loop's cmdline is a bare RELATIVE `watchdog.py --loop`- so both legitimate
    # shapes are pinned by filename AND the --loop flag together. The binary-name scope is what
    # keeps a build worker's `claude.exe -p "...watchdog.py --loop..."` prompt out of the census.
    @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.Name -match 'python' -and $_.CommandLine `
        -and $_.CommandLine -notmatch 'Get-CimInstance' `
        -and $_.CommandLine -match 'watchdog\.py' `
        -and $_.CommandLine -match '--loop'
    })
}

function Test-CocWatchdogLoop {
    # MAX across 3 reads, exactly as Get-SingletonCounts. A null CommandLine under CIM only ever
    # DROPS a row (it did precisely that to the watcher on 9 Jul, on all three reads), so a single
    # flaky read censuses a live loop as absent. For a detector that only ever costs a missed
    # alarm; here it would SPAWN A DUPLICATE. Max across reads can never inflate a count.
    $max = 0
    for ($r = 0; $r -lt 3; $r++) {
        $hits = @(Get-CocWatchdogLoopProcs)
        if ($hits.Count -gt $max) { $max = $hits.Count }
        Start-Sleep -Milliseconds 300
    }
    return $max
}

function Assert-WatchdogLoop([switch]$Immediate) {
    if ((Test-CocWatchdogLoop) -ne 0) { $script:watchdogDownBeats = 0; return }

    # TWO consecutive confirmed-down beats before acting, for Assert-Guardian's exact reason:
    # relaunching on a single CIM flake spawns a duplicate, the reaper kills it, and the pair
    # flap once a minute for ever. A real death persists; a flake does not.
    #
    # -Immediate is what -ReviveWatchdogOnce passes. $script:watchdogDownBeats lives in the
    # PROCESS, so a one-shot `powershell -File ... -ReviveWatchdogOnce` starts it at 0 every
    # invocation and could NEVER reach 2- the switch would silently revive nothing, however
    # often it were called. It drives THIS function (never a copy) and skips only the debounce,
    # which smooths a 60s beat and means nothing to one deliberate invocation.
    if (-not $Immediate) {
        $script:watchdogDownBeats++
        if ($script:watchdogDownBeats -lt 2) {
            Write-WatchLog "coc watchdog --loop censused as down (beat $($script:watchdogDownBeats)/2) - confirming before relaunch"
            return
        }
    }
    $script:watchdogDownBeats = 0

    $wd = Join-Path $cocDir "watchdog.py"
    if (-not (Test-Path $wd)) { Write-WatchLog "coc watchdog --loop is down but watchdog.py is missing - not relaunching"; return }

    # Read the ping cooldown BEFORE relaunching, and write it only on a CONFIRMED revive: a flag
    # written on the mere attempt would let a loop that never comes up ping every single beat.
    $wFlag  = Join-Path $vault ".baxter_watchdog_revived.txt"
    $recent = $false
    if (Test-Path $wFlag) { try { $recent = ((((Get-Date) - [datetime]::Parse((Get-Content $wFlag -Raw))).TotalMinutes) -lt 30) } catch {} }

    # THE INTERPRETER IS THE WHOLE POINT. watchdog.py spawns every daemon with PY = sys.executable,
    # so a loop revived under the watcher's own python (3.12) or an arbitrary PATH pythonw would
    # look perfectly healthy in every census while spawning a coc_autopilot that dies on
    # `import cv2`. $pyCoc is the 3.11 store alias- the only interpreter here with cv2- and its
    # pythonw.exe sibling is the same package, windowless. Never resolve `pythonw` off PATH.
    $pyw = Join-Path (Split-Path $pyCoc) "pythonw.exe"
    if (-not (Test-Path $pyw)) { $pyw = "pythonw" }

    # SPAWN DETACHED- the revived loop must NOT inherit our std handles. Start-Hidden spawns with
    # UseShellExecute=$false, which hands the child duplicates of our stdout/stderr. Any caller
    # that CAPTURES our output- and `& powershell.exe -File ... -ReviveWatchdogOnce *>$null` is
    # exactly how the one-shot is driven- then reads that pipe until every holder closes it. The
    # loop never exits, so the caller hangs for ever on a revive that already succeeded. Observed:
    # powershell.exe exits in 6s, the `&` around it blocks past 2 minutes.
    #
    # UseShellExecute=$true inherits no handles at all. It is safe HERE, and only here, because
    # pythonw.exe is a GUI-subsystem binary: Windows allocates it no console, so there is no black
    # flash and no focus steal- the very things CreateNoWindow exists to prevent for the CONSOLE
    # binaries (python.exe / node.exe) every other spawn in this file launches. Do not "simplify"
    # this back to Start-Hidden.
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName         = $pyw
    $psi.Arguments        = "`"$wd`" --loop"
    $psi.WorkingDirectory = $cocDir
    $psi.UseShellExecute  = $true
    $psi.WindowStyle      = [System.Diagnostics.ProcessWindowStyle]::Hidden
    try { [void][System.Diagnostics.Process]::Start($psi) }
    catch { Write-WatchLog "coc watchdog --loop relaunch FAILED to spawn: $_"; return }

    # PROVE the relaunch took. A relaunch that is merely assumed is worthless- the same discipline
    # Assert-Singletons applies to its reap. The loop takes a WMI sweep to settle, so re-census
    # only after it has, and log the two outcomes DISTINCTLY: a silent "still absent" would read
    # in the log exactly like a success.
    Start-Sleep -Seconds $script:WatchdogSettleSec
    if ((Test-CocWatchdogLoop) -lt 1) {
        Write-WatchLog "coc watchdog --loop was DOWN - relaunched under $pyw but it is STILL ABSENT after settle; the revive did NOT take"
        return
    }
    $newPid = (Get-CocWatchdogLoopProcs | Select-Object -First 1).ProcessId
    Write-WatchLog "coc watchdog --loop was DOWN - relaunched it hidden under $pyw and confirmed it revived as PID $newPid"

    # STANDALONE ping, its own message, gated by the cooldown- never folded into another
    # confirmation. The flag gates the PING ONLY; the relaunch above is never gated.
    if ($recent) { return }
    $sayPy = Join-Path $utilsDir "baxter_say.py"
    $msg = "The CoC watchdog loop had died, sir- I've revived it (PID $newPid). It's the only thing keeping the five daemons alive, and nothing would have restarted it before your next logon."

    # THE TEST SWITCH MUST NOT STAMP THE REAL FLAG. It used to sit AFTER the WriteAllText, so a
    # rehearsal of the revive gate muted the next thirty minutes of GENUINE revive pings.
    # Suppressing a ping and recording that one was sent are opposite acts.
    if ($env:BAXTER_WATCHDOG_TEST) { Write-WatchLog "[test] revive ping suppressed: $msg"; return }

    # SEND FIRST, STAMP SECOND (10 Jul). This tail used to read
    #     try { & python (Join-Path $utilsDir "baxter_say.py") $msg 2>$null } catch {}
    # with the flag stamped two lines above it- the identical swallow that made Assert-Singletons
    # reap a duplicate coc autopilot at 09:25:10 and send nothing. `python` off PATH, a launch
    # failure that leaves $LASTEXITCODE stale, a catch that eats the diagnostic, and a cooldown
    # written for a ping that never left. Send-BaxterPing returns $true only when baxter_say RAN
    # and exited 0; only that stamps the flag, and every other outcome is logged with its reason.
    $sent = Send-BaxterPing $sayPy $msg
    if (-not $sent) {
        Write-WatchLog "revive ping NOT SENT- cooldown flag withheld, the next beat will try again"
        return
    }
    [IO.File]::WriteAllText($wFlag, (Get-Date -Format o), $enc)
}

# DERIVED WATCH SET (9 Jul). Watch = every source the child holds RESIDENT in memory: its own
# file plus the modules it imports, at any depth (editing baxter_lanes.py goes just as stale
# inside the listener as editing baxter_slash.py). Until today that list was a literal array,
# hand-copied from baxter_slash's import statements - the same defect class as the prompt-rule
# duplication fixed one level down, and it bit that very build: baxter_rules.py had to be
# hand-added here or the listener would have served the old prompt for ever. A hand-copied
# derivative of the source rots the moment someone adds an import and forgets the copy, and it
# rots SILENTLY: nothing warns, the listener just keeps running dead code.
#
# So we ask baxter_imports.py to walk baxter_slash's real import graph (a static ast walk -
# no token read, no discord.Client built, and it sees function-level lazy imports a runtime
# sys.modules probe would miss). The literal array survives as $slashWatchFallback, a
# validated FALLBACK ONLY, and `baxter_imports.py --check-fallback` fails loudly if it drifts.
$importsPy = Join-Path $utilsDir "baxter_imports.py"
$script:WatchSetCache = @{}

function Get-WatchSetKey([string[]]$paths) {
    # Newest mtime across the CURRENTLY-KNOWN set, plus its size. A new import can only appear
    # if a file we already watch was edited, so this key moves exactly when the closure might
    # have changed - and never on an idle beat. Without it we would spawn python every 15s
    # inside the hot loop, and a slow python would stall the watcher, which looks identical
    # to a dead one.
    $ticks = 0
    foreach ($p in $paths) {
        try { $t = (Get-Item $p -ErrorAction Stop).LastWriteTimeUtc.Ticks; if ($t -gt $ticks) { $ticks = $t } } catch {}
    }
    return "$($paths.Count):$ticks"
}

function Get-WatchSet {
    param([Parameter(Mandatory)][string]$RootPy, [Parameter(Mandatory)][string[]]$Fallback)

    $cached = $script:WatchSetCache[$RootPy]
    if ($cached -and (Get-WatchSetKey $cached.Set) -eq $cached.Key) { return $cached.Set }

    $derived = $null
    $why     = $null
    if ($env:BAXTER_WATCHGRAPH_FORCE_FAIL -eq '1') {
        $why = "BAXTER_WATCHGRAPH_FORCE_FAIL=1"          # the e2e exam's fault injection
    } elseif (-not (Test-Path $importsPy)) {
        $why = "baxter_imports.py missing"
    } else {
        try {
            $psi = New-Object System.Diagnostics.ProcessStartInfo
            $psi.FileName               = $py312
            $psi.Arguments              = "`"$importsPy`" `"$RootPy`""
            $psi.UseShellExecute        = $false
            $psi.CreateNoWindow         = $true
            $psi.RedirectStandardOutput = $true
            $psi.RedirectStandardError  = $true
            # This block bypasses Start-Hidden, so it must carry the bytecode vars itself- it is
            # the 3.12 spawn, and 3.12 owns 18 of the pycs in utils/__pycache__.
            $psi.EnvironmentVariables["PYTHONDONTWRITEBYTECODE"] = "1"
            $psi.EnvironmentVariables["PYTHONPYCACHEPREFIX"]     = $script:PycachePrefix
            $p  = [System.Diagnostics.Process]::Start($psi)
            $so = $p.StandardOutput.ReadToEndAsync()
            $se = $p.StandardError.ReadToEndAsync()
            if (-not $p.WaitForExit(10000)) {
                try { $p.Kill() } catch {}
                $why = "timed out after 10s"
            } elseif ($p.ExitCode -ne 0) {
                $why = "exit $($p.ExitCode): $((($se.Result) -replace '\s+', ' ').Trim())"
            } else {
                $derived = @($so.Result -split "`r?`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ })
            }
        } catch { $why = "$_" }
    }

    # VALIDATE before trusting. A walk that returns a SHORT list (one parse error, one bad
    # path) would silently narrow the watch set: edits to baxter_rules.py would stop reaching
    # the listener and nothing would log. A narrowed set is worse than the literal array,
    # because at least the array is wrong in a way someone eventually notices.
    if ($derived) {
        $rootFull = [IO.Path]::GetFullPath($RootPy)
        $bad = $null
        if ($derived.Count -lt 1) { $bad = "empty result" }
        else {
            $missing = @($derived | Where-Object { -not (Test-Path $_) })
            if ($missing.Count -gt 0) { $bad = "$($missing.Count) derived path(s) do not exist" }
            elseif (@($derived | Sort-Object -Unique).Count -ne $derived.Count) { $bad = "duplicate entries" }
            elseif (@($derived | Where-Object { $_ -ieq $rootFull }).Count -eq 0) { $bad = "root absent from its own closure" }
        }
        if ($bad) { $why = $bad; $derived = $null }
    }

    if ($derived) {
        $set = [string[]]$derived
        $script:WatchSetCache[$RootPy] = @{ Set = $set; Key = (Get-WatchSetKey $set) }
        return $set
    }
    Write-WatchLog "watch-set derivation FAILED ($why) - falling back to literal list ($($Fallback.Count) entries)"
    # Cache the fallback too, keyed the same way: a persistently failing walk must not respawn
    # python on every beat. A source edit changes the key and we try again.
    $script:WatchSetCache[$RootPy] = @{ Set = [string[]]$Fallback; Key = (Get-WatchSetKey $Fallback) }
    return [string[]]$Fallback
}

# FALLBACK ONLY - never the source of truth. It exists for the case where the walk cannot run
# at all, and it fires only then - i.e. exactly when nobody is watching it, which is why it is
# checked while everything still works.
#
# MACHINE-RECONCILED IN PLACE (10 Jul). Do not hand-edit the names below. baxter_imports.
# write_fallback() renders them from baxter_slash's live import closure and commits them
# through the baxter_hub_edit fence; triage's drift guard calls it on every pass, so a human
# edit lives until the next pass and is then overwritten. Until today a drift instead QUEUED A
# REPAIR BUILD, and every landed import spawned a lane to rewrite a list it could derive.
# `baxter_imports.py --check-fallback <this file>` reports drift; --write-fallback repairs it.
$slashWatchFallback = @(
    'baxter_slash.py', 'baxter_autobuild.py', 'baxter_clusters.py', 'baxter_deals.py',
    'baxter_eol.py', 'baxter_fast.py', 'baxter_hub_edit.py', 'baxter_imports.py',
    'baxter_lanes.py', 'baxter_modelguard.py', 'baxter_name.py', 'baxter_orch.py',
    'baxter_preannounce_guard.py', 'baxter_reaction_watch.py', 'baxter_read_channel.py', 'baxter_rejig.py',
    'baxter_reminders.py', 'baxter_resurrect_audit.py', 'baxter_roll.py', 'baxter_rules.py',
    'baxter_send_dedup.py', 'baxter_siblings.py', 'baxter_text.py', 'baxter_triage.py',
    'baxter_turntokens.py', 'baxter_usage.py', 'baxter_verify.py'
) | ForEach-Object { Join-Path $utilsDir $_ }

# A resident carries EITHER a literal Watch (single file, no import graph to walk - the CoC
# pair and the node bridge) or a WatchRoot to derive from. Name/Cmd match the process by
# binary NAME + cmdline, per the census memory: a bare cmdline substring would count this
# very shell.
$residents = [ordered]@{
    'baxter_slash'    = @{
        WatchRoot = $slashBot; WatchFallback = $slashWatchFallback
        Name  = 'python\.exe'; Cmd = 'baxter_slash\.py'
        File  = $py312; Arguments = "`"$slashBot`""; Cwd = $utilsDir
        # Busy is matched by binary NAME + cmdline, never a bare substring: a shell whose
        # command line merely MENTIONS baxter_reply_worker.py (a doctor census, a bash -c
        # wrapper, the very command that greps for it) would otherwise count as a live
        # worker and defer the reload for as long as it sat there. Same trap that faked the
        # "whatsapp bridge x4" alarm in Assert-Singletons.
        BusyName = 'python'; Busy = 'baxter_reply_worker\.py'
    }
    'coc discord'     = @{
        Watch = @(Join-Path $cocDir 'coc_discord.py')
        Name  = 'python'; Cmd = 'coc_discord\.py'
        File  = $pyCoc; Arguments = "`"$cocDir\coc_discord.py`""; Cwd = $cocDir
    }
    'coc autopilot'   = @{
        Watch = @(Join-Path $cocDir 'coc_autopilot.py')
        Name  = 'python'; Cmd = 'coc_autopilot\.py'
        File  = $pyCoc; Arguments = "`"$cocDir\coc_autopilot.py`""; Cwd = $cocDir
    }
    'whatsapp bridge' = @{
        Watch = @(Join-Path $waDir 'bridge.mjs')
        Name  = 'node\.exe'; Cmd = 'bridge\.mjs'
        File  = 'node'; Arguments = 'bridge.mjs'; Cwd = $waDir
        # Heavy + the owner must scan a QR if it comes up unlinked: never bounce it during /off
        # or once it has flagged that it needs a relink.
        SkipWhenOff = $true
        SkipIf = { Test-Path $waRelink }
    }
}

# The ONE place a resident's watch set is resolved. Both the hot loop (Invoke-ChildReload)
# and -EmitWatchSet come through here: an 'emit' copy of this logic would re-introduce the
# exact duplication this change exists to remove, and the emitted set would drift from the
# one the loop actually reloads on - making the probe prove nothing.
function Resolve-ResidentWatch($c) {
    if ($c.WatchRoot) { return Get-WatchSet -RootPy $c.WatchRoot -Fallback $c.WatchFallback }
    return [string[]]$c.Watch
}

# -EmitWatchSet: print the RESOLVED watch set, launch nothing, exit. This is what turns the
# watch set into observable BEHAVIOUR rather than source text a grep can be fooled by -
# baxter_watchgraph_e2e.py reads it back and compares it to the live import closure. It sits
# BEFORE the single-instance guard, every child launch, the child-state write, the guardian
# relaunch, the singleton reap and the self-restart, so it can be run against a live fleet
# without perturbing it. (A probe switch that booted a second listener would be a fleet
# duplication bug of exactly the kind Assert-Singletons exists to reap.)
if ($EmitWatchSet) {
    foreach ($k in $residents.Keys) {
        foreach ($p in (Resolve-ResidentWatch $residents[$k])) { Write-Output ("{0}`t{1}" -f $k, $p) }
    }
    exit 0
}

# -ReviveWatchdogOnce: drive Assert-WatchdogLoop ONCE, then exit. It sits here, beside
# -EmitWatchSet, for the same reasons and one more of its own: it must come BEFORE the
# single-instance guard, because the resident watcher's heartbeat is by definition fresh and
# the guard would make this one-shot bow out and revive nothing. It is also before every child
# launch, the guardian relaunch and the singleton reap, so it can be fired at a live fleet
# without perturbing it. It calls the SAME function the beat calls- an 'emit'-style copy would
# drift from the loop's real path and make the probe prove nothing.
#
# IT MUST EXIT 0, and not merely out of tidiness. Callers verify the revived interpreter with
# `& <exe> -c 'import cv2'`, but the store python resolves to C:\Program Files\WindowsApps\...,
# which is ACL-denied to an unelevated exec: PowerShell raises NativeCommandFailed and leaves
# $LASTEXITCODE UNTOUCHED- still holding whatever WE exited with. A non-zero exit here is read
# back, silently and wrongly, as "the revived loop runs an interpreter without cv2".
if ($ReviveWatchdogOnce) {
    Assert-WatchdogLoop -Immediate
    exit 0
}

# --- single instance: if another watcher beat very recently, don't double-poll ---
# -SelfTest is exempt: the resident watcher's heartbeat is by definition fresh, so an ungated probe
# would print "Another Baxter watcher is alive", `return`, and exit 0 having executed not one case-
# a green run that proved nothing. Same reasoning as -EmitWatchSet/-ReviveWatchdogOnce sitting above
# this guard; the selftest cannot, because it needs the functions defined below it.
if (-not $SelfTest -and -not $TriageWaitSelfTest -and (Test-Path $watchBeat)) {
    try {
        $line = Get-Content $watchBeat -Raw
        $stamp = [datetime]::Parse(($line -split "`t")[0])
        if (((Get-Date) - $stamp).TotalSeconds -lt 90 -and ($line -notmatch "stopped")) {
            Write-Host "Another Baxter watcher is alive (beat <90s ago) - exiting duplicate." -ForegroundColor Yellow
            return
        }
    } catch {}
}

if (-not $SelfTest -and -not $TriageWaitSelfTest) {
    Write-Host "Baxter watching (every ${IntervalSec}s, always-on; pauses only during fullscreen games). Ctrl+C to stop." -ForegroundColor Green
    Write-WatchLog "watcher started (interval ${IntervalSec}s, always-on, fullscreen-aware)"
}
$wasGaming = $false

$usagePy = "C:\Users\you\Documents\Python Scripts\utils\baxter_usage.py"
$stopFlag = Join-Path $vault ".baxter_stop"
# Grandmaster OFF (the owner's /off): free the PC to game. When present we SKIP the heavy,
# lag-causing spawns (triage claude worker, voice transcription, WhatsApp bridge) but
# KEEP the fast lane + heartbeat + usage-enforce alive so /on and his questions land.
$offFlag = Join-Path $vault ".baxter_off"

# HARD USAGE ENFORCEMENT (the owner, 5th July: "pause at 80 must ACTUALLY pause").
# Recompute the band + write/clear .baxter_stop, then KILL any runaway Baxter claude
# worker the band forbids. Called on EVERY beat slice (~5-15s)- NOT just once per
# outer loop- so a runaway dies within a slice of the meter crossing the line, not
# up to a full 60s interval later. This is the active backstop: a worker that ignores
# its own soft gate does not survive the next beat.
# A .baxter_override / .baxter_breach flag with a future 'until' is the owner's authorisation-
# true while it has not expired. The kill loop must respect these or it would slay the very
# work he just green-lit (blocked() already lets it through).
function Test-FlagActive($path) {
    if (-not (Test-Path $path)) { return $false }
    try { return ([datetime]((Get-Content $path -Raw | ConvertFrom-Json).until)) -gt (Get-Date) }
    catch { return $false }
}

# DO NOT "FIX" THE `& python` BELOW BY ROUTING IT THROUGH pythonw (measured, 9th July).
# The 09:47 flash incident blamed this line: a console-less parent calling python.exe every 15s
# was said to allocate a fresh console window each time. It does not. This watcher is spawned
# through Start-Hidden (CREATE_NO_WINDOW), which gives it a console WITH NO WINDOW- and `&`
# hands that console straight to the child, which allocates nothing. baxter_flashprobe.py
# watched 16 --enforce firings across 200s and 88 across 720s: zero windows, on-screen or off
# (mode `ps_ampersand_withconsole` reproduces it on demand).
# Swapping to pythonw would discard the exit code and stderr of every call in this file, three
# of which are baxter_say.py- so the duplicate-reap warning and the watchdog-revive ping would
# fail silently. The flash was never here. Leave it.
# Drop every governor-dropped .yield marker + grace deadline. Called wherever the big band
# stops applying- flag gone, breach on, override on, or no live big worker left to pause.
# A marker that outlives its band halts + re-queues its build on every gate check, forever.
function Clear-GovernorYieldSafely {
    try {
        . 'C:\Users\you\Documents\Python Scripts\utils\baxter_govstamp.ps1'
        Clear-GovernorYield -All
    } catch {}
}

function Invoke-UsageEnforce {
    # THE GOVERNOR'S HEARTBEAT MUST NEVER FAIL IN SILENCE (10th July). This line used to read
    #     try { & python $usagePy --enforce 2>$null | Out-Null } catch {}
    # the identical swallow that muted the reap ping and the watchdog-revive ping the same day:
    # bare 'python' resolved off PATH, stdout+stderr binned, $LASTEXITCODE never read, and a
    # catch {} eating the diagnostic. When enforce() never runs, .baxter_stop goes stale and NO
    # runaway worker is killed- the 80/90 wall silently opens and nothing says so. So resolve the
    # interpreter explicitly (Send-BaxterPing's proven shape, watch.ps1:237-239), KEEP the
    # '& $exe'-with-console model the flash tombstone above defends- NO Start-Process at this 15s
    # cadence, so no console flash- and PROVE the call ran: --enforce always prints 'band: <level>'.
    # Every other outcome is logged with its reason rather than swallowed. The fall-through to the
    # $stopFlag check below is unchanged: a blind beat still enforces the last-known band.
    $enfExe = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $enfExe) { $enfExe = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe" }
    if (-not $enfExe -or -not (Test-Path $enfExe)) {
        Write-WatchLog "usage-enforce NOT RUN- no python interpreter found; the governor is BLIND this beat"
    }
    elseif (-not (Test-Path $usagePy)) {
        Write-WatchLog "usage-enforce NOT RUN- no baxter_usage.py at $usagePy; the governor is BLIND this beat"
    }
    else {
        try {
            $enfOut = & $enfExe $usagePy --enforce
            if ($LASTEXITCODE -ne 0) {
                Write-WatchLog "usage-enforce FAILED- baxter_usage.py --enforce exit $LASTEXITCODE; the governor did not enforce this beat"
            }
            elseif ((([string]::Join("`n", @($enfOut)))) -notmatch 'band:') {
                Write-WatchLog "usage-enforce ran but printed no band line- the governor may not have enforced this beat"
            }
        }
        catch {
            Write-WatchLog "usage-enforce THREW- $($_.Exception.Message); the governor did not enforce this beat"
        }
    }
    if (Test-Path $stopFlag) {
        try {
            # --breach lifts EVERY band: no kills at all this beat, and no lane left standing
            # down under a marker the band no longer justifies.
            if (Test-FlagActive (Join-Path $vault ".baxter_breach")) { Clear-GovernorYieldSafely; return }
            # --override lifts only the 80-90 big band: spare big-task workers at 'big',
            # but still enforce the 'routine' (90) vital-only wall.
            $overrideOn = Test-FlagActive (Join-Path $vault ".baxter_override")
            $lvl = ((Get-Content $stopFlag -Raw | ConvertFrom-Json).level)
            # 3-STATE governor kill map (the owner, 8th July- flat 80/90, no 70, vitals NEVER
            # auto-killed). Mirrors baxter_usage.blocked's soft gates:
            #   'big'     (80-90) -> kill only BIG/project workers (resume-worker prompt names
            #                        the 'big-task' slot). Spare routine triage AND the fast
            #                        lane (vital- answers the owner).
            #   'routine' (90+)   -> BARE MINIMUM / vitals-only: also kill routine triage
            #                        workers (prompt names BAXTER_TRIAGE.md) AND any stray
            #                        big-task worker. STILL spare the fast lane + CoC- vitals
            #                        keep running; there is no 'kill vitals' tier any more.
            # ('floor' is retired: FLOOR_SESSION is unreachable so _band never returns it, and
            #  the fast lane is deliberately never a victim.)
            $victims = Get-CimInstance Win32_Process | Where-Object {
                $_.Name -eq 'claude.exe' -and $_.CommandLine -match '\s-p\s' -and (
                    ($lvl -eq 'routine' -and ($_.CommandLine -match 'BAXTER_TRIAGE' -or $_.CommandLine -match 'big-task')) -or
                    ($lvl -eq 'big' -and -not $overrideOn -and $_.CommandLine -match 'big-task')
                )
            }
            # SAY SO BEFORE YOU KILL (9th July). Stop-Process is indistinguishable from a real
            # crash downstream- exit 4294967295, empty tail- so stamp each victim's lane journal
            # halted_by=governor FIRST. _handle_failure reads the stamp ahead of the exit code and
            # treats the build as HELD: re-queued at p2, retry/repair counters untouched, no repair
            # worker. claude.exe's PARENT is the python resume worker, whose pid the journal carries.
            #
            # AND AT 'big', ASK BEFORE YOU KILL (9th July, second half). The contract says 80-90%
            # means big tasks PAUSE INTO THE QUEUE. A build already halts cleanly at its own
            # `--check project --lane` gate, so a Stop-Process on the band edge is both wrong and
            # unnecessary: it skips --halt, loses mid-flight state, and can leave half-written code
            # live ([[halfbuilt-code-goes-live-instantly]]). So the governor drops the lane's .yield
            # marker and gives the worker a grace window to stand down on its own. Only a worker
            # that IGNORES the marker is force-killed, and that kill says so.
            #
            # 'routine' (90%+, vitals-only) is untouched and kills on sight. That wall must bite
            # immediately- a grace window there lets usage run away exactly when headroom is scarcest.
            #
            # The catch stays non-fatal but never SILENT: if the shim is missing or throws, we fall
            # back to the old unconditional kill so the band keeps its teeth, and we say loudly that
            # these kills will be misread as crashes.
            # No live big worker to pause (nobody matched, or --override spared them all): any
            # marker still on disk is stale, and stale is a silent lane-killer. Sweep and stop.
            if ($lvl -eq 'big' -and @($victims).Count -eq 0) { Clear-GovernorYieldSafely; return }
            try {
                . 'C:\Users\you\Documents\Python Scripts\utils\baxter_govstamp.ps1'
                if ($lvl -eq 'big') {
                    $graceS = Get-GovernorGraceSeconds
                    foreach ($p in (Invoke-GovernorYield -Victims $victims -Level $lvl)) {
                        Write-WatchLog "yield dropped (big): PID $p has $graceS s to halt cleanly"
                    }
                    foreach ($v in $victims) {
                        if (Test-GovernorGraceExpired -Victim $v) {
                            try { Stop-Process -Id $v.ProcessId -Force -Confirm:$false
                                  Write-WatchLog "HARD STOP: worker ignored yield- killed PID $($v.ProcessId)" } catch {}
                            Clear-GovernorYield -ProcId $v.ProcessId
                        }
                    }
                } else {
                    Stamp-GovernorKill ($victims | Select-Object -ExpandProperty ParentProcessId) $lvl
                    foreach ($v in $victims) {
                        try { Stop-Process -Id $v.ProcessId -Force -Confirm:$false
                              Write-WatchLog "HARD STOP ($lvl): killed runaway worker PID $($v.ProcessId)" } catch {}
                    }
                }
            } catch {
                Write-WatchLog "govstamp FAILED ($lvl): $($_.Exception.Message)- falling back to the hard kill; these kills will be misread as crashes"
                foreach ($v in $victims) {
                    try { Stop-Process -Id $v.ProcessId -Force -Confirm:$false
                          Write-WatchLog "HARD STOP ($lvl): killed runaway worker PID $($v.ProcessId)" } catch {}
                }
            }
        } catch {}
    }
    else {
        # THE BAND HAS CLEARED. A governor yield marker that outlives its band is a silent
        # lane-killer: the build halts and re-queues on every subsequent gate check, forever.
        # Sweep our own markers (never the delegator's clash markers) and forget every deadline.
        try {
            . 'C:\Users\you\Documents\Python Scripts\utils\baxter_govstamp.ps1'
            Clear-GovernorYield -All
        } catch {}
    }
}

# CHILD HOT-RELOAD (9 Jul - the fifth "full pass" strike, and the answer to the owner's
# "but this only fixes this instance"). A resident child reads its source ONCE, at launch:
# an on-disk prompt/logic fix silently does nothing until someone kills the process by hand.
# baxter_slash.py's system prompt was corrected at 00:42; the instance launched at 23:34 the
# night before kept serving the pre-fix string for another 22 minutes, because the launch
# block below only ever spawns it when NO process matches. The watcher gave ITSELF exactly
# this guard on 6 Jul (see SELF-RESTART, next block) - same failure class, same comment - and
# never propagated it to any of its children. Every long-lived child had it latent.
#
# The signal is the child's OWN start time against its sources' mtime: a process that started
# BEFORE its newest source was written is running dead code. That needs no launch-time
# bookkeeping, survives a watcher restart for free, and catches a child that was already
# stale before this guard existed (PID 50880 would have been caught on the first beat).
# .baxter_child_mtimes.json records the observed mtimes and the last bounce purely as a
# backstop: a child whose relaunch keeps failing must not be re-killed every single beat,
# so one child bounces at most once per 5 minutes.
#
# NEVER bounce mid-reply: an entry with a Busy pattern is skipped while a matching worker is
# in flight (the listener's detached baxter_reply_worker) and takes its bounce on the next
# idle beat. Relaunch is direct rather than left to the supervisor blocks, so the new code is
# live within the same beat. The CoC pair has a second net regardless: coc_bot/watchdog.py is
# a scheduled task that relaunches any dead daemon within 5 min.
function Read-ChildState($Path = $childState) {
    $h = @{}
    if (Test-Path $Path) {
        try {
            $o = Get-Content $Path -Raw | ConvertFrom-Json
            foreach ($p in $o.PSObject.Properties) {
                # EVERY key MUST survive this round-trip, and this list is hand-written, so a new
                # key is invisible until someone adds it here. bounced_for taught that: dropping it
                # silently disabled the 5-minute anti-loop floor below, which compares it against
                # the current source stamp, and a child whose relaunch kept failing was re-killed
                # on every beat, for ever, with nothing said. stale_since is the same shape of trap-
                # lose it and the quiet gate below re-arms from scratch each beat, so the MaxDeferMin
                # ceiling never fires and a long burn defers the reload indefinitely.
                $h[$p.Name] = @{
                    mtime       = $p.Value.mtime
                    bounced     = $p.Value.bounced
                    bounced_for = $p.Value.bounced_for
                    stale_since = $p.Value.stale_since
                }
            }
        } catch {}
    }
    return $h
}

# PURE: no clock read, no CIM, no logging, no state. Everything it needs is an argument, so the
# selftest can drive every branch without a fleet. Bounce iff the sources have gone QUIET for
# QuietSec (the burn is over), or we have been deferring for MaxDeferMin (the ceiling: a burn
# longer than the ceiling still gets exactly one reload rather than none).
function Test-ResidentSettling {
    param(
        [Parameter(Mandatory)][datetime]$Newest,
        [Parameter(Mandatory)][datetime]$Now,
        $StaleSince,
        [int]$QuietSec,
        [int]$MaxDeferMin
    )
    $quiet = ($Now - $Newest).TotalSeconds
    if ($quiet -ge $QuietSec) {
        return @{ Bounce = $true; Reason = ("quiet {0:n0}s of {1}s" -f $quiet, $QuietSec) }
    }
    # No stale_since yet = this is the first beat we found it stale, so nothing has been deferred.
    $since = $Now
    if ($StaleSince) {
        if ($StaleSince -is [datetime]) { $since = $StaleSince }
        else { try { $since = [datetime]::Parse([string]$StaleSince) } catch { $since = $Now } }
    }
    $deferred = ($Now - $since).TotalMinutes
    if ($deferred -ge $MaxDeferMin) {
        return @{ Bounce = $true; Reason = ("ceiling {0:n1}m of {1}m deferred" -f $deferred, $MaxDeferMin) }
    }
    return @{ Bounce = $false; Reason = ("{0:n0}s quiet of {1}s, deferring" -f $quiet, $QuietSec) }
}

$script:lastChildReload = [datetime]::MinValue

# The DEFAULT outward paths. Under -SelfTest they must never run: a selftest that forgets to inject
# would Stop-Process the live listener and Start-Hidden a second one. So they abort loudly instead,
# and bump a counter first- the function's own try/catch would otherwise swallow the throw and the
# selftest would pass having killed a resident. Per [[selftests-stub-every-outward-path]]: a lane
# selftest once reached _say and posted a false alert to the owner.
$script:DefaultKiller = {
    param($TargetPid)
    if ($script:SelfTestMode) {
        $script:RealOutwardCalls++
        throw "SELFTEST GUARD: default Killer reached for PID $TargetPid - the test failed to inject -Killer"
    }
    Stop-Process -Id $TargetPid -Force -Confirm:$false
}
$script:DefaultSpawner = {
    param($c)
    if ($script:SelfTestMode) {
        $script:RealOutwardCalls++
        throw "SELFTEST GUARD: default Spawner reached for $($c.File) - the test failed to inject -Spawner"
    }
    Start-Hidden -FilePath $c.File -Arguments $c.Arguments -WorkingDirectory $c.Cwd -PassThru
}
$script:DefaultProcSource = {
    @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine })
}

function Invoke-ChildReload {
    param(
        $off,
        # Injection seams (all default to the real thing). Every clock read, process enumeration,
        # kill and spawn below goes through one of these, so -SelfTest can drive the whole function
        # against a synthetic fleet at a synthetic time without touching a single live process.
        [datetime]$Now = (Get-Date),
        [scriptblock]$ProcSource = $script:DefaultProcSource,
        [scriptblock]$Killer     = $script:DefaultKiller,
        [scriptblock]$Spawner    = $script:DefaultSpawner,
        $Residents = $residents,
        $StatePath = $childState,
        # Distinct names on purpose: a param named $QuietSec could not default to the script's
        # $QuietSec (it would shadow itself and read back as 0, disabling the gate).
        [int]$SettleQuietSec    = $QuietSec,
        [int]$SettleMaxDeferMin = $MaxDeferMin
    )
    $script:lastChildReload = $Now
    try {
        $procs = @(& $ProcSource)
        # A flaky/empty CIM read must never be read as "everything is down".
        if ($procs.Count -eq 0) { return }

        $state = Read-ChildState $StatePath
        $dirty = $false
        foreach ($k in $Residents.Keys) {
            $c = $Residents[$k]
            if ($off -and $c.SkipWhenOff) { continue }
            if ($c.SkipIf -and (& $c.SkipIf)) { continue }

            # Resolved per beat, not once at startup: the watcher runs for days, and a module
            # added to baxter_slash's imports at noon must be watched by teatime. The mtime
            # cache inside Get-WatchSet keeps this to one python spawn per source edit.
            $srcs = @((Resolve-ResidentWatch $c) | Where-Object { Test-Path $_ })
            if ($srcs.Count -eq 0) { continue }
            # Sort-Object, not Measure-Object -Maximum: PS 5.1's Measure-Object only maxes numerics.
            $newest = @($srcs | ForEach-Object { (Get-Item $_).LastWriteTime } | Sort-Object -Descending)[0]

            # A source stamped in the FUTURE (clock skew, a bad copy) can never be satisfied by
            # any relaunch - it would bounce the child on every beat forever. Leave it alone.
            if ($newest -gt $Now.AddMinutes(2)) {
                Write-WatchLog "$k has a source dated in the future ($($newest.ToString('HH:mm:ss'))) - not reloading"
                continue
            }

            if (-not $state.ContainsKey($k)) { $state[$k] = @{ mtime = $null; bounced = $null; bounced_for = $null; stale_since = $null } }
            $stamp = $newest.ToString('o')
            if ($state[$k].mtime -ne $stamp) { $state[$k].mtime = $stamp; $dirty = $true }

            $live = @($procs | Where-Object { $_.Name -match $c.Name -and $_.CommandLine -match $c.Cmd })
            if ($live.Count -eq 0) { continue }              # down: its own supervisor block owns the relaunch
            # Oldest instance wins: if a child is somehow doubled and either half is stale, the
            # bounce below reaps both and relaunches exactly one.
            $started = @($live | ForEach-Object { $_.CreationDate } | Sort-Object)[0]
            if ($started -ge $newest) {
                # Fresh. Clear any deferral clock, or the NEXT stale episode inherits this one's
                # age and trips the MaxDeferMin ceiling on its very first beat.
                if ($state[$k].stale_since) { $state[$k].stale_since = $null; $dirty = $true }
                continue
            }

            # SETTLING GATE (the fix). A build burn rewrites several of the listener's sources
            # seconds apart; the guard below used to bounce it the first beat each one landed, so
            # one logical change cost three kills and three cold starts, mid-burn. Hold the bounce
            # until the sources have been quiet for QuietSec- and no longer than MaxDeferMin, so a
            # burn that never stops still gets exactly one reload rather than none.
            if (-not $state[$k].stale_since) { $state[$k].stale_since = $Now.ToString('o'); $dirty = $true }
            $settle = Test-ResidentSettling -Newest $newest -Now $Now -StaleSince $state[$k].stale_since `
                                            -QuietSec $SettleQuietSec -MaxDeferMin $SettleMaxDeferMin
            if (-not $settle.Bounce) {
                Write-WatchLog "$k has newer code on disk - settling ($($settle.Reason))"
                continue
            }

            # Anti-loop floor, keyed to the mtime we last bounced FOR - not to wall-clock alone.
            # Still stale for the SAME source stamp we already bounced on means the relaunch
            # isn't taking (bad interpreter, child dies on import): back off 5 min rather than
            # kill it every beat. A NEWER edit is a fresh event and bounces at once - the owner
            # fixing a file twice in a minute must not wait, that is this whole task's bug.
            if ($state[$k].bounced -and $state[$k].bounced_for -eq $stamp) {
                try {
                    if (($Now - [datetime]::Parse($state[$k].bounced)).TotalMinutes -lt 5) {
                        Write-WatchLog "$k still stale after a bounce for the same source - holding off 5m (relaunch failing?)"
                        continue
                    }
                } catch {}
            }
            if ($c.Busy) {
                $busy = @($procs | Where-Object { $_.Name -match $c.BusyName -and $_.CommandLine -match $c.Busy })
                if ($busy.Count -gt 0) {
                    Write-WatchLog "$k has newer code on disk - deferring reload, $($busy.Count) worker(s) in flight"
                    continue
                }
            }

            $old = ($live | ForEach-Object { $_.ProcessId }) -join ','
            foreach ($p in $live) {
                # The guard's throw must NOT be swallowed here, or a selftest that forgot to inject
                # would kill a live resident and still report green.
                try { & $Killer $p.ProcessId } catch { if ($script:SelfTestMode) { throw } }
            }
            if (-not $script:SelfTestMode) { Start-Sleep -Milliseconds 700 }
            # The bounce is happening: the deferral clock has done its job and must reset, else the
            # next stale episode starts life already at the ceiling.
            $state[$k].bounced = $Now.ToString('o'); $state[$k].bounced_for = $stamp
            $state[$k].stale_since = $null; $dirty = $true
            try {
                $new = & $Spawner $c
                if (-not $script:SelfTestMode) { Start-Sleep -Milliseconds 1500 }
                if ($new -and -not $new.HasExited) {
                    Write-WatchLog ("$k reloaded: code written {0}, process had started {1} - killed PID $old, now PID $($new.Id)" -f $newest.ToString('HH:mm:ss'), $started.ToString('HH:mm:ss'))
                } else {
                    Write-WatchLog "$k reload FAILED - relaunched process died immediately (was PID $old)"
                }
            } catch {
                if ($script:SelfTestMode) { throw }   # never swallow the Spawner kill-guard
                Write-WatchLog "$k reload relaunch FAILED (was PID $old): $_"
            }
        }
        if ($dirty) { try { [IO.File]::WriteAllText($StatePath, ($state | ConvertTo-Json -Depth 4), $enc) } catch {} }
    } catch {
        # In production a reload fault must never take the watcher down. Under -SelfTest the
        # opposite is true: a swallowed fault is a green test that proved nothing.
        if ($script:SelfTestMode) { throw }
    }
}

# The reload check used to run ONCE per outer beat - and the outer beat blocks on the triage
# child, which routinely takes minutes ([[watcher-blocks-on-triage]]). So "an on-disk fix loads
# within one beat" was only true on an idle loop: during a triage cycle a corrected source could
# sit unloaded for 5+ minutes, which is the very staleness this whole guard exists to prevent.
# Call this from the hot paths instead. The 15s floor bounds the cost to at most one CIM
# enumeration per slice however many places call it, and Get-WatchSet's mtime cache means the
# derivation itself spawns python only when a watched source actually changed.
function Invoke-ChildReloadThrottled($off) {
    if (((Get-Date) - $script:lastChildReload).TotalSeconds -lt 15) { return }
    Invoke-ChildReload $off
}

# --- the triage-wait loop -----------------------------------------------------------------------
# A triage cycle blocks the outer beat for MINUTES (it waits on claude workers), so anything that
# only the slice loop spawns simply does not run for that whole cycle. That starved the reaction
# watcher- whose ONLY spawner is the slice loop, so a dropped 👀/⚙️/✅ could not self-heal until
# triage exited- and, more mildly, the /usage poller and the fast lane. All three now fire on this
# loop's 5s beat too. Each is a short-lived, self-locking one-shot, so a spawn per beat is safe:
# the second invocation finds the lock held and exits.
#
# PURE-ish: every outward act goes through an injected scriptblock, so -TriageWaitSelfTest can drive
# the real loop against a real child process without spawning one live one.
$script:DefaultWaitSpawner = {
    param($s)
    if ($script:SelfTestMode) {
        $script:RealOutwardCalls++
        throw "SELFTEST GUARD: default wait-loop Spawner reached for $($s.Name) - the test failed to inject -Spawner"
    }
    Start-Hidden -FilePath $s.File -Arguments $s.Arguments
}

# The beat's spawn set, in fire order. $react leads deliberately: it is the only one of the four
# with no other spawner anywhere in the fleet, and the reaction lifecycle is mandatory.
function Get-TriageWaitSpawns {
    param(
        [Parameter(Mandatory)][string]$Reminders,
        [Parameter(Mandatory)][string]$React,
        [Parameter(Mandatory)][string]$UsageCmd,
        [Parameter(Mandatory)][string]$Fast
    )
    return @(
        @{ Name = 'react';     File = 'python'; Arguments = "`"$React`"" }
        @{ Name = 'reminders'; File = 'python'; Arguments = "`"$Reminders`" --fire" }
        @{ Name = 'usage_cmd'; File = 'python'; Arguments = "`"$UsageCmd`"" }
        @{ Name = 'fast';      File = 'python'; Arguments = "`"$Fast`"" }
    )
}

function Invoke-TriageWaitLoop {
    param(
        [Parameter(Mandatory)]$Proc,
        [Parameter(Mandatory)]$Spawns,
        $off,
        [scriptblock]$Spawner  = $script:DefaultWaitSpawner,
        [scriptblock]$Beat     = { Write-Beat "working"; Invoke-UsageEnforce },
        [scriptblock]$Maintain = { param($o) Invoke-ChildReloadThrottled $o },
        [scriptblock]$Sleeper  = { Start-Sleep -Seconds 5 },
        [int]$MaxWaitSec       = 1200,
        [scriptblock]$Clock    = { Get-Date },
        [scriptblock]$Killer   = { param($p) Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue },
        [string]$HeartbeatPath = $script:triageBeat,
        [int]$StaleAlertSec    = 1800,
        # INWARD by default, deliberately. A stale-cycle alarm is a log line, not a Discord ping:
        # the loop below does its own Write-WatchLog, so this default is a true no-op and needs no
        # SELFTEST GUARD throw the way $script:DefaultKiller and $script:DefaultWaitSpawner do.
        [scriptblock]$Alerter  = { param($m) }
    )
    # A null $Proc means the spawn never produced a process. Returning hands control back to the
    # outer loop, which spawns a fresh triage on the next beat. Spinning here on `-not $null` (which
    # is $true, forever) is how the whole cycle went dark for 8h on 10th July.
    if (-not $Proc) { Write-WatchLog "triage spawn returned no process- skipping wait"; return }

    # THE BOUND. `while (-not $Proc.HasExited)` alone gives one wedged child veto over the entire
    # fleet: run() carries the queue pump, the lane reaper, the briefs and inbox filing, so a triage
    # that never exits stops all of them and NOTHING notices- the watcher's own heartbeat stays
    # fresh, because this loop is what writes it.
    #
    # MEASURED, 10th July: a triage child spawned 00:37:44 burned 28,995s of cpu in 29,257s of wall-
    # one core pinned at 99% for eight hours- while this loop waited on it. No cycle completed, and
    # the queue sat at 24 tasks, 0 running, until the owner noticed by eye. That is the whole of what was
    # measured. NOT measured: which frame spun. Nobody attached a debugger before the child was
    # killed. An earlier note here named baxter_stuck_doctor's process-tree walk as the cause on
    # process accounting alone; re-measured against the live fleet that walk took 6.4ms per snapshot
    # over 456 processes, and the spin did not reproduce.
    #
    # The probe now arms faulthandler before each pass, so the next wedge dumps every thread's stack
    # to .baxter_spin_dump.txt and aborts. Read that file: it names the frame this comment cannot.
    # The bound is defence, not diagnosis- a wedged child costs one cycle, not the day.
    $script:waitStaleAlerted = $false
    $start = & $Clock
    while (-not $Proc.HasExited) {
        if ($MaxWaitSec -gt 0 -and ((& $Clock) - $start).TotalSeconds -ge $MaxWaitSec) {
            # The unconditional `break` IS the once-only guarantee: the Killer fires exactly once per
            # invocation, and the wait ends whether that kill succeeded, threw, or left the child
            # alive. A Killer that throws must still break- otherwise the very wedge this bound
            # exists for outlives its own kill. (A `$killed` latch was tried here and removed: with
            # the break unconditional, the second call it guarded was unreachable, and no mutation
            # of it could turn a single selftest case red. Dead guards read as protection.)
            Write-WatchLog "triage child wedged (>${MaxWaitSec}s)- killing pid $($Proc.Id) and resuming the cycle"
            try { & $Killer $Proc } catch { Write-WatchLog "failed to kill wedged triage: $_" }
            break
        }
        # THE ALARM THAT WAS MISSING FOR 8h05m. It is NOT a kill gate: baxter_triage.py stamps the
        # heartbeat in run()'s finally, so during a healthy multi-minute cycle it is stale BY
        # DEFINITION and only goes fresh when the child exits. Killing on staleness would murder
        # every long claude triage. The kill stays governed solely by MaxWaitSec above. A missing or
        # unparseable stamp is UNKNOWN, never stale- a fresh vault has none, and must not alarm.
        if (-not $script:waitStaleAlerted) {
            $age = $null
            try { $age = ((& $Clock) - [datetime]::Parse([IO.File]::ReadAllText($HeartbeatPath).Trim())).TotalSeconds } catch { $age = $null }
            if ($null -ne $age -and $age -ge $StaleAlertSec) {
                $script:waitStaleAlerted = $true
                $msg = "no triage cycle has completed for $([math]::Round($age))s (ceiling ${StaleAlertSec}s)- the proactive beat is dark"
                Write-WatchLog $msg
                try { & $Alerter $msg } catch {}
            }
        }
        & $Beat
        # One try/catch per spawn: a python that fails to launch must not skip the three after it.
        # The guard above counts its breach BEFORE throwing, so a swallow here still fails the exam.
        foreach ($s in $Spawns) { try { & $Spawner $s } catch {} }
        & $Maintain $off
        & $Sleeper
    }
}

# -SelfTest: drive the settling gate and Invoke-ChildReload end to end against a synthetic fleet at
# a synthetic time, print one `SELFTEST case=<name> PASS|FAIL` line per case, and exit non-zero if
# any case failed OR if any DEFAULT outward path was reached. It launches nothing, kills nothing and
# writes no fleet state: the child-state file is a temp path and the log is in memory.
#
# It sits AFTER Invoke-ChildReload/Throttled because it calls them. The single-instance guard, the
# banner and the 'watcher started' line are gated on `-not $SelfTest` so the probe reaches here
# rather than bowing out to the live watcher's fresh heartbeat and exiting 0 having tested nothing.
function Invoke-WatchSelfTest {
    $script:SelfTestMode = $true
    $script:RealOutwardCalls = 0
    $script:selfTestFails = 0
    $t0 = [datetime]::Parse('2026-07-09T12:00:00')

    # [Console]::Out, NOT Write-Output. A Write-Output inside a function feeds that function's
    # PIPELINE, so every marker below became part of Invoke-WatchSelfTest's return value instead of
    # reaching stdout- and `exit (<that array>)` then exited 0, silently, having printed nothing.
    # An rc-only exam would have called that a pass. Write straight to the console stream.
    function Say-Test([string]$line) { [Console]::Out.WriteLine($line) }

    function Assert-Case([string]$name, [bool]$ok, [string]$detail = '') {
        if ($ok) { Say-Test "SELFTEST case=$name PASS" }
        else { Say-Test "SELFTEST case=$name FAIL $detail"; $script:selfTestFails++ }
    }

    # --- pure gate: sources still being written -> defer ---------------------------------------
    $r = Test-ResidentSettling -Newest $t0.AddSeconds(-10) -Now $t0 -StaleSince $t0.AddSeconds(-10) `
                               -QuietSec $QuietSec -MaxDeferMin $MaxDeferMin
    Assert-Case 'settle-quiet-defer' (-not $r.Bounce) "Bounce=$($r.Bounce) reason=$($r.Reason) QuietSec=$QuietSec"

    # --- pure gate: sources have gone quiet -> bounce -------------------------------------------
    $r = Test-ResidentSettling -Newest $t0.AddSeconds(-($QuietSec + 5)) -Now $t0 -StaleSince $t0.AddSeconds(-($QuietSec + 5)) `
                               -QuietSec $QuietSec -MaxDeferMin $MaxDeferMin
    Assert-Case 'settle-quiet-elapsed' ($r.Bounce -and $r.Reason -match 'quiet') "Bounce=$($r.Bounce) reason=$($r.Reason)"

    # --- pure gate: still noisy, but the defer ceiling is reached -> bounce anyway ---------------
    $r = Test-ResidentSettling -Newest $t0.AddSeconds(-1) -Now $t0 -StaleSince $t0.AddMinutes(-($MaxDeferMin + 1)) `
                               -QuietSec $QuietSec -MaxDeferMin $MaxDeferMin
    Assert-Case 'settle-ceiling' ($r.Bounce -and $r.Reason -match 'ceiling') "Bounce=$($r.Bounce) reason=$($r.Reason)"

    # --- kill guard: the DEFAULT killer/spawner must abort, loudly, under selftest ---------------
    $before = $script:RealOutwardCalls
    $killerThrew = $false; $spawnerThrew = $false
    try { & $script:DefaultKiller 999999 } catch { $killerThrew = $true }
    try { & $script:DefaultSpawner @{ File = 'python'; Arguments = ''; Cwd = '.' } } catch { $spawnerThrew = $true }
    $guardOk = $killerThrew -and $spawnerThrew -and (($script:RealOutwardCalls - $before) -eq 2)
    Assert-Case 'killguard' $guardOk "killerThrew=$killerThrew spawnerThrew=$spawnerThrew calls=$($script:RealOutwardCalls - $before)"
    $script:RealOutwardCalls = 0   # those two breaches were deliberate; only UNINTENDED ones count now

    # --- integration: the REAL Invoke-ChildReload against a synthetic stale resident -------------
    # Real state round-trip, real gate, real control flow. Only the clock, the process list, the
    # kill and the spawn are stubbed. A kill here means a live listener would have been bounced.
    $tmpDir = Join-Path ([IO.Path]::GetTempPath()) ("bxwatch-selftest-" + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $tmpDir -Force | Out-Null
    try {
        $fakeSrc = Join-Path $tmpDir 'fake_resident.py'
        Set-Content -Path $fakeSrc -Value '# selftest source' -Encoding UTF8
        $statePath = Join-Path $tmpDir 'child_state.json'
        $srcTime = (Get-Item $fakeSrc).LastWriteTime

        # A literal Watch (not WatchRoot) so Resolve-ResidentWatch returns the path without spawning python.
        $fakeResidents = [ordered]@{
            'fake_resident' = @{
                Watch = @($fakeSrc)
                Name  = 'python\.exe'; Cmd = 'fake_resident\.py'
                File  = 'python'; Arguments = "`"$fakeSrc`""; Cwd = $tmpDir
            }
        }
        # Started 10 minutes before the source was written: stale, so it needs a reload.
        $fakeProc = [pscustomobject]@{ Name = 'python.exe'; CommandLine = "python `"$fakeSrc`""
                                       ProcessId = 424242; CreationDate = $srcTime.AddMinutes(-10) }
        $procs = { @($fakeProc) }.GetNewClosure()

        $killed  = New-Object Collections.Generic.List[int]
        $spawned = New-Object Collections.Generic.List[string]
        $killer  = { param($TargetPid) [void]$killed.Add([int]$TargetPid) }.GetNewClosure()
        $spawner = { param($c) [void]$spawned.Add([string]$c.File)
                     [pscustomobject]@{ HasExited = $false; Id = 777 } }.GetNewClosure()

        # (a) NOISY: 10s since the last write, well inside QuietSec -> defer, no kill, no spawn.
        $script:SelfTestLog.Clear()
        Invoke-ChildReload $false -Now $srcTime.AddSeconds(10) -ProcSource $procs -Killer $killer `
            -Spawner $spawner -Residents $fakeResidents -StatePath $statePath `
            -SettleQuietSec $QuietSec -SettleMaxDeferMin $MaxDeferMin | Out-Null
        $log = ($script:SelfTestLog -join ' | ')
        $deferOk = ($killed.Count -eq 0) -and ($spawned.Count -eq 0) -and ($log -match 'settling')
        Assert-Case 'reload-defers' $deferOk "killed=$($killed.Count) spawned=$($spawned.Count) log=[$log]"

        # stale_since must survive Read-ChildState's hand-written key list, or the ceiling never fires.
        $persisted = $false
        try { $persisted = [bool]((Get-Content $statePath -Raw | ConvertFrom-Json).'fake_resident'.stale_since) } catch {}
        Assert-Case 'stale-since-persisted' $persisted "state=$(if (Test-Path $statePath) { Get-Content $statePath -Raw } else { 'no state file' })"

        # (b) QUIET: QuietSec+5s since the last write -> exactly one kill and exactly one spawn.
        $killed.Clear(); $spawned.Clear(); $script:SelfTestLog.Clear()
        Invoke-ChildReload $false -Now $srcTime.AddSeconds($QuietSec + 5) -ProcSource $procs -Killer $killer `
            -Spawner $spawner -Residents $fakeResidents -StatePath $statePath `
            -SettleQuietSec $QuietSec -SettleMaxDeferMin $MaxDeferMin | Out-Null
        $log = ($script:SelfTestLog -join ' | ')
        $bounceOk = ($killed.Count -eq 1) -and ($killed[0] -eq 424242) -and ($spawned.Count -eq 1) -and ($log -match 'reloaded')
        Assert-Case 'reload-bounces' $bounceOk "killed=$($killed -join ',') spawned=$($spawned.Count) log=[$log]"
    } finally {
        try { Remove-Item -Recurse -Force $tmpDir -ErrorAction SilentlyContinue } catch {}
    }

    $failures = $script:selfTestFails
    if ($script:RealOutwardCalls -gt 0) {
        Say-Test "SELFTEST GUARD BREACH: $($script:RealOutwardCalls) real outward call(s) reached a default scriptblock"
        $failures++
    }
    if ($failures -eq 0) { Say-Test "SELFTEST RESULT PASS"; return 0 }
    Say-Test "SELFTEST RESULT FAIL ($failures failing case(s))"
    return 1
}

if ($SelfTest) { exit (Invoke-WatchSelfTest) }

# -TriageWaitSelfTest: prove the four one-shots fire on EVERY beat of the loop that runs while the
# triage child works- the loop that, until 9th July, fired only the reminder poller and left the
# reaction watcher (its sole spawner being the unreachable slice loop) starved for the whole cycle.
# Six behavioural cases plus one regression guard on the comment that used to claim otherwise. The
# last behavioural case drives the REAL loop against a REAL sleeping child, so "during the block"
# is observed against a live HasExited, not asserted about a synthetic one.
function Invoke-TriageWaitSelfTest {
    $script:SelfTestMode = $true
    $script:RealOutwardCalls = 0
    $script:selfTestFails = 0

    function Say-Test([string]$line) { [Console]::Out.WriteLine($line) }
    function Assert-Case([string]$name, [bool]$ok, [string]$detail = '') {
        if ($ok) { Say-Test "TRIAGEWAIT case=$name PASS" }
        else { Say-Test "TRIAGEWAIT case=$name FAIL $detail"; $script:selfTestFails++ }
    }

    $spawns = Get-TriageWaitSpawns -Reminders 'R.py' -React 'C.py' -UsageCmd 'U.py' -Fast 'F.py'
    $names  = @($spawns | ForEach-Object { $_.Name })

    # --- the set is complete: a hoist that drops one of the four is the whole bug, again ----------
    $complete = (@('react','reminders','usage_cmd','fast') | Where-Object { $names -notcontains $_ }).Count -eq 0
    Assert-Case 'spawn-set-complete' ($complete -and $names.Count -eq 4) "names=$($names -join ',')"

    # --- $react leads: it is the only one with no other spawner in the fleet ----------------------
    $ri = [array]::IndexOf($names, 'react')
    $orderOk = ($ri -eq 0) -and ($ri -lt [array]::IndexOf($names, 'usage_cmd')) -and ($ri -lt [array]::IndexOf($names, 'fast'))
    Assert-Case 'react-spawned-first' $orderOk "order=$($names -join ',')"

    # --- the reminder poller keeps its --fire; every spawn quotes its script path -----------------
    $remArg  = ($spawns | Where-Object { $_.Name -eq 'reminders' }).Arguments
    $argsOk  = ($remArg -match '--fire') -and (@($spawns | Where-Object { $_.Arguments -notmatch '^"' }).Count -eq 0) `
               -and (@($spawns | Where-Object { $_.File -ne 'python' }).Count -eq 0)
    Assert-Case 'spawn-args-well-formed' $argsOk "reminders=[$remArg]"

    # --- an already-exited child spawns nothing: the loop must not fire a beat it never entered ---
    $spawned = New-Object Collections.Generic.List[string]
    $stub    = { param($s) [void]$spawned.Add([string]$s.Name) }.GetNewClosure()
    $noop    = { }
    $noopM   = { param($o) }
    Invoke-TriageWaitLoop -Proc ([pscustomobject]@{ HasExited = $true }) -Spawns $spawns -off $false `
        -Spawner $stub -Beat $noop -Maintain $noopM -Sleeper $noop
    Assert-Case 'exited-child-no-spawn' ($spawned.Count -eq 0) "spawned=$($spawned.Count)"

    # --- synthetic child, 3 beats: all four fire on EVERY beat, and the beat/maintain hooks run ---
    # $script:-scoped counters, NOT locals: GetNewClosure() snapshots a scalar by value, so a
    # `$local++` inside a closure increments the closure's private copy and reads back as 0.
    $script:twBeats = 0
    $script:twMaint = 0
    $spawned.Clear()
    $fakeProc = [pscustomobject]@{}
    $fakeProc | Add-Member -MemberType ScriptProperty -Name HasExited -Value { $script:twBeats -ge 3 }
    $beat  = { $script:twBeats++ }
    $maint = { param($o) $script:twMaint++ }
    Invoke-TriageWaitLoop -Proc $fakeProc -Spawns $spawns -off $false `
        -Spawner $stub -Beat $beat -Maintain $maint -Sleeper $noop
    $counts = @{}
    foreach ($n in $spawned) { $counts[$n] = 1 + [int]$counts[$n] }
    $everyBeat = ($counts['react'] -eq 3) -and ($counts['reminders'] -eq 3) -and `
                 ($counts['usage_cmd'] -eq 3) -and ($counts['fast'] -eq 3) -and ($spawned.Count -eq 12)
    Assert-Case 'all-four-every-beat' $everyBeat "total=$($spawned.Count) react=$($counts['react']) ucmd=$($counts['usage_cmd']) fast=$($counts['fast']) rem=$($counts['reminders'])"
    Assert-Case 'beat-and-maintain-run' (($script:twBeats -eq 3) -and ($script:twMaint -eq 3)) "beats=$($script:twBeats) maintain=$($script:twMaint)"

    # --- a throwing spawn must not skip the spawns queued behind it -------------------------------
    $script:twBeats = 0; $spawned.Clear()
    $fakeProc2 = [pscustomobject]@{}
    $fakeProc2 | Add-Member -MemberType ScriptProperty -Name HasExited -Value { $script:twBeats -ge 1 }
    $throwOnReact = { param($s) if ($s.Name -eq 'react') { throw 'launch failed' }; [void]$spawned.Add([string]$s.Name) }.GetNewClosure()
    $beat1 = { $script:twBeats++ }
    Invoke-TriageWaitLoop -Proc $fakeProc2 -Spawns $spawns -off $false `
        -Spawner $throwOnReact -Beat $beat1 -Maintain $noopM -Sleeper $noop
    Assert-Case 'failed-spawn-does-not-skip-rest' ($spawned.Count -eq 3) "survivors=$($spawned -join ',')"

    # --- THE REAL THING: a real, deliberately long child; a real HasExited; a real 1s sleep. ------
    # The stubbed spawner records WHEN each spawn happened, so we can assert the reaction watcher
    # was fired at least twice while the child was still running- the exact starvation this fixes.
    $spawned.Clear()
    $reactTimes = New-Object Collections.Generic.List[datetime]
    $timedStub  = { param($s) [void]$spawned.Add([string]$s.Name)
                    if ($s.Name -eq 'react') { [void]$reactTimes.Add((Get-Date)) } }.GetNewClosure()
    $realOk = $false; $detail = ''
    try {
        $child = Start-Hidden -FilePath "python" -Arguments '-c "import time; time.sleep(6)"' -PassThru
        $t0 = Get-Date
        Invoke-TriageWaitLoop -Proc $child -Spawns $spawns -off $false `
            -Spawner $timedStub -Beat $noop -Maintain $noopM -Sleeper { Start-Sleep -Seconds 1 }
        $blockSec = ((Get-Date) - $t0).TotalSeconds
        # Every spawn recorded above happened inside the while(-not HasExited) body, so the only
        # thing left to prove is that the body ran repeatedly across a genuinely long block.
        $during = @($reactTimes | Where-Object { $_ -lt $child.ExitTime }).Count
        $realOk = $child.HasExited -and ($blockSec -ge 5) -and ($during -ge 2) -and ($spawned.Count -ge 8)
        $detail = "blockSec=$([math]::Round($blockSec,1)) reactDuring=$during totalSpawns=$($spawned.Count) exited=$($child.HasExited)"
    } catch { $detail = "threw: $_" }
    Assert-Case 'react-fires-during-real-block' $realOk $detail

    # --- THE BOUND. A child that NEVER exits: the 10th-July wedge, in miniature. -------------------
    # The mocked Clock advances 60s per read, so the 1200s bound is reached in ~20 synthetic beats
    # without a single real second passing. THE FUSE IS MANDATORY: if the bound ever regresses,
    # HasExited would stay $false forever and this exam would HANG rather than go red- the fuse
    # flips HasExited past $script:twFuse beats, and a blown fuse force-fails the case.
    $script:twFuse  = 400
    $script:twWedge = 0
    $script:twKills = 0
    $script:twClock = [datetime]::Parse('2026-07-10T00:00:00')
    $wedgeProc = [pscustomobject]@{ Id = -1 }
    $wedgeProc | Add-Member -MemberType ScriptProperty -Name HasExited -Value { $script:twWedge -ge $script:twFuse }
    $tick      = { $script:twClock = $script:twClock.AddSeconds(60); $script:twClock }
    $wBeat     = { $script:twWedge++ }
    $countKill = { param($p) $script:twKills++ }
    $throwKill = { param($p) throw 'no such process' }
    # A path that cannot exist: the wedge cases must not read (or depend on) the live stamp.
    $noHb = Join-Path ([IO.Path]::GetTempPath()) 'bxwatch-absent-stamp-do-not-create.txt'
    if (Test-Path $noHb) { Remove-Item $noHb -Force -ErrorAction SilentlyContinue }

    $script:SelfTestLog.Clear()
    $wedgeReturned = $false
    try {
        Invoke-TriageWaitLoop -Proc $wedgeProc -Spawns $spawns -off $false `
            -Spawner $stub -Beat $wBeat -Maintain $noopM -Sleeper $noop `
            -MaxWaitSec 1200 -Clock $tick -Killer $countKill -HeartbeatPath $noHb
        $wedgeReturned = $true
    } catch { }
    $fuseBlown   = $script:twWedge -ge $script:twFuse
    $wedgeLogged = @($script:SelfTestLog | Where-Object { $_ -match 'triage child wedged' }).Count -eq 1
    Assert-Case 'wedge-kill-breaks-loop' ($wedgeReturned -and (-not $fuseBlown) -and $wedgeLogged) `
        "returned=$wedgeReturned beats=$($script:twWedge) fuse=$($script:twFuse) logged=$wedgeLogged"

    # --- exactly once, not at-least-once: a re-entered kill would Stop-Process a recycled PID -----
    $script:twWedge = 0; $script:twKills = 0
    $script:twClock = [datetime]::Parse('2026-07-10T00:00:00')
    try {
        Invoke-TriageWaitLoop -Proc $wedgeProc -Spawns $spawns -off $false `
            -Spawner $stub -Beat $wBeat -Maintain $noopM -Sleeper $noop `
            -MaxWaitSec 1200 -Clock $tick -Killer $countKill -HeartbeatPath $noHb
    } catch { }
    Assert-Case 'wedge-killer-called-once' (($script:twKills -eq 1) -and ($script:twWedge -lt $script:twFuse)) `
        "kills=$($script:twKills) beats=$($script:twWedge)"

    # --- a Killer that throws still breaks: the wedge must not outlive its own failed kill --------
    $script:twWedge = 0; $script:twKills = 0
    $script:twClock = [datetime]::Parse('2026-07-10T00:00:00')
    $script:SelfTestLog.Clear()
    $throwReturned = $false
    try {
        Invoke-TriageWaitLoop -Proc $wedgeProc -Spawns $spawns -off $false `
            -Spawner $stub -Beat $wBeat -Maintain $noopM -Sleeper $noop `
            -MaxWaitSec 1200 -Clock $tick -Killer $throwKill -HeartbeatPath $noHb
        $throwReturned = $true
    } catch { }
    $failLogged = @($script:SelfTestLog | Where-Object { $_ -match 'failed to kill wedged triage' }).Count -eq 1
    Assert-Case 'killer-throws-still-breaks' ($throwReturned -and $failLogged -and ($script:twWedge -lt $script:twFuse)) `
        "returned=$throwReturned logged=$failLogged beats=$($script:twWedge)"

    # --- MaxWaitSec 0 still means unbounded: a healthy long triage is never killed ----------------
    $script:twZero = 0; $script:twKills = 0
    $script:twClock = [datetime]::Parse('2026-07-10T00:00:00')
    $exitProc = [pscustomobject]@{ Id = -2 }
    $exitProc | Add-Member -MemberType ScriptProperty -Name HasExited -Value { $script:twZero -ge 3 }
    Invoke-TriageWaitLoop -Proc $exitProc -Spawns $spawns -off $false `
        -Spawner $stub -Beat { $script:twZero++ } -Maintain $noopM -Sleeper $noop `
        -MaxWaitSec 0 -Clock $tick -Killer $countKill -HeartbeatPath $noHb
    Assert-Case 'maxwait-zero-never-kills' (($script:twKills -eq 0) -and ($script:twZero -eq 3)) `
        "kills=$($script:twKills) beats=$($script:twZero)"

    # --- the heartbeat reader. A TEMP stamp, never the live .baxter_heartbeat.txt -----------------
    $hbDir = Join-Path ([IO.Path]::GetTempPath()) ("bxstamp-" + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $hbDir -Force | Out-Null
    try {
        $hbFile     = Join-Path $hbDir 'stamp.txt'
        $fixedNow   = [datetime]::Parse('2026-07-10T12:00:00')
        $fixedClock = { [datetime]::Parse('2026-07-10T12:00:00') }
        $script:twAlerts = 0
        $alerter = { param($m) $script:twAlerts++ }
        $hbProc = [pscustomobject]@{ Id = -3 }
        $hbProc | Add-Member -MemberType ScriptProperty -Name HasExited -Value { $script:twZero -ge 5 }

        # 3h stale, 5 beats: the alarm fires EXACTLY once, latched, not once per beat.
        [IO.File]::WriteAllText($hbFile, $fixedNow.AddHours(-3).ToString('o'), $enc)
        $script:twZero = 0; $script:twAlerts = 0
        Invoke-TriageWaitLoop -Proc $hbProc -Spawns $spawns -off $false `
            -Spawner $stub -Beat { $script:twZero++ } -Maintain $noopM -Sleeper $noop `
            -MaxWaitSec 999999 -Clock $fixedClock -Killer $countKill -HeartbeatPath $hbFile -Alerter $alerter
        Assert-Case 'heartbeat-stale-alerts-once' (($script:twAlerts -eq 1) -and ($script:twZero -eq 5)) `
            "alerts=$($script:twAlerts) beats=$($script:twZero)"

        # A cycle that completed this instant: silence.
        [IO.File]::WriteAllText($hbFile, $fixedNow.ToString('o'), $enc)
        $script:twZero = 0; $script:twAlerts = 0
        Invoke-TriageWaitLoop -Proc $hbProc -Spawns $spawns -off $false `
            -Spawner $stub -Beat { $script:twZero++ } -Maintain $noopM -Sleeper $noop `
            -MaxWaitSec 999999 -Clock $fixedClock -Killer $countKill -HeartbeatPath $hbFile -Alerter $alerter
        Assert-Case 'heartbeat-fresh-no-alert' (($script:twAlerts -eq 0) -and ($script:twZero -eq 5)) `
            "alerts=$($script:twAlerts) beats=$($script:twZero)"

        # A half-written or corrupt stamp: unparseable is unknown, not stale. No alert, no throw.
        # This is what makes the try/catch load-bearing rather than redundant with a Test-Path.
        [IO.File]::WriteAllText($hbFile, 'not a timestamp', $enc)
        $script:twZero = 0; $script:twAlerts = 0
        $badOk = $false
        try {
            Invoke-TriageWaitLoop -Proc $hbProc -Spawns $spawns -off $false `
                -Spawner $stub -Beat { $script:twZero++ } -Maintain $noopM -Sleeper $noop `
                -MaxWaitSec 999999 -Clock $fixedClock -Killer $countKill -HeartbeatPath $hbFile -Alerter $alerter
            $badOk = $true
        } catch { }
        Assert-Case 'heartbeat-unparseable-no-alert' ($badOk -and ($script:twAlerts -eq 0) -and ($script:twZero -eq 5)) `
            "returned=$badOk alerts=$($script:twAlerts) beats=$($script:twZero)"

        # No stamp at all (a fresh vault): unknown is not stale. No alert, no throw.
        $script:twZero = 0; $script:twAlerts = 0
        $missingOk = $false
        try {
            Invoke-TriageWaitLoop -Proc $hbProc -Spawns $spawns -off $false `
                -Spawner $stub -Beat { $script:twZero++ } -Maintain $noopM -Sleeper $noop `
                -MaxWaitSec 999999 -Clock $fixedClock -Killer $countKill -HeartbeatPath $noHb -Alerter $alerter
            $missingOk = $true
        } catch { }
        Assert-Case 'heartbeat-missing-no-throw' ($missingOk -and ($script:twAlerts -eq 0) -and ($script:twZero -eq 5)) `
            "returned=$missingOk alerts=$($script:twAlerts) beats=$($script:twZero)"

        # UNKNOWN IS NEVER STALE, at ANY threshold. PowerShell 5.1 measured, 10th July:
        # `$null -ge 0` is $false but `$null -ge -1` is $true- $null does not coerce to a plain 0.
        # So a threshold below zero makes an ABSENT stamp compare as stale and alarms a vault that
        # has simply never run a cycle. `$null -ne $age` is the only thing in front of that, and it
        # is unfalsifiable at any non-negative threshold: this case must use -1 or it tests nothing.
        $script:twZero = 0; $script:twAlerts = 0
        Invoke-TriageWaitLoop -Proc $hbProc -Spawns $spawns -off $false `
            -Spawner $stub -Beat { $script:twZero++ } -Maintain $noopM -Sleeper $noop `
            -MaxWaitSec 999999 -Clock $fixedClock -Killer $countKill -HeartbeatPath $noHb `
            -StaleAlertSec (-1) -Alerter $alerter
        Assert-Case 'heartbeat-unknown-never-stale' (($script:twAlerts -eq 0) -and ($script:twZero -eq 5)) `
            "alerts=$($script:twAlerts) beats=$($script:twZero)"
        # THE PRODUCTION SHAPE. Line 1641 calls this loop with NO -HeartbeatPath, -StaleAlertSec or
        # -Alerter, so all three defaults are load-bearing and every case above bypasses them by
        # passing the path explicitly. Point $script:triageBeat at a stale scratch stamp, omit the
        # argument, and prove the loop reads the vault stamp on its own. Without this, the reader
        # could exist only under test while production went on reading nothing- which is the bug.
        $savedBeat = $script:triageBeat
        try {
            [IO.File]::WriteAllText($hbFile, $fixedNow.AddHours(-3).ToString('o'), $enc)
            $script:triageBeat = $hbFile
            $script:twZero = 0; $script:twAlerts = 0
            Invoke-TriageWaitLoop -Proc $hbProc -Spawns $spawns -off $false `
                -Spawner $stub -Beat { $script:twZero++ } -Maintain $noopM -Sleeper $noop `
                -MaxWaitSec 999999 -Clock $fixedClock -Killer $countKill -Alerter $alerter
            Assert-Case 'heartbeat-default-binds' (($script:twAlerts -eq 1) -and ($script:twZero -eq 5)) `
                "alerts=$($script:twAlerts) beats=$($script:twZero)"
        } finally { $script:triageBeat = $savedBeat }
    } finally {
        # Never trust the code under test to name the path we delete: assert it is our scratch dir.
        if ($hbDir -like (Join-Path ([IO.Path]::GetTempPath()) 'bxstamp-*')) {
            Remove-Item $hbDir -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    # --- the WIRING check: the reader must exist in production, not only under test ---------------
    $beatPathOk = $script:triageBeat -and $script:triageBeat.EndsWith('.baxter_heartbeat.txt') `
                  -and (Test-Path (Split-Path $script:triageBeat -Parent))
    Assert-Case 'heartbeat-path-defaults-to-vault' ([bool]$beatPathOk) "path=[$($script:triageBeat)]"

    # --- regression guard: the slice-loop comment must not claim the fast lane fires during triage -
    # The needle is ASSEMBLED, never written whole: a literal copy of the false claim would sit in
    # this very file and match itself, failing the case forever (it did, first run).
    $src    = Get-Content $PSCommandPath -Raw
    $needle = 'answered in ~15-60s even while a long ' + 'triage run holds the main lock'
    $liesOk = (-not $src.Contains($needle)) -and ($src -match 'This loop is NOT reached while triage runs')
    Assert-Case 'slice-comment-not-self-refuting' $liesOk "old claim present=$($src.Contains($needle))"

    $failures = $script:selfTestFails
    if ($script:RealOutwardCalls -gt 0) {
        Say-Test "TRIAGEWAIT GUARD BREACH: $($script:RealOutwardCalls) real outward call(s) reached a default scriptblock"
        $failures++
    }
    if ($failures -eq 0) { Say-Test "TRIAGEWAIT RESULT PASS"; return 0 }
    Say-Test "TRIAGEWAIT RESULT FAIL ($failures failing case(s))"
    return 1
}

if ($TriageWaitSelfTest) { exit (Invoke-TriageWaitSelfTest) }

# SELF-RESTART ON SCRIPT UPDATE (6 Jul - the deeper duplicate root cause). A long-lived
# PowerShell loop loads its script into memory once; editing this .ps1 never hot-reloads
# the running instance. The 6-Jul Assert-Singletons alarm landed on disk but the watcher
# running since the night before never executed it, so the duplicate slid undetected. Stamp
# our mtime at launch; if the file gets newer, relaunch ourselves (matching the guardian's
# launch form) and exit so on-disk fixes take hold within one beat, not at next login.
$scriptPath  = $PSCommandPath
try { $scriptMtime = (Get-Item $scriptPath).LastWriteTimeUtc } catch { $scriptMtime = (Get-Date).ToUniversalTime() }

try {
    while ($true) {
        Write-Beat "alive"

        try {
            if ((Get-Item $scriptPath).LastWriteTimeUtc -gt $scriptMtime) {
                Write-WatchLog "watcher script updated on disk - self-restarting to load new code"
                Write-Beat "stopped"   # let the replacement's single-instance guard pass
                Start-Hidden -FilePath "powershell.exe" -Arguments "-WindowStyle Hidden -ExecutionPolicy Bypass -NoProfile -File `"$scriptPath`""
                return
            }
        } catch {}

        Invoke-UsageEnforce
        Assert-Singletons   # once per ~60s outer beat: catch any duplicate singleton within a minute
        Assert-WatchdogLoop # the resurrectable singleton: revive the CoC watchdog loop if it died

        $gaming = Test-FullscreenGame
        if ($gaming -ne $wasGaming) {
            Write-WatchLog $(if ($gaming) { "fullscreen app detected - standing aside" } else { "fullscreen app gone - resuming" })
            $wasGaming = $gaming
        }
        # Grandmaster OFF: skip all heavy spawns (triage/voice/WhatsApp) to free the PC;
        # the fast-lane spawn below stays live regardless so /on always lands.
        $off = Test-Path $offFlag

        # Bounce any resident child whose source is newer than the process running it, so an
        # on-disk fix loads within one beat instead of at next login. Runs before the
        # supervisor blocks: a child killed here is relaunched here, not left down.
        if (-not $gaming) { Invoke-ChildReload $off }

        if (-not $gaming -and -not $off) {
            # keep the WhatsApp bridge alive (read-only linked device). It writes its
            # own heartbeat every 60s and self-guards against duplicates. When it
            # exits with a needs-relink flag we DON'T relaunch - the owner must scan a QR.
            # only keep it alive once LINKED (auth exists) or a link attempt is armed
            # (link_now.txt) - otherwise an unpaired bridge would churn QRs all night.
            $waArmed = (Test-Path (Join-Path $waDir "auth\creds.json")) -or (Test-Path (Join-Path $waDir "link_now.txt"))
            if ($waArmed -and (Test-Path (Join-Path $waDir "bridge.mjs")) -and -not (Test-Path $waRelink)) {
                $waAlive = $false
                if (Test-Path $waBeat) {
                    try {
                        $ws = [datetime]::Parse(((Get-Content $waBeat -Raw) -split "`t")[0])
                        if (((Get-Date) - $ws).TotalMinutes -lt 3) { $waAlive = $true }
                    } catch {}
                }
                if (-not $waAlive) {
                    try {
                        Start-Hidden -FilePath "node" -Arguments "bridge.mjs" -WorkingDirectory $waDir
                        Write-WatchLog "whatsapp bridge (re)launched"
                    } catch { Write-WatchLog "whatsapp bridge launch failed: $_" }
                }
            }
            # clear a stale lock left behind by a crashed/killed triage (>10 min old)
            if (Test-Path $lock) {
                try {
                    $age = (Get-Date) - (Get-Item $lock).LastWriteTime
                    if ($age.TotalMinutes -gt 10) { Remove-Item $lock -Force -ErrorAction SilentlyContinue; Write-WatchLog "cleared stale lock (age $([int]$age.TotalMinutes)m)" }
                } catch {}
            }
            # Run triage as a CHILD process and keep beating while it works -- a
            # claude triage can take minutes, and a frozen beat would make the
            # guardian wrongly relaunch a duplicate. Beat every few seconds instead.
            try {
                $proc = Start-Hidden -FilePath "python" -Arguments "`"$py`"" -PassThru
                # BelowNormal so a triage/claude run can never compete with a game for CPU.
                # (Windows: children of a BelowNormal parent inherit it, so claude gets it too.)
                try { $proc.PriorityClass = "BelowNormal" } catch {}
                # Beat, reload stale children, and fire the four one-shots WHILE triage runs. The
                # slice loop below is unreachable until the child exits, so this loop is the only
                # thing standing between a minutes-long triage cycle and a starved reaction
                # watcher / /usage poller / fast lane / reminder poll. See Invoke-TriageWaitLoop.
                Invoke-TriageWaitLoop -Proc $proc -off $off `
                    -Spawns (Get-TriageWaitSpawns -Reminders $rem -React $react -UsageCmd $ucmd -Fast $fast)
                if ($proc.ExitCode -ne 0) { Write-WatchLog "triage exited non-zero ($($proc.ExitCode))" }
            } catch {
                Write-WatchLog "triage crashed: $_"
                Write-Host "triage error: $_" -ForegroundColor Red
            }
            # Perma voice-capture ingest: transcribe any phone-synced audio chunks
            # (local CPU, self-locking) and coalesce finished talking-sessions into
            # triage. Cheap when the inbox is empty; only flushes on a session gap so
            # it never wakes a worker per chunk. Once per beat is plenty for 5-min chunks.
            $voice = "C:\Users\you\Documents\Python Scripts\utils\baxter_voice.py"
            try { Start-Hidden -FilePath "python" -Arguments "`"$voice`"" } catch {}
        }
        # Baxter SLASH + REAL-TIME LISTENER (GUARDED, persistent- 7 Jul). One gateway process
        # (Python 3.12, discord.py) that serves the native slash commands AND the event-driven
        # on_message listener that gives an instant reply in every channel (no polling). Kept
        # alive OUTSIDE the $off gate (it's the command surface- /on must always land) but
        # paused during fullscreen gaming to protect frames. Single-instance: only (re)launch
        # when no python.exe is already running baxter_slash.py (match by NAME + cmdline per the
        # census memory- never a bare cmdline substring that would count this very shell).
        if (-not $gaming) {
            try {
                $slashRunning = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
                    $_.Name -eq 'python.exe' -and $_.CommandLine -match 'baxter_slash\.py' })
                if ($slashRunning.Count -eq 0) {
                    try {
                        Start-Hidden -FilePath $py312 -Arguments "`"$slashBot`"" -WorkingDirectory $utilsDir
                        Write-WatchLog "baxter_slash (slash + real-time listener) launched"
                    } catch { Write-WatchLog "baxter_slash launch failed: $_" }
                }
            } catch {}
            # CODEX + JEM ARE NOT LAUNCHED HERE ANY MORE (9th July, the owner's decouple order).
            # This block used to start Codex, which made Codex a supervised child of THIS
            # loop- and this loop blocks for minutes on the triage child below, and dies
            # when the watcher dies. That is precisely why Codex went down with Baxter while
            # Jem (launched by nothing) lived on. Both bots are now tended by
            # baxter_coop_guardian.py, a scheduled task with no relationship to Baxter.
            # Do NOT re-add a launch here: the coupling IS the bug.
        }
        # sleep the interval in short slices, beating as we go (stay fresh while idle too).
        # Each slice fires the same four one-shots the triage-wait loop does:
        #   $fast  - the FAST LANE (baxter_fast.py, detached, self-locking). A BACKSTOP, not the
        #            primary responder: the owner's replies come from the resident listener
        #            (baxter_slash.on_message), an event-driven gateway decoupled from this loop.
        #            The fast lane only bites when that listener is dead, mid-bounce or paused.
        #   $ucmd  - /usage's OWN orthogonal poller (6th-July v2): self-locking, reads the baked
        #            .baxter_usage_live.json + native-replies in ~1s. Decoupled from the fast
        #            lane's 240s FLOCK, so /usage never waits on a slow claude reply.
        #   $react - reaction-lifecycle self-audit (8th July), a self-throttling one-shot so a
        #            dropped 👀/⚙️/✅ self-heals. Verify-before-repair: only ticks a message with
        #            a real delivered reply.
        #   $rem   - the ad-hoc reminder poller.
        # This loop is NOT reached while triage runs- the wait loop above blocks on the child for
        # the whole cycle. That is why all four are fired there too, and why they are declared at
        # the top of the file rather than here. (Until 9th July the comment on this block claimed
        # the fast lane still fired while a triage run held the main lock. It could not, and
        # -TriageWaitSelfTest now guards against that claim returning.)
        $slept = 0
        while ($slept -lt $IntervalSec) {
            try { Start-Hidden -FilePath "python" -Arguments "`"$rem`" --fire" } catch {}
            if (-not $gaming) { try { Start-Hidden -FilePath "python" -Arguments "`"$fast`"" } catch {} }
            if (-not $gaming) { try { Start-Hidden -FilePath "python" -Arguments "`"$ucmd`"" } catch {} }
            if (-not $gaming) { try { Start-Hidden -FilePath "python" -Arguments "`"$react`"" } catch {} }
            if (-not $gaming) { Invoke-ChildReloadThrottled $off }
            Start-Sleep -Seconds ([Math]::Min(15, $IntervalSec - $slept)); $slept += 15; Write-Beat "alive"
            Invoke-UsageEnforce   # kill+flag check every 15s slice while idle- not just once per interval
        }
    }
}
finally {
    Write-Beat "stopped"
    Write-WatchLog "watcher stopped"
}
