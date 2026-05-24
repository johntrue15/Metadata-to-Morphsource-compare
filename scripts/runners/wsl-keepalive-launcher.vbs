' MorphoClaw WSL keepalive launcher.
'
' Holds a persistent WSL session open inside the target distro so that
' WSL2 does NOT shut the distro down between watchdog ticks. WSL2 idles
' the user-space distro whenever no interactive user session has run
' a command for `vmIdleTimeout` ms (default 60s). On WSL 2.7.3.0 the
' actions-runner systemd service alone does NOT count as a user session
' for that calculation, so the distro shuts down every minute or two
' even though Runner.Listener is running. Each shutdown forces the runner
' to re-authenticate with GitHub when the next watchdog tick wakes the
' distro again, which makes the runner look intermittently "offline" to
' GitHub's API.
'
' By spawning `wsl.exe -d <distro> -- bash -c "exec sleep infinity"` and
' detaching, we keep at least one user-space process alive in the distro,
' which prevents the idle shutdown.
'
' This script is invoked by a Scheduled Task at a 1-minute cadence (plus
' at logon). It is idempotent: if a keepalive wsl.exe is already running,
' it exits without spawning another.
'
' Why VBScript? Same reason as runner-watchdog-launcher.vbs: WScript.Shell.Run
' with intWindowStyle=0 creates no console window at all, whereas
' powershell.exe -WindowStyle Hidden still flashes briefly under Task
' Scheduler LogonType=Interactive.
'
' Args (optional, one positional):
'   distro name (default: Ubuntu-24.04)

Option Explicit

Const SW_HIDE = 0

' --- args ---------------------------------------------------------------
Dim shell, fso, args, distro
Set shell = CreateObject("WScript.Shell")
Set fso   = CreateObject("Scripting.FileSystemObject")
Set args  = WScript.Arguments

If args.Count >= 1 Then
    distro = args(0)
Else
    distro = "Ubuntu-24.04"
End If

' --- log helper ----------------------------------------------------------
Dim logDir, logPath
logDir  = shell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\MorphoClaw"
logPath = logDir & "\wsl-keepalive.log"
If Not fso.FolderExists(logDir) Then fso.CreateFolder logDir

' Rotate the log if it grows beyond ~256 KB so we never balloon.
If fso.FileExists(logPath) Then
    Dim f
    Set f = fso.GetFile(logPath)
    If f.Size > 262144 Then fso.DeleteFile logPath, True
End If

Sub LogLine(level, message)
    Dim ts, line, fh
    ts   = Year(Now) & "-" & Right("0" & Month(Now), 2) & "-" & Right("0" & Day(Now), 2) _
         & "T" & Right("0" & Hour(Now), 2) & ":" & Right("0" & Minute(Now), 2) _
         & ":" & Right("0" & Second(Now), 2)
    line = ts & " [" & level & "] " & message
    On Error Resume Next
    Set fh = fso.OpenTextFile(logPath, 8, True)
    fh.WriteLine line
    fh.Close
    On Error Goto 0
End Sub

' --- deduplication --------------------------------------------------------
' Look for an existing wsl.exe process whose commandline contains our
' sentinel substring. WMI's Win32_Process is the only way to read the
' commandline of another process from VBScript without admin rights.
Dim sentinel
sentinel = "exec sleep infinity"

Dim wmi, procs, p, alreadyRunning
alreadyRunning = False

On Error Resume Next
Set wmi = GetObject("winmgmts:\\.\root\cimv2")
If Err.Number <> 0 Then
    LogLine "ERROR", "could not bind to WMI; spawning blindly (" & Err.Description & ")"
    Err.Clear
Else
    Set procs = wmi.ExecQuery("SELECT CommandLine FROM Win32_Process WHERE Name='wsl.exe'")
    For Each p In procs
        If Not IsNull(p.CommandLine) Then
            If InStr(LCase(p.CommandLine), LCase(sentinel)) > 0 Then
                alreadyRunning = True
                Exit For
            End If
        End If
    Next
End If
On Error Goto 0

If alreadyRunning Then
    ' Existing keepalive is fine; quiet exit. No log line on the happy path
    ' so we don't spam.
    WScript.Quit 0
End If

' --- spawn ---------------------------------------------------------------
Dim cmd
cmd = "wsl.exe -d " & distro & " -- bash -c """ & sentinel & """"

LogLine "INFO", "spawning keepalive: " & cmd

' Detached + hidden. We do NOT wait — WScript exits immediately while the
' spawned wsl.exe + sleep continue.
shell.Run cmd, SW_HIDE, False

WScript.Quit 0
