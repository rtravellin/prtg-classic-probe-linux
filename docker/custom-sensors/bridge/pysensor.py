#!/usr/bin/env python3
# Example PRTG Python Script Advanced sensor body, run by NATIVE Linux python3.
import sys
v = sys.version.split()[0]
print("<prtg>")
print("  <result>")
print("    <channel>Python Value</channel>")
print("    <value>314</value>")
print("  </result>")
print("  <result>")
print("    <channel>Python Float</channel>")
print("    <value>3.14159</value>")
print("    <float>1</float>")
print("  </result>")
print("  <text>OK from Linux python3 " + v + "</text>")
print("</prtg>")
