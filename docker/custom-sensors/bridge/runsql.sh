#!/bin/sh
# Generic SQL v2 bridge for the Wine probe (invoked by EXEXML/sql-v2.bat via Wine `start /unix`).
#
# WHY a bridge: the native PRTG v2-SQL sensor (RawType postgresql/mssqlv2/...) cannot run on the
# Wine probe because that sensor class's child-process handle IPC hits a Wine service-context
# limitation, so we bridge to a native Linux client. And running SQLv2.exe directly as the
# EXE/Script exe makes it write to the probe's captured PIPE, which yields nothing under Wine.
# This bridge sidesteps both: it runs the REAL `SQLv2.exe` in a FRESH wine context with stdout
# to a FILE (proven to work — returns the query value), then types the file back to PRTG via
# the proven EXE/Script echo-capture path (the same pattern runpy.sh / runps.sh use).
#
# Usage: the EXE/Script Advanced sensor's "exeparams" = a target KEY into the runtime creds
# file /opt/sql-bridge/targets.json. No DB credentials live in the PRTG sensor config.
#   targets.json: { "<key>": { "server":"PostgreSQL|MSSQL|MySQL|Oracle|ADOSQL", "host":"",
#                              "port":"", "database":"", "dbuser":"", "dbpass":"",
#                              "sslmode":"Prefer", "sqlfile":"<file in Custom Sensors\\sql\\<flavor>\\>",
#                              "channel":"<channel name>" } }
set -u
KEY="${1:-}"
TARGETS=/opt/sql-bridge/targets.json
RAW="/tmp/sqlout-${KEY}.raw"
OUT="/tmp/sqlout-${KEY}.xml"
DONE="/tmp/sqlout-${KEY}.done"
DBG="/tmp/sqldbg-${KEY}.txt"
SS="${WINEPREFIX:-/home/prtg/.wine}/drive_c/Program Files (x86)/PRTG Network Monitor/Sensor System"
SQLBASE_WIN='C:\Program Files (x86)\PRTG Network Monitor\Custom Sensors\sql'

emit_err() { printf '<prtg><error>1</error><text>%s</text></prtg>\n' "$1" > "$OUT"; echo done > "$DONE"; exit 0; }

[ -n "$KEY" ] || emit_err "sql-bridge: no target key (set the sensor's exeparams to a key in targets.json)"
[ -f "$TARGETS" ] || emit_err "sql-bridge: $TARGETS not found (mount per-target DB creds there)"

# Resolve the target config (python3 is present on the probe image).
CFG="$(python3 - "$TARGETS" "$KEY" 2>/dev/null <<'PY'
import sys,json,shlex
try:
    t=json.load(open(sys.argv[1]))[sys.argv[2]]
except Exception as e:
    print("ERR=%s"%shlex.quote("bad targets.json / key: %s"%e)); sys.exit(0)
flv=str(t.get('server','PostgreSQL'))
sub={'PostgreSQL':'postgresql','MSSQL':'mssql','MySQL':'mysql','Oracle':'oracle','ADOSQL':'adosql'}.get(flv,'postgresql')
vals={'SRV':flv,'HOST':t.get('host',''),'PORT':t.get('port',''),'DB':t.get('database',''),
      'USER':t.get('dbuser',''),'PASS':t.get('dbpass',''),'SSL':t.get('sslmode','Prefer'),
      'SQLFILE':t.get('sqlfile',''),'CH':t.get('channel','Value'),'SUB':sub}
print('\n'.join('%s=%s'%(k,shlex.quote(str(v))) for k,v in vals.items()))
PY
)"
eval "$CFG"
[ "${ERR:-}" = "" ] || emit_err "$ERR"

echo "RUNSQL_START key=$KEY $(date +%H:%M:%S)" > "$DBG"
cd "$SS" || emit_err "sql-bridge: Sensor System dir missing"
# Fresh wine context: the probe service has an empty env, so export the prefix/overrides
# explicitly (mscoree=b so the .NET helper gets Mono). stdout -> file (NOT the probe's pipe).
WINEDEBUG=-all WINEPREFIX=/home/prtg/.wine WINEARCH=win32 \
  WINEDLLOVERRIDES="mscoree=b;mshtml=;icmp=n" DISPLAY=:0 \
  wine SQLv2.exe -querytype=ReadData -server="$SRV" -host="$HOST" -port="$PORT" \
    -database="$DB" -dbauth=1 -dbuser="$USER" -dbpass="$PASS" -sslmode="$SSL" \
    -transaction=NoTransaction -selectvalue=ColumnId -channelids=1 -selectors=0 -isdif=0 \
    -sqlpath="${SQLBASE_WIN}\\${SUB}\\${SQLFILE}" > "$RAW" 2>/dev/null
echo "RUNSQL_WINE_RC=$? $(date +%H:%M:%S)" >> "$DBG"

# SQLv2.exe emits a UTF-8 BOM + <?xml?> decl and uses <channelid> (for the native sensor's
# predefined channels). EXE/Script Advanced needs output starting at <prtg> with a named
# <channel>. Strip the decl/BOM line and rewrite <channelid>N</channelid> -> <channel>NAME</channel>.
sed "/<?xml/d; s#<channelid>[0-9]*</channelid>#<channel>${CH}</channel>#" "$RAW" > "$OUT"
echo done > "$DONE"
