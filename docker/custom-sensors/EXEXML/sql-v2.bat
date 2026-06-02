@echo off
REM PRTG EXE/Script Advanced launcher for SQL v2 monitoring on the Wine probe.
REM The native v2-SQL sensor cannot run under Wine, so this bridges to
REM a fresh `wine SQLv2.exe` via `start /unix` (same pattern as py-sensor.bat) and types the
REM result back. The sensor's "exeparams" must be a target KEY into /opt/sql-bridge/targets.json.
REM Per-key temp files so concurrent SQL sensors don't collide. See docker/DOTNET-ENGINE-C.md.
del Z:\tmp\sqlout-%1.done >nul 2>&1
start /unix /bin/sh /opt/runsql.sh %1
:loop
if exist Z:\tmp\sqlout-%1.done goto done
ping -n 1 -w 200 127.0.0.1 >nul
goto loop
:done
type Z:\tmp\sqlout-%1.xml
