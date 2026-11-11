# Running trove on a schedule

`trove sync` is meant to run unattended: it skips what's already there, stops
cleanly when a site's quota runs out, and only one pass runs at a time. A pass that
starts while another is still going exits at once with code 4, so a schedule can be
frequent without passes piling up.

## What to schedule

```bash
trove sync --recent-first
```

- `--recent-first` syncs the sites whose content changed most recently first, so a
  pass that stops early (a quota, a long download) still covers the active ones.
  `trove sites --ages` shows the same order, with how long each has been quiet.
- How often: daily suits most lists. MEGA's anonymous transfer quota refills over
  roughly six hours, so with a large MEGA backlog, a pass every six hours drains it
  steadily.
- Add `--delete-orphans` only once you trust the results; until then, read the
  orphans in the log. Its refusals still apply when unattended.
- ufile downloads need a visible browser and sometimes a solved reCAPTCHA, so leave
  ufile sites disabled for scheduled runs and sync them by hand
  (`trove sync "Site name"`).

Exit codes, for whatever watches the job:

| Code | Meaning |
|---|---|
| 0 | Everything succeeded |
| 1 | Something failed (a dead link, a file error); the rest still ran |
| 2 | Usage or config problem |
| 3 | The run stopped early (quota, outage, refused login); run again later |
| 4 | Another `trove sync` was already running; this one did nothing |

A dead link fails every run until you deal with it, so code 1 is normal for a list
that has one. `trove sites --gone` lists them; set `enabled: false` to retire one.

Credentials: an SFTP password stored with `trove set-password` lives in the OS
keyring of the user who stored it, so the job must run as that user.

## Windows: Task Scheduler

A wrapper that keeps a log per run and the last 30 of them, saved for example as
`C:\Tools\trove\run-trove.ps1`:

```powershell
$ErrorActionPreference = "Continue"
$logDir = Join-Path $env:LOCALAPPDATA "trove-downloader\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir ("sync_{0}.log" -f (Get-Date -Format "yyyy-MM-dd_HHmmss"))

& trove sync --recent-first @args *>&1 | Tee-Object -FilePath $log
$code = $LASTEXITCODE

Get-ChildItem $logDir -Filter "sync_*.log" | Sort-Object LastWriteTime -Descending |
    Select-Object -Skip 30 | Remove-Item -Force -ErrorAction SilentlyContinue
exit $code
```

If `trove` isn't on the task's PATH (a virtual environment, say), call it by its full
path, such as `C:\path\to\venv\Scripts\trove.exe`.

Register it to run daily at 03:00, as you, from an elevated or normal PowerShell:

```powershell
$action  = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "C:\Tools\trove\run-trove.ps1"'
$trigger = New-ScheduledTaskTrigger -Daily -At 3am
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 12)
Register-ScheduledTask -TaskName "trove sync" -Action $action -Trigger $trigger -Settings $settings
```

For every six hours instead, add a repetition to the trigger:

```powershell
$trigger.Repetition = (New-ScheduledTaskTrigger -Once -At 3am `
    -RepetitionInterval (New-TimeSpan -Hours 6)).Repetition
Set-ScheduledTask -TaskName "trove sync" -Trigger $trigger
```

Notes:

- Registered this way, the task runs only while you're logged on (locking the screen
  is fine), which keeps the keyring and mapped drives available. To run while logged
  off, choose "Run whether user is logged on or not" in Task Scheduler and store the
  password; network drives then need their UNC path or an `sftp://` destination.
- `-MultipleInstances IgnoreNew` stops Task Scheduler starting a second copy; the
  run lock covers anything else, such as a manual run during a scheduled one.
- `Start-ScheduledTask "trove sync"` runs it now; `Get-ScheduledTaskInfo "trove sync"`
  shows the last result, which is the exit code.

## Linux: cron or a systemd timer

cron, daily at 03:00 (`crontab -e`):

```cron
0 3 * * * $HOME/.local/bin/trove sync --recent-first >> $HOME/.local/state/trove-sync.log 2>&1
```

Or a systemd user timer, which keeps logs in the journal. `~/.config/systemd/user/trove-sync.service`:

```ini
[Unit]
Description=trove sync

[Service]
Type=oneshot
ExecStart=%h/.local/bin/trove sync --recent-first
# Codes 1 and 4 aren't failures of the job itself.
SuccessExitStatus=1 4
```

`~/.config/systemd/user/trove-sync.timer`:

```ini
[Unit]
Description=Run trove sync daily

[Timer]
OnCalendar=*-*-* 03:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

Enable it with `systemctl --user enable --now trove-sync.timer`, and run
`loginctl enable-linger $USER` so it runs while you're logged out. Logs:
`journalctl --user -u trove-sync`. A keyring isn't always available to a timer or
cron job on a server; use the config's `password_file`, an SSH key, or
`$TROVE_SFTP_PASSWORD` there.

## macOS: launchd

`~/Library/LaunchAgents/local.trove.sync.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>local.trove.sync</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/YOU/.local/bin/trove</string>
    <string>sync</string>
    <string>--recent-first</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>3</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardOutPath</key><string>/Users/YOU/Library/Logs/trove-sync.log</string>
  <key>StandardErrorPath</key><string>/Users/YOU/Library/Logs/trove-sync.log</string>
</dict>
</plist>
```

Load it with `launchctl load ~/Library/LaunchAgents/local.trove.sync.plist`. A
LaunchAgent runs while you're logged in and can use the login keychain; a missed time
(the Mac was asleep) runs at wake.
