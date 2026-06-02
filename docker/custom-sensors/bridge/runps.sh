#!/bin/sh
# Linux-side runner invoked by ps-sensor.bat via Wine `start /unix`.
# Runs the real PowerShell Core sensor script, captures output, drops a done-sentinel.
/usr/bin/pwsh -NoProfile -NonInteractive -File /opt/pssensor.ps1 > /tmp/psout.xml 2>&1
echo done > /tmp/psout.done
