Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
batPath = fso.BuildPath(scriptDir, "run_monitor.bat")
WshShell.Run chr(34) & batPath & chr(34), 0, False
Set WshShell = Nothing
