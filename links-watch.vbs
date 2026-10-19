' Links → ideas watcher: runs links.py hidden (checks the HA list and Beeper chats every "interval" seconds).
' python.exe with a hidden window (pythonw has no stdout, which ideas.py reconfigures). Put a copy (or a shortcut) in the Startup folder to start it at logon.
' If links.py ever exits it is started again 10 s later; its error output goes to watcher-errors.log.
Set sh = CreateObject("WScript.Shell")
Set fs = CreateObject("Scripting.FileSystemObject")
here = fs.GetParentFolderName(WScript.ScriptFullName)
If fs.FileExists(here & "\links.py") = False Then here = sh.ExpandEnvironmentStrings("%USERPROFILE%") & "\insta-ideas"
sh.CurrentDirectory = here
cmd = "cmd /c """"" & here & "\venv\Scripts\python.exe"" """ & here & "\links.py"" 2>> """ & here & "\watcher-errors.log"""""
Do
  sh.Run cmd, 0, True
  WScript.Sleep 10000
Loop
