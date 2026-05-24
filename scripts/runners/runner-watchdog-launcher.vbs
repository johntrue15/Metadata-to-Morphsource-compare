' MorphoClaw runner-watchdog launcher.
'
' Wraps runner-watchdog.ps1 so the Task Scheduler action can spawn it
' WITHOUT briefly showing a cmd / PowerShell console window. Calling
' powershell.exe directly with -WindowStyle Hidden still flashes the
' console for ~100 ms on Interactive logon sessions; WScript.Shell.Run
' with intWindowStyle=0 hides it for real.
'
' Used by install-watchdog.ps1.

Option Explicit

Dim shell, fso, scriptDir, psScript, cmd
Set shell = CreateObject("WScript.Shell")
Set fso   = CreateObject("Scripting.FileSystemObject")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
psScript  = scriptDir & "\runner-watchdog.ps1"

If Not fso.FileExists(psScript) Then
    ' Best-effort log to %LOCALAPPDATA%\MorphoClaw\runner-watchdog.log
    ' so an operator can tell the launcher couldn't find its payload.
    Dim logDir, logPath, logFile
    logDir = shell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\MorphoClaw"
    If Not fso.FolderExists(logDir) Then fso.CreateFolder(logDir)
    logPath = logDir & "\runner-watchdog.log"
    Set logFile = fso.OpenTextFile(logPath, 8, True)
    logFile.WriteLine Now & " [ERROR] launcher could not find " & psScript
    logFile.Close
    WScript.Quit 2
End If

cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File """ & psScript & """"

' intWindowStyle = 0 (SW_HIDE), bWaitOnReturn = False
' WScript.Shell.Run returns the process spawn result; we don't wait.
shell.Run cmd, 0, False
