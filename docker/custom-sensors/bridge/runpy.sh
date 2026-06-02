#!/bin/sh
# Linux-side runner invoked by py-sensor.bat via Wine `start /unix`.
# Runs the real CPython sensor script, captures output, drops a done-sentinel.
/usr/bin/python3 /opt/pysensor.py > /tmp/pyout.xml 2>&1
echo done > /tmp/pyout.done
