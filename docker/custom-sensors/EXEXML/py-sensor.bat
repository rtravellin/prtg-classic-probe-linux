@echo off
REM PRTG EXE/Script Advanced launcher that bridges to NATIVE Linux python3.
REM Wine cmd.exe cannot exec a Linux ELF directly, so we use `start /unix`
REM to launch /bin/sh, run the real interpreter, and capture stdout to a file
REM that cmd then `type`s back to PRTG. A done-sentinel makes the wait reliable
REM (Wine's `start /wait /unix` does not reliably block on the unix child).
del Z:\tmp\pyout.done >nul 2>&1
start /unix /bin/sh /opt/runpy.sh
:loop
if exist Z:\tmp\pyout.done goto done
ping -n 1 -w 200 127.0.0.1 >nul
goto loop
:done
type Z:\tmp\pyout.xml
