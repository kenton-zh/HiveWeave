' silent-run.vbs — launch a command with NO visible window.
'
' Usage: wscript.exe //nologo silent-run.vbs <arg1> <arg2> ...
'
' Concatenates all arguments with single spaces and runs the result
' with WindowStyle=0 (hidden). Multi-arg form avoids the nested-quote
' pain of passing a single quoted command line through BAT -> wscript.
'
' bWaitOnReturn = False: caller does not block. uvicorn / vite are
' long-running daemons; the hidden cmd window stays open as long as
' the process lives, and detaches from the calling console (closing
' the start-all.bat window does NOT kill the service — use stop.bat).

Dim shell
Set shell = CreateObject("WScript.Shell")

If WScript.Arguments.Count < 1 Then
    WScript.Echo "silent-run.vbs: missing command argument"
    WScript.Quit 1
End If

Dim cmd
cmd = ""
For i = 0 To WScript.Arguments.Count - 1
    If i > 0 Then cmd = cmd & " "
    cmd = cmd & WScript.Arguments(i)
Next

shell.Run cmd, 0, False
