# SSH Script sensors— target-side scripts

PRTG's **SSH Script** (`sshscript`) and **SSH Script Advanced** (`sshscriptxml`)
sensors run a script that lives on the **monitored Linux target host**, reached over
SSH by the probe. **None of these files belong on the probe image**— they are
deployed on the target. They are kept here only as reference/examples and are
intentionally **not** staged by `install-custom-sensors.sh`.

The classic probe's own SSH client (`ssh.dll` / `paelibssh.dll`) drives both
variants correctly under Wine— verified on `prtg-probe:1.0` (SSH Script and SSH
Script Advanced sensors, both **Up** against an SSH server on `127.0.0.1`). See
`docker/CUSTOM-SENSORS.md` § SSH for the full writeup.

## Deploy on the target host

| Sensor type | RawType | Target directory | Output format |
|---|---|---|---|
| SSH Script Advanced | `sshscriptxml` | `/var/prtg/scriptsxml/` | PRTG **XML** on stdout |
| SSH Script (plain)  | `sshscript`    | `/var/prtg/scripts/`    | `returncode:value:message` (v2— three fields) |

```bash
# On the TARGET host (the device PRTG monitors over SSH):
sudo mkdir -p /var/prtg/scripts /var/prtg/scriptsxml
sudo cp scripts/dockertest.sh        /var/prtg/scripts/
sudo cp scriptsxml/dockertestxml.sh  /var/prtg/scriptsxml/
sudo chmod 755 /var/prtg/scripts/dockertest.sh /var/prtg/scriptsxml/dockertestxml.sh
sudo chown <ssh-login-user>:<grp> /var/prtg/scripts/dockertest.sh /var/prtg/scriptsxml/dockertestxml.sh
```

## Two gotchas

1. **Wrong directory = "0 scripts discovered".** The Advanced sensor
   (`sshscriptxml`) lists **`/var/prtg/scriptsxml/`**, while the plain sensor
   (`sshscript`) lists **`/var/prtg/scripts/`**. A script in the wrong one yields an
   empty dropdown and the sensor never runs. This is a directory-placement issue, not
   a Wine limitation.
2. **Plain `sshscript` needs three fields.** The modern v2 SSHv2 sensor requires
   `returncode:value:message` (e.g. `0:555:msg`). The legacy two-field `value:message`
   is rejected with **PE132 "Response not well-formed"**. The Advanced variant uses
   PRTG XML and has no such issue.

## Device prerequisites (PRTG side)

Set **Linux/SSH credentials** on the device (username + password, `LinuxLoginMode`
password). If the device inherits incomplete Linux creds, turn off
`InheritLinuxCredentials` or the core rejects the metascan with *"Incomplete
connection settings"* before it reaches the probe. Create via PrtgAPI:

```powershell
$dev = Get-Device -Id <id>
$p = $dev | New-SensorParameters -RawType sshscriptxml   # or sshscript
$p.scriptfile = $p.Targets["scriptfile"] | ? { $_.Name -eq "dockertestxml.sh" }
$p.Name = "SSH Script Advanced"; $dev | Add-Sensor $p
```
