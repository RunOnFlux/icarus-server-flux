#!/bin/bash
# shellcheck disable=SC2016  # wait_for conditions are single-quoted and eval-ed later, on purpose
# Runs INSIDE the image (tests/test-image.sh). The repository's tests/ folder is mounted at /t
# and scripts/ at /s.
set -uo pipefail
export LC_ALL=C

pass=0
fail=0
check() { # description, expected, actual
  if [ "$2" = "$3" ]; then
    pass=$((pass + 1))
  else
    fail=$((fail + 1))
    printf 'FAIL %s\n  expected: [%s]\n  actual:   [%s]\n' "$1" "$2" "$3"
  fi
}
status() { "$@" >/dev/null 2>&1; echo $?; }
wait_for() {
  local _
  for _ in $(seq 1 $(($2 * 5))); do
    eval "$1" && return 0
    sleep 0.2
  done
  return 1
}

# --- What the scripts need is in the image -----------------------------------------------------
for tool in steamcmd python3 wine wineboot wineserver pgrep pkill tail tar; do
  check "${tool} is installed" "0" "$(status command -v "${tool}")"
done
check "wine is the pinned build" "0" "$(status bash -c 'wine --version | grep -q "wine-10.0"')"
check "and it has fsync" "0" "$(status bash -c 'wine --version | grep -q "Fsync"')"
check "flux-icarus is on the PATH" "0" "$(status command -v flux-icarus)"
check "flux-icarus starts (usage)" "64" "$(status flux-icarus)"
for script in flux-entrypoint.sh flux-lib.sh; do
  check "${script} parses" "0" "$(status bash -n "/opt/flux/${script}")"
done
check "the image's scripts are the repository's" "0" "$(status diff -r --exclude=__pycache__ /s /opt/flux)"
check "the unit tests pass with the image's Python" "0" "$(status bash -c 'cd /tmp && rm -rf repo && mkdir repo && cp -r /s repo/scripts && cp -r /t repo/tests && cd repo && ./tests/test-flux.sh')"

# --- The supervisor, around a stand-in Steam and a stand-in server -----------------------------
# steamcmd "installs" the exe (the first call fails with "Missing configuration" like a fresh
# SteamCMD does); wine runs a stand-in that behaves like the server where it matters: it answers
# the Steam query on its query port (P_s:Lobby, or empty once a prospect is loaded), logs players,
# reads ServerSettings.ini through the linked folder, loads the prospect it was told to, and ends AT
# ONCE on SIGTERM and SIGINT without saving anything.
mkdir -p /usr/local/bin /opt/standin/Icarus/Binaries/Win64
cat >/usr/local/bin/steamcmd <<'STUB'
#!/bin/bash
dir=""
for a in "$@"; do [ "${prev:-}" = "+force_install_dir" ] && dir="$a"; prev="$a"; done
echo "$*" >>/tmp/steamcmd-calls
[ -n "${dir}" ] || exit 0
if [ ! -f /tmp/.first-failed ]; then touch /tmp/.first-failed; echo "ERROR! Failed to install app '2089300' (Missing configuration)"; exit 8; fi
mkdir -p "${dir}/steamapps" "${dir}/Icarus/Binaries/Win64"
: >"${dir}/Icarus/Binaries/Win64/IcarusServer-Win64-Shipping.exe"
printf '"AppState"\n{\n\t"buildid"\t\t"100"\n}\n' >"${dir}/steamapps/appmanifest_2089300.acf"
if [ -f /tmp/.installed ]; then echo "Success! App '2089300' already up to date."; else touch /tmp/.installed; echo "Success! App '2089300' fully installed."; fi
STUB
cat >/opt/standin/Icarus/Binaries/Win64/IcarusServer-Win64-Shipping.exe <<'GAME'
#!/usr/bin/env python3
# Named like the server so the supervisor finds it by its exe name.
import os, signal, socket, sys, threading, time
cmd = " ".join(sys.argv[1:])
def arg(name, default=""):
    for a in sys.argv[1:]:
        if a.lower().startswith("-%s=" % name.lower()):
            return a.split("=", 1)[1]
    return default
port, qport, name = int(arg("port", "17777")), int(arg("QueryPort", "27015")), arg("SteamServerName")
saved = "/mnt/icarus/server/Icarus/Saved"
ini = os.path.join(saved, "Config", "WindowsServer", "ServerSettings.ini")
cfg = {}
for line in open(ini, encoding="utf-8-sig"):
    if "=" in line:
        k, v = line.strip().split("=", 1)
        cfg[k] = v
