' Launch a program with NO visible window. Needed because uv's venv pythonw.exe
' is a trampoline that re-launches the console python.exe (which flashes/holds a
' terminal). WScript.Shell.Run with windowstyle 0 hides it reliably; the True
' waits so a scheduled task tracks the child's lifetime (restart-on-failure).
' Usage: wscript //nologo run-hidden.vbs "<exe>" "<arg1>" ["<arg2>" ...]
Set sh = CreateObject("WScript.Shell")
q = Chr(34)
cmd = q & WScript.Arguments(0) & q
For i = 1 To WScript.Arguments.Count - 1
  cmd = cmd & " " & q & WScript.Arguments(i) & q
Next
sh.Run cmd, 0, True
