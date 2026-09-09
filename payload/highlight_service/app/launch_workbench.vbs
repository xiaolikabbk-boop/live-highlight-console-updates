Option Explicit

Dim shell, fso, scriptDir, serviceDir, rootDir, mode, command, exitCode
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
serviceDir = fso.GetParentFolderName(scriptDir)
rootDir = fso.GetParentFolderName(serviceDir)
mode = "start"
If WScript.Arguments.Count > 0 Then mode = LCase(WScript.Arguments(0))

If mode = "wait" Then
    WaitForWorkbench shell
    WScript.Quit 0
End If
If mode = "selftest" Then WScript.Quit 0

command = "powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File " & Quote(rootDir & "\start_console.ps1")
If mode = "background" Then command = command & " -NoDesktopWindow"

' WScript owns the child process, so Windows Terminal cannot turn it into a
' visible tab even when it is configured as the machine's default terminal.
exitCode = shell.Run(command, 0, False)
If exitCode <> 0 Then
    shell.Popup "Live Highlight Workbench could not start. Please reinstall the latest update.", 0, "Live Highlight Workbench", 16
End If

Sub WaitForWorkbench(appShell)
    Dim attempt, request, edgePath, address
    address = "http://127.0.0.1:8876/"
    For attempt = 1 To 180
        On Error Resume Next
        Set request = CreateObject("WinHttp.WinHttpRequest.5.1")
        request.SetTimeouts 500, 500, 500, 1000
        request.Open "GET", address, False
        request.Send
        If Err.Number = 0 And request.Status >= 200 And request.Status < 500 Then
            On Error GoTo 0
            edgePath = FindEdge(appShell)
            If edgePath <> "" Then
                appShell.Run Quote(edgePath) & " --app=" & address & " --start-maximized --no-first-run", 1, False
            Else
                appShell.Run address, 1, False
            End If
            Exit Sub
        End If
        Err.Clear
        On Error GoTo 0
        WScript.Sleep 1000
    Next

    appShell.Popup "Startup timed out. Please send startup-error.log from the program folder for diagnosis.", 0, "Live Highlight Workbench", 48
End Sub

Function FindEdge(appShell)
    Dim candidates, item
    candidates = Array( _
        appShell.ExpandEnvironmentStrings("%ProgramFiles(x86)%") & "\Microsoft\Edge\Application\msedge.exe", _
        appShell.ExpandEnvironmentStrings("%ProgramFiles%") & "\Microsoft\Edge\Application\msedge.exe", _
        appShell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Microsoft\Edge\Application\msedge.exe" _
    )
    FindEdge = ""
    For Each item In candidates
        If fso.FileExists(item) Then
            FindEdge = item
            Exit Function
        End If
    Next
End Function

Function Quote(value)
    Quote = Chr(34) & value & Chr(34)
End Function