os.makedirs(os.path.join(saved, "Logs"), exist_ok=True)
log = os.path.join(saved, "Logs", "Icarus.log")
open(log, "w").write("LogInit: Command Line:  " + cmd + "\n")
print("LogInit: Command Line:  " + cmd, flush=True)
print("LogIcarusStateRecorderComponent: Display: BeginRecording - OwningActor: BP_X | Recorder: Y", flush=True)
print("env DISPLAY=%s" % os.environ.get("DISPLAY", "unset"), flush=True)
print("name=%s maxp=%s port=%d qport=%d load=%s" % (name, cfg.get("MaxPlayers"), port, qport, cfg.get("LoadProspect", "")), flush=True)
print("WARN Z:\\mnt AdminWhitelist=%s" % os.path.islink(os.path.join(saved, "AdminWhitelist.txt")), flush=True)
signal.signal(signal.SIGINT, lambda *_: os._exit(0))    # Wine: an immediate ExitProcess, no shutdown code
signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
prospect = cfg.get("LoadProspect", "")
if prospect:
    path = os.path.join(saved, "PlayerData", "DedicatedServer", "Prospects", prospect + ".json")
    print("LogDedicatedServerEntry: Attempting to load prospect: " + path, flush=True)
time.sleep(float(os.environ.get("STANDIN_BOOT", "1.5")))   # the real one needs ~25 s to the lobby
if os.environ.get("STANDIN_PLAYER"):
    with open(log, "a") as f:
        f.write("[2026.10.29-12.00.10:000][100]LogIcarusPlayerData: AddConnectedPlayer - UserId: 76561198000000001 | PlayerName: Alice\n")
sess = b"P_s:" + (b"_" + prospect.encode() if prospect else b"Lobby")
resp = (b"\xff\xff\xff\xffI\x11" + name.encode() + b"\x00\x00Icarus\x00Icarus\x00\x00\x00" + bytes([1 if os.environ.get("STANDIN_PLAYER") else 0]) + b"\x08\x00dw\x00\x010.0.0.1\x00\xb1" +
        port.to_bytes(2, "little") + b"\x10\xc8\x98\xef\xca\xcb@\x01" + b"BUILDID:0," + sess + b",G_s:3.0\x00" + b"\x00" * 8)
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.bind(("0.0.0.0", qport))
print("LogNet: GameNetDriver SteamNetDriver_1 IpNetDriver listening on port %d" % port, flush=True)
while True:
    data, addr = s.recvfrom(100)
    if data.startswith(b"\xff\xff\xff\xffTSource"):
        s.sendto(resp, addr)
GAME
chmod +x /usr/local/bin/steamcmd /opt/standin/Icarus/Binaries/Win64/IcarusServer-Win64-Shipping.exe
cat >/usr/local/bin/wine <<'STUB'
#!/bin/bash
exec /opt/standin/"$1" "${@:2}"
STUB
chmod +x /usr/local/bin/wine
cat >/usr/local/bin/wineboot <<'STUB'
#!/bin/bash
mkdir -p "${WINEPREFIX}/drive_c"
STUB
chmod +x /usr/local/bin/wineboot
export PATH="/usr/local/bin:${PATH}"

data=/mnt/icarus/data
srv=/mnt/icarus/server
# A prospect already on the volume, as a node that ran before would have it.
mkdir -p "${data}/PlayerData/DedicatedServer/Prospects"
python3 - <<'PY'
import json
doc = {"ProspectInfo": {"ProspectID": "Home", "ProspectDTKey": "OpenWorld_Styx", "Difficulty": "Medium"}, "ProspectBlob": {"BinaryBlob": "eJzs"}}
open("/mnt/icarus/data/PlayerData/DedicatedServer/Prospects/Home.json", "w").write(json.dumps(doc, indent="\t"))
PY
export FLUX_RESTART_BACKOFF=1 FLUX_UPDATE_CHECK_MINUTES=0 ICARUS_SERVER_NAME="Image Test" ICARUS_MAX_PLAYERS=4 \
  ICARUS_GAME_PORT=31000 ICARUS_QUERY_PORT=31001 ICARUS_ADMIN_PASSWORD=hunter2 STANDIN_PLAYER=1
