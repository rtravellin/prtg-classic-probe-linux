#!/bin/bash
# PRTG "SSH Script Advanced" (RawType sshscriptxml) example.
#
# DEPLOY LOCATION: on the *monitored Linux TARGET host*, NOT on the probe:
#     /var/prtg/scriptsxml/dockertestxml.sh   (chmod 755, owned by the SSH login user)
#
# The classic probe's SSH client lists /var/prtg/scriptsxml/ over SSH during the
# sensor's metascan to populate the script dropdown, then runs the chosen script over a
# shell channel (SSH_ORIGINAL_COMMAND is empty — PRTG types the command into the shell).
# Output must be PRTG XML on stdout, exactly like an EXE/Script Advanced sensor.
echo "<prtg><result><channel>SSH XML Value</channel><value>654</value></result><text>OK from SSH Script Advanced on Wine probe</text></prtg>"
