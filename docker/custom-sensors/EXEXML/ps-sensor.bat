@echo off
REM PRTG EXE/Script Advanced launcher that bridges to NATIVE Linux PowerShell (pwsh).
REM Same start /unix + done-sentinel pattern as py-sensor.bat. Wine's builtin
REM powershell.exe is a non-functional stub; this runs real PowerShell Core 7.x.
del Z:\tmp\psout.done >nul 2>&1
start /unix /bin/sh /opt/runps.sh
:loop
if exist Z:\tmp\psout.done goto done
ping -n 1 -w 200 127.0.0.1 >nul
goto loop
:done
type Z:\tmp\psout.xml