/opt/flux/flux-entrypoint.sh >/tmp/entry.log 2>&1 &
entry=$!
check "the server comes up (its query port answers)" "0" "$(status wait_for 'grep -q "\"running\"" ${data}/flux/state.json 2>/dev/null' 40)"
check "a fresh SteamCMD's first failure is retried without validate" "0" "$(grep -c validate /tmp/steamcmd-calls)"
check "and it took two calls" "2" "$(wc -l </tmp/steamcmd-calls)"
check "the Wine prefix is made on the install volume" "0" "$(status test -d "${srv}/flux-wine/drive_c")"
check "no virtual display is started" "1" "$(status pgrep -x Xvfb)"
check "and the server gets none" "1" "$(grep -c 'env DISPLAY=unset' /tmp/entry.log)"
check "the settings file is on the data volume" "0" "$(status test -f "${data}/Config/WindowsServer/ServerSettings.ini")"
check "through the link" "${data}/Config/WindowsServer" "$(readlink "${srv}/Icarus/Saved/Config/WindowsServer")"
check "the saves are linked" "${data}/PlayerData/DedicatedServer" "$(readlink "${srv}/Icarus/Saved/PlayerData/DedicatedServer")"
check "the name, slots and ports reached the server" "1" "$(grep -c 'name=Image Test maxp=4 port=31000 qport=31001 load=Home' /tmp/entry.log)"
check "the existing prospect was the one to start on" "1" "$(grep -c 'Attempting to load prospect: .*Prospects/Home.json' /tmp/entry.log)"
check "the lists are linked" "1" "$(grep -c 'AdminWhitelist=True' /tmp/entry.log)"
check "and the status" "Image Test" "$(flux-icarus status | python3 -c 'import json,sys;print(json.load(sys.stdin)["serverName"])')"
check "the ports are the environment's" "31000 31001" "$(flux-icarus status | python3 -c 'import json,sys;d=json.load(sys.stdin)["ports"];print(d["game"],d["query"])')"
check "players come from the game's log" "Alice" "$(flux-icarus status | python3 -c 'import json,sys;print(json.load(sys.stdin)["players"][0]["name"])')"
check "the session is the loaded prospect (no lobby)" "Home" "$(flux-icarus status | python3 -c 'import json,sys;print(json.load(sys.stdin)["sessionProspect"])')"
check "the container log has no per-actor noise" "0" "$(grep -c -E '^(LogIcarusStateRecorderComponent)' /tmp/entry.log)"
check "but the game's lines reach it" "1" "$(grep -c 'listening on port 31000' /tmp/entry.log)"
check "the password is not on the command line" "0" "$(grep -c hunter2 /tmp/entry.log)"
check "the password is in the settings file" "1" "$(tr -d '\r' <"${data}/Config/WindowsServer/ServerSettings.ini" | grep -c '^AdminPassword=hunter2$')"
check "the game log is on the install volume" "0" "$(status test -s "${srv}/flux-logs/current.log")"
check "the ready check passes (the query port answers)" "0" "$(status flux-icarus ready)"
check "a backup was made at the start" "1" "$(flux-icarus backups | python3 -c 'import json,sys;print(len(json.load(sys.stdin)["backups"]))')"

# A restart through the panel: the new setting is in the settings file, and nothing counts as a crash.
flux-icarus set-settings '{"values": {"SessionName": "Second Name"}}' >/dev/null
flux-icarus restart >/dev/null
check "a panel restart starts again with the new setting" "0" "$(status wait_for '[ "$(flux-icarus status | python3 -c "import json,sys;d=json.load(sys.stdin);print(d[\"state\"],d[\"generation\"],d[\"serverName\"])")" = "running 2 Second Name" ]' 40)"
check "the restart was not counted as a crash" "0" "$(grep -c 'restarts within' /tmp/entry.log)"
check "an up-to-date install is not retried" "0" "$(grep -c 'steamcmd failed' /tmp/entry.log | awk '{print ($1 > 1) ? 1 : 0}')"

# Starting another prospect from the panel restarts on it.
python3 - <<'PY'
import json
doc = {"ProspectInfo": {"ProspectID": "Second", "ProspectDTKey": "OpenWorld_Styx", "Difficulty": "Easy"}, "ProspectBlob": {"BinaryBlob": "eJzs"}}
open("/mnt/icarus/data/PlayerData/DedicatedServer/Prospects/Second.json", "w").write(json.dumps(doc, indent="\t"))
PY
wait_for 'grep -q "\"running\"" ${data}/flux/state.json' 20
flux-icarus prospect-start Second >/dev/null
check "starting a prospect restarts on it" "0" "$(status wait_for '[ "$(flux-icarus status | python3 -c "import json,sys;d=json.load(sys.stdin);print(d[\"state\"],d[\"generation\"],d[\"prospect\"])")" = "running 3 Second" ]' 40)"

# A restore through the panel's command.
wait_for 'grep -q "\"running\"" ${data}/flux/state.json' 20
flux-icarus backup "test" force >/dev/null
backup="$(flux-icarus backups | python3 -c 'import json,sys;print(json.load(sys.stdin)["backups"][0]["name"])')"
check "restore restarts the running server" "True" "$(flux-icarus restore "${backup}" | python3 -c 'import json,sys;print(json.load(sys.stdin)["restarting"])')"
check "and the next start restored it" "0" "$(status wait_for '[ "$(flux-icarus status | python3 -c "import json,sys;print(json.load(sys.stdin)[\"generation\"])")" = 4 ]' 40)"
check "and says so" "True" "$(python3 -c 'import json;print(json.load(open("'"${data}"'/flux/last-restore.json"))["ok"])')"

