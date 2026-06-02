#!/bin/bash
# PRTG "SSH Script" (plain, RawType sshscript) example.
#
# DEPLOY LOCATION: on the *monitored Linux TARGET host*, NOT on the probe:
#     /var/prtg/scripts/dockertest.sh   (chmod 755, owned by the SSH login user)
#
# IMPORTANT — output format. The modern SSH Script sensor is handled by the v2
# "SSHv2" Momo module on this probe, which requires THREE colon-separated fields:
#     returncode:value:message
# e.g. "0:555:All good". returncode 0=OK, 1=Warning, 2=Down. The legacy two-field
# "value:message" form (e.g. "555:OK") is REJECTED with PE132 "Response not
# well-formed" by the v2 sensor. (The SSH Script *Advanced* variant uses PRTG XML
# instead — see ../scriptsxml/dockertestxml.sh.)
echo "0:555:OK from SSH Script (plain) on Wine probe"