# A torn prospect (a write cut short) never reaches the game: the guard puts the backup's copy back.
wait_for 'grep -q "\"running\"" ${data}/flux/state.json' 20
printf '{"ProspectInfo": {"ProspectID": "Home"}, "ProspectBlob": {"BinaryBlob": "cut' >"${data}/PlayerData/DedicatedServer/Prospects/Home.json"
flux-icarus restart >/dev/null
check "a restart with a torn prospect gets it back from the backup" "0" "$(status wait_for '[ "$(flux-icarus status | python3 -c "import json,sys;print(json.load(sys.stdin)[\"generation\"])")" = 5 ] && python3 -c "import json;json.load(open(\"${data}/PlayerData/DedicatedServer/Prospects/Home.json\"))"' 40)"
check "and the log says so" "1" "$([ "$(grep -c 'prospect Home is not valid JSON' /tmp/entry.log)" -ge 1 ] && echo 1 || echo 0)"

# A crash: restarted in place.
wait_for 'grep -q "\"running\"" ${data}/flux/state.json' 20
pkill -KILL -f '[I]carusServer-Win64-Shipping\.exe'
check "a crashed server is started again" "0" "$(status wait_for '[ "$(flux-icarus status | python3 -c "import json,sys;d=json.load(sys.stdin);print(d[\"state\"],d[\"generation\"])")" = "running 6" ]' 40)"

# A stop from Docker.
wait_for 'grep -q "\"running\"" ${data}/flux/state.json' 20
start="$(date +%s)"
kill -TERM "${entry}"
check "a stop ends the supervisor" "0" "$(status wait_for '! kill -0 ${entry} 2>/dev/null' 20)"
check "inside Docker's 10 seconds" "0" "$(status test $(($(date +%s) - start)) -le 6)"
check "and says stopped" "stopped" "$(python3 -c 'import json;print(json.load(open("'"${data}"'/flux/state.json"))["state"])')"
check "nothing of the server is left" "1" "$(status pgrep -f '[I]carusServer-Win64-Shipping\.exe')"

# With no prospect yet the server starts in its lobby.
rm -rf "${data}/PlayerData" "${data}/flux/state.json" "${data}/Config" "${data}/flux/backups"
STANDIN_PLAYER='' /opt/flux/flux-entrypoint.sh >/tmp/entry3.log 2>&1 &
entry3=$!
check "with no prospect the server comes up in its lobby" "0" "$(status wait_for '[ "$(flux-icarus status | python3 -c "import json,sys;d=json.load(sys.stdin);print(d[\"state\"],d[\"session\"])")" = "running Lobby" ]' 40)"
check "started with an empty LoadProspect" "1" "$(grep -c 'load=$' /tmp/entry3.log)"
kill -TERM "${entry3}"
wait_for '! kill -0 ${entry3} 2>/dev/null' 10

# A port the server cannot use is not a crash: the state says what to change and nothing restarts.
ICARUS_GAME_PORT=80 /opt/flux/flux-entrypoint.sh >/tmp/entry2.log 2>&1 &
entry2=$!
check "a bad port gives the needs-fix state" "0" "$(status wait_for 'grep -q "needs-fix" ${data}/flux/state.json 2>/dev/null' 20)"
check "that names the variable" "1" "$(grep -c 'ICARUS_GAME_PORT=80' ${data}/flux/state.json)"
check "and keeps the supervisor alive" "0" "$(status kill -0 ${entry2})"
kill -TERM "${entry2}"
check "a stop ends it" "0" "$(status wait_for '! kill -0 ${entry2} 2>/dev/null' 10)"

# --- flux.log is local; the banner ----------------------------------------------------------------
check "flux.log is on the install volume" "0" "$(status test -s "${srv}/flux-logs/flux.log")"
check "and not on the synced data volume" "1" "$(status test -e "${data}/flux/flux.log")"
check "the banner is in the container output" "0" "$(status grep -q 'Icarus server image by FluxCloud - https://runonflux.com' /tmp/entry.log)"
check "and in flux.log" "0" "$(status grep -q 'Host this and 30+ other games in one click: https://runonflux.com/games' "${srv}/flux-logs/flux.log")"
check "at the container start and again for each generation" "1" "$([ "$(grep -c 'server image by FluxCloud' /tmp/entry.log)" -ge 2 ] && echo 1 || echo 0)"

printf '\n%d passed, %d failed\n' "${pass}" "${fail}"
[ "${fail}" = "0" ] || { echo '--- entrypoint log'; cat /tmp/entry.log; exit 1; }
