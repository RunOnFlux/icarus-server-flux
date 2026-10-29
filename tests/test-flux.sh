#!/bin/bash
# Unit tests: the pure decisions in flux-lib.sh and fluxicarus.py's file handling. No Docker, no
# Steam, no Wine, no game. Run from the repository root.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
export TZ=UTC

# shellcheck source=scripts/flux-lib.sh
FLUX_LOG="" source scripts/flux-lib.sh

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
tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT

# --- Inputs ------------------------------------------------------------------------------------
check "7777 is a port" "7777" "$(flux_port 7777 1)"
check "empty takes the default" "27015" "$(flux_port '' 27015)"
check "a leading zero is not octal" "9700" "$(flux_port 09700 1)"
check "port 80 is refused" "1" "$(status flux_port 80 1)"
check "a word is not a port" "1" "$(status flux_port abc 1)"
check "distinct ports" "0" "$(status flux_ports_distinct 7777 27015)"
check "a port used twice" "1" "$(status flux_ports_distinct 7777 7777)"
check "restart time 5:00" "05:00" "$(flux_restart_time 5:00)"
check "restart time off" "" "$(flux_restart_time off)"
check "restart time 24:00 refused" "1" "$(status flux_restart_time 24:00)"
# 2026-10-11 12:00 UTC = 1791720000
check "next 05:00 after noon is tomorrow" "$((1791720000 - 43200 + 86400 + 18000))" "$(flux_next_daily 1791720000 05:00)"
check "next 13:30 after noon is today" "$((1791720000 + 5400))" "$(flux_next_daily 1791720000 13:30)"
check "the wine path of a folder" 'Z:\mnt\icarus\data\Config' "$(flux_wine_path /mnt/icarus/data/Config)"
check "the game is found by its exe name" "IcarusServer-Win64-Shipping.exe" "$(printf 'x\nIcarus/Binaries/Win64/IcarusServer-Win64-Shipping.exe -log -Port=17777\n' | grep -o -E "${FLUX_GAME_PATTERN}")"
check "and pgrep's own command line is not" "1" "$(status bash -c "echo 'pgrep -f ${FLUX_GAME_PATTERN}' | grep -q -E '${FLUX_GAME_PATTERN}'")"

# --- Restart budget ----------------------------------------------------------------------------
check "old restarts drop out of the window" "950 990" "$(flux_restarts_in_window 1000 100 800 950 990)"
check "a crash counts" "0" "$(status flux_restart_counts 'the server ended on its own (exit 139)')"
check "a game update does not count" "1" "$(status flux_restart_counts 'planned: game update 1 -> 2')"
check "a panel restart does not count" "1" "$(status flux_restart_counts 'requested: from the panel')"
check "prune keeps the newest" "c d" "$(flux_files_to_prune 2 a b c d | tr '\n' ' ' | sed 's/ $//')"
check "marker notbefore" "123" "$(printf 'x\nnotbefore=123\n' >"${tmp}/m"; flux_marker_not_before "${tmp}/m")"
check "marker without notbefore" "0" "$(printf 'x\n' >"${tmp}/m"; flux_marker_not_before "${tmp}/m")"

# --- Log noise and passwords -------------------------------------------------------------------
check "per-actor recording is noise" "0" "$(status flux_noise <<<"LogIcarusStateRecorderComponent: Display: BeginRecording - OwningActor: BP_EnzymeGeyser12 | Recorder: EnzymeGeyserRecorderComponent_0")"
check "AI spawn setup is noise" "0" "$(status flux_noise <<<"LogAISpawnBehaviour: Just initialised UAISpawnBehaviour for RowHandle: Ape_Desert")"
check "console variable warnings are noise" "0" "$(status flux_noise <<<"LogConsoleManager: Warning: Setting the console variable 'r.RTXGI.DDGI' with 'SetByGameSetting' was ignored")"
check "plugin mounting is noise" "0" "$(status flux_noise <<<"LogPluginManager: Mounting plugin OnlineSubsystemSteam")"
check "graphics settings echoes are noise" "0" "$(status flux_noise <<<"LogIcarusGraphicsSettings: >> FSR Mode is now: 0")"
check "blank lines are noise" "0" "$(status flux_noise <<<"   ")"
check "the listening line is not noise" "1" "$(status flux_noise <<<"LogNet: GameNetDriver SteamNetDriver_2147481983 IpNetDriver listening on port 17777")"
check "a player joining is not noise" "1" "$(status flux_noise <<<"LogIcarusPlayerData: AddConnectedPlayer - UserId: 76561198000000001 | PlayerName: Alice")"
check "a fatal error is not noise" "1" "$(status flux_noise <<<"LogWindows: Error: Fatal error: [File:Unknown] [Line: 197]")"
check "the dedicated server entry lines are not noise" "1" "$(status flux_noise <<<"LogDedicatedServerEntry: Prospect file loaded. Loading map...")"
check "passwords are redacted" 'LogInit: Command Line:  -log -ServerPassword=*** -Port=17777 -AdminPassword=***' "$(echo 'LogInit: Command Line:  -log -ServerPassword=hunter2 -Port=17777 -AdminPassword="a"' | flux_redact)"

# --- Steam builds ------------------------------------------------------------------------------
printf '"AppState"\n{\n\t"appid"\t\t"2089300"\n\t"buildid"\t\t"26134457"\n}\n' >"${tmp}/manifest.acf"
check "installed build" "26134457" "$(flux_installed_build "${tmp}/manifest.acf")"
check "public build from app_info" "26200000" "$(printf '%s\n' '"2089300"' '{' '"depots"' '{' '"branches"' '{' '"public"' '{' '"buildid"		"26200000"' '}' | flux_public_build)"

# --- fluxicarus.py ----------------------------------------------------------------------------
data="${tmp}/data"
server="${tmp}/server"
mkdir -p "${server}/steamapps"
cp "${tmp}/manifest.acf" "${server}/steamapps/appmanifest_2089300.acf"
ic() { # VAR=value... command args: fluxicarus.py with exactly these variables
  local vars=()
  while [[ "${1:-}" == *=* ]]; do vars+=("$1"); shift; done
  env -i PATH="${PATH}" HOME="${tmp}" FLUX_DATA_DIR="${data}" FLUX_SERVER_DIR="${server}" \
    FLUX_RESTART_MARKER="${tmp}/restart-marker" "${vars[@]}" python3 scripts/fluxicarus.py "$@"
}
jget() { python3 -c 'import json,sys
d=json.load(sys.stdin)
for k in sys.argv[1].split("."):
    d=d[int(k)] if isinstance(d,list) else d[k]
print(d if not isinstance(d,(dict,list)) else json.dumps(d))' "$1"; }
ini="${data}/Config/WindowsServer/ServerSettings.ini"
inival() { tr -d '\r' <"${ini}" | sed -n "s/^$1=//p"; }
saved="${server}/Icarus/Saved"
prospects="${data}/PlayerData/DedicatedServer/Prospects"

# A prospect file the way the game writes it: ProspectInfo first, then the (large) blob.
mkprospect() { # name mission difficulty [blob]
  python3 - "$@" <<'PY'
import json, sys
name, mission, diff = sys.argv[1:4]
blob = sys.argv[4] if len(sys.argv) > 4 else "eJzs" * 5000
info = {"ProspectID": name, "ClaimedAccountID": "76561198000000001", "ClaimedAccountCharacter": 1, "ProspectDTKey": mission,
        "FactionMissionDTKey": "", "LobbyName": "", "ExpireTime": -1, "ProspectState": "Active", "AssociatedMembers": [{"AccountName": "A", "UserID": "x"}],
        "Cost": 0, "Reward": 0, "Difficulty": diff, "Insurance": False, "NoRespawns": False, "ElapsedTime": 1391, "SelectedDropPoint": 21, "CustomSettings": []}
doc = {"ProspectInfo": info, "ProspectBlob": {"Key": "actors", "Hash": "00", "TotalLength": len(blob), "DataLength": len(blob), "UncompressedLength": 9, "BinaryBlob": blob}}
print(json.dumps(doc, indent="\t"))
PY
}

ic apply >"${tmp}/apply.out"
check "apply links the game's Config to the data volume" "${data}/Config/WindowsServer" "$(readlink "${saved}/Config/WindowsServer")"
check "and the saves" "${data}/PlayerData/DedicatedServer" "$(readlink "${saved}/PlayerData/DedicatedServer")"
check "and makes the settings file from the shipped defaults" "Icarus Server" "$(inival SessionName)"
check "with the game's own section" "1" "$(grep -c '^\[/Script/Icarus.DedicatedServerSettings\]' "${ini}")"
check "the admin list is linked" "${data}/Lists/AdminWhitelist.txt" "$(readlink "${saved}/AdminWhitelist.txt")"
check "and the ban list" "${data}/Lists/PlayerBanList.txt" "$(readlink "${saved}/PlayerBanList.txt")"
check "the default launch line" "-log -nullrhi -Port=17777 -QueryPort=27015 -SteamServerName=Icarus Server -MaxPlayers=8" "$(ic args | tr '\n' ' ' | sed 's/ $//')"
check "the state records the ports" "17777 27015" "$(jget gamePort <"${data}/flux/state.json") $(jget queryPort <"${data}/flux/state.json")"
check "with no prospect the server starts in its lobby (LoadProspect empty)" "" "$(inival LoadProspect)"
check "and says so" "1" "$(grep -c 'starts in its lobby' "${tmp}/apply.out")"
check "ResumeProspect stays on" "True" "$(inival ResumeProspect)"
check "the server never shuts itself down when empty (so a prospect stays loaded)" "-1.000000" "$(inival ShutdownIfEmptyFor)"
check "the game saves by itself (the game writes it as a float)" "5.000000" "$(inival GameSaveFrequency)"

ic ICARUS_GAME_PORT=31000 ICARUS_QUERY_PORT=31001 ICARUS_SERVER_NAME="Lab One" ICARUS_MAX_PLAYERS=6 ICARUS_ADMIN_PASSWORD=adm1 ICARUS_JOIN_PASSWORD=pw1 apply >/dev/null
check "env sets the name" "Lab One" "$(inival SessionName)"
check "env sets the players" "6" "$(inival MaxPlayers)"
check "env sets the admin password" "adm1" "$(inival AdminPassword)"
check "env sets the join password" "pw1" "$(inival JoinPassword)"
check "env sets the launch line" "-log -nullrhi -Port=31000 -QueryPort=31001 -SteamServerName=Lab One -MaxPlayers=6" "$(ic ICARUS_GAME_PORT=31000 ICARUS_QUERY_PORT=31001 args | tr '\n' ' ' | sed 's/ $//')"
check "the name is one argument" "1" "$(ic args | grep -c '^-SteamServerName=Lab One$')"
check "extra arguments are added" "1" "$(ic ICARUS_EXTRA_ARGS='-MULTIHOME=1.2.3.4' args | grep -c '^-MULTIHOME=1.2.3.4$')"
check "but not ones that would fight the image's own" "0" "$(ic ICARUS_EXTRA_ARGS='-Port=1 -LoadProspect=x -CreateProspect=y' args | grep -ci -E 'loadprospect|createprospect|^-Port=1$')"
ic set-settings '{"values": {"SessionName": "Panel Name"}}' >/dev/null
ic ICARUS_SERVER_NAME="Lab One" apply >/dev/null
check "the same env value does not undo a panel change" "Panel Name" "$(inival SessionName)"
ic ICARUS_SERVER_NAME="Lab Two" apply >/dev/null
check "a changed env value applies" "Lab Two" "$(inival SessionName)"
check "an empty env value leaves the setting" "Lab Two" "$(ic ICARUS_SERVER_NAME= apply >/dev/null; inival SessionName)"
check "a port below 1024 is refused" "2" "$(status ic ICARUS_GAME_PORT=80 apply)"
check "the refusal names the variable" "1" "$(ic ICARUS_GAME_PORT=80 apply | grep -c 'ERROR ICARUS_GAME_PORT=80')"
check "both ports the same is refused" "2" "$(status ic ICARUS_GAME_PORT=20000 ICARUS_QUERY_PORT=20000 apply)"
check "a word is not a port" "2" "$(status ic ICARUS_QUERY_PORT=abc apply)"
check "args refuses what apply refuses" "2" "$(status ic ICARUS_GAME_PORT=80 args)"
check "more than 8 players is refused by the env (the game's maximum), the old value stays" "6" "$(ic ICARUS_MAX_PLAYERS=9 apply >/dev/null; inival MaxPlayers)"

# Settings, validated by the type the default has.
check "settings lists the keys the panel can change" "12" "$(ic settings | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["settings"]))')"
check "but not the ones the image manages" "0" "$(ic settings | python3 -c 'import json,sys; print(sum(s["key"] in ("LoadProspect","CreateProspect","ResumeProspect","LastProspectName") for s in json.load(sys.stdin)["settings"]))')"
check "a key has its type" "bool" "$(ic settings | python3 -c 'import json,sys
for s in json.load(sys.stdin)["settings"]:
    if s["key"]=="SaveGameOnExit": print(s["type"])')"
check "secrets are not sent back" "" "$(ic settings | python3 -c 'import json,sys
for s in json.load(sys.stdin)["settings"]:
    if s["key"]=="AdminPassword": print(s["value"])')"
check "a number key is written as the game writes it" "300.000000" "$(ic set-settings '{"values": {"ShutdownIfEmptyFor": 300}}' >/dev/null; inival ShutdownIfEmptyFor)"
check "-1 means never" "-1.000000" "$(ic set-settings '{"values": {"ShutdownIfEmptyFor": -1}}' >/dev/null; inival ShutdownIfEmptyFor)"
check "a bool is written True" "False" "$(ic set-settings '{"values": {"AllowNonAdminsToLaunchProspects": false}}' >/dev/null; inival AllowNonAdminsToLaunchProspects)"
check "an unknown key is refused" "1" "$(status ic set-settings '{"values": {"Typo": 1}}')"
check "so is a managed one" "1" "$(status ic set-settings '{"values": {"LoadProspect": "x"}}')"
check "a text where a number is wanted" "1" "$(status ic set-settings '{"values": {"MaxPlayers": "many"}}')"
check "players above the game's 8" "1" "$(status ic set-settings '{"values": {"MaxPlayers": 9}}')"
check "a name over the 64 characters Steam allows" "1" "$(status ic set-settings "{\"values\": {\"SessionName\": \"$(printf 'x%.0s' $(seq 1 65))\"}}")"
check "a password with a space" "1" "$(status ic set-settings '{"values": {"AdminPassword": "a b"}}')"
check "one bad value applies none of the batch" "True" "$(ic set-settings '{"values": {"SaveGameOnExit": false, "MaxPlayers": 99}}' >/dev/null; inival SaveGameOnExit)"
check "a save frequency of 0 is refused" "1" "$(status ic set-settings '{"values": {"GameSaveFrequency": 0}}')"
check "the join password is readable for the panel" "pw1" "$(ic join-password | jget joinPassword)"
check "CRLF files stay CRLF" "True" "$(printf '[/Script/Icarus.DedicatedServerSettings]\r\nSessionName=a\r\nMaxPlayers=4\r\n' >"${ini}"; ic set-settings '{"values": {"MaxPlayers": 5}}' >/dev/null; [ "$(grep -c $'\r' "${ini}")" = 3 ] && echo True)"
rm -f "${ini}"
ic ICARUS_JOIN_PASSWORD=pw1 apply >/dev/null
ic set-settings '{"values": {"MaxPlayers": 6, "SessionName": "Lab Two"}}' >/dev/null

# Lists.
check "a list starts empty" "0" "$(ic list admins | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["entries"]))')"
ic set-list admins '{"ids": ["76561198000000001", "https://steamcommunity.com/profiles/76561198000000002/"]}' >/dev/null
check "ids are taken from links too, one per line" "76561198000000001 76561198000000002" "$(tr '\n' ' ' <"${data}/Lists/AdminWhitelist.txt" | sed 's/ $//')"
check "and are listed" "76561198000000002" "$(ic list admins | jget entries.1.id)"
check "a name is not an id" "1" "$(status ic set-list banned '{"ids": ["bob"]}')"
check "an unknown list" "64" "$(status ic list friends)"
check "ICARUS_ADMINS adds and never removes" "3" "$(ic ICARUS_ADMINS='76561198000000003, 76561198000000001' apply >/dev/null; wc -l <"${data}/Lists/AdminWhitelist.txt")"
printf '76561198000000009\n' >"${saved}/PlayerBanList.txt.new"
rm -f "${saved}/PlayerBanList.txt"; mv "${saved}/PlayerBanList.txt.new" "${saved}/PlayerBanList.txt"
ic apply >/dev/null
check "a ban list the game replaced with its own file is taken onto the volume" "76561198000000009" "$(cat "${data}/Lists/PlayerBanList.txt")"
check "and linked again" "${data}/Lists/PlayerBanList.txt" "$(readlink "${saved}/PlayerBanList.txt")"

# Prospects.
mkdir -p "${prospects}"
mkprospect "Home Base" Tier1_Forest_Recon_0 Hard >"${prospects}/Home Base.json"
mkprospect "Styx One" OpenWorld_Styx Medium >"${prospects}/Styx One.json"
touch -d '2026-10-20 10:00:00' "${prospects}/Home Base.json"
touch -d '2026-10-25 10:00:00' "${prospects}/Styx One.json"
check "the most recently saved prospect is started when there is no better choice" "Styx One" "$(ic apply >/dev/null; inival LoadProspect)"
check "prospects are listed newest first" "Styx One" "$(ic prospects | jget prospects.0.name)"
check "with the mission and its name" "Styx" "$(ic prospects | jget prospects.0.missionName)"
check "and the difficulty" "Hard" "$(ic prospects | jget prospects.1.difficulty)"
check "the mission's name from the game's list" "BEACHHEAD: Recon" "$(ic prospects | jget prospects.1.missionName)"
check "a prospect is valid" "True" "$(ic prospects | jget prospects.0.valid)"
check "the active one is marked" "True" "$(ic prospects | jget prospects.0.active)"
python3 - "${ini}" <<'PY'
import sys
p = sys.argv[1]
t = open(p).read().replace("LastProspectName=", "LastProspectName=Home Base")
open(p, "w").write(t)
PY
check "the game's LastProspectName wins over the newest file" "Home Base" "$(ic apply >/dev/null; inival LoadProspect)"
check "starting a prospect from the panel is used once" "Styx One" "$(ic prospect-start "Styx One" >/dev/null; ic apply >/dev/null; inival LoadProspect)"
check "and not twice" "Home Base" "$(ic apply >/dev/null; inival LoadProspect)"
check "an unknown prospect is refused" "1" "$(status ic prospect-start Nope)"
check "a path is not a prospect name" "1" "$(status ic prospect-start ../../etc/passwd)"
printf '{"ProspectInfo": {"ProspectID": "Torn"}, "ProspectBlob": {"BinaryBlob": "abc' >"${prospects}/Torn.json"
check "a torn file cannot be started" "1" "$(status ic prospect-start Torn)"
rm -f "${prospects}/Torn.json"

mkprospect "Other Name" OpenWorld_Styx Easy >"${prospects}/Wrong File Name.json"
touch -d '2026-10-30 10:00:00' "${prospects}/Wrong File Name.json"
check "a file whose name is not its ProspectID is listed as not valid" "False" "$(ic prospects | python3 -c 'import json,sys
for p in json.load(sys.stdin)["prospects"]:
    if p["name"]=="Wrong File Name": print(p["valid"])')"
check "it cannot be started" "1" "$(status ic prospect-start "Wrong File Name")"
check "and is never picked as the newest" "Home Base" "$(ic apply >/dev/null; inival LoadProspect)"
rm -f "${prospects}/Wrong File Name.json"

# Backups, the guard and restore.
printf 'x' | ic backup first force >/dev/null
check "a backup is made" "1" "$(ic backups | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["backups"]))')"
check "it holds the prospects, the settings and the lists" "3" "$(tar -tzf "${data}/flux/backups/"*-icarus.tar.gz | grep -c -E 'Home Base.json|ServerSettings.ini|AdminWhitelist.txt')"
check "a second one straight away is skipped" "1" "$(ic backup again | grep -c skipped)"
check "forced, it is made" "2" "$(ic backup again force >/dev/null; ic backups | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["backups"]))')"
sleep 1
printf '{"ProspectInfo": {"ProspectID": "Home Base"}, "ProspectBlob": {"BinaryBlob": "cut off here' >"${prospects}/Home Base.json"
check "the guard moves a torn file aside" "1" "$(ic prospect-guard | grep -c 'not valid JSON')"
check "and puts the newest backup's copy back" "True" "$(python3 -c 'import json;d=json.load(open("'"${prospects}/Home Base.json"'"));print("ProspectBlob" in d)')"
check "the torn file is kept for a look" "1" "$(find "${data}/flux/corrupt" -name '*Home Base.json' | wc -l)"
check "the guard has nothing to say about good files" "0" "$(ic prospect-guard | wc -l)"
printf 'not json at all' >"${prospects}/Orphan.json"
check "a torn file with no backup is reported" "1" "$(ic prospect-guard | grep -c 'no backup')"
check "and is out of the Prospects folder" "1" "$(status test -e "${prospects}/Orphan.json")"

# Deleting and importing.
mkdir -p "${data}/flux/uploads"
mkprospect "Imported" OpenWorld_Styx Easy >"${data}/flux/uploads/up.json"
check "an import adds the prospect, named by its ProspectID" "Imported" "$(ic prospect-import "${data}/flux/uploads/up.json" | jget name)"
check "the file is there" "0" "$(status test -f "${prospects}/Imported.json")"
mkprospect "Imported" OpenWorld_Styx Easy >"${data}/flux/uploads/up.json"
check "importing the same name again is refused" "1" "$(status ic prospect-import "${data}/flux/uploads/up.json")"
check "unless asked to overwrite (after a backup)" "Imported" "$(ic prospect-import "${data}/flux/uploads/up.json" overwrite 2>/dev/null | jget name)"
check "a file outside the uploads folder is refused" "1" "$(status ic prospect-import "${tmp}/manifest.acf")"
printf '{"hello": 1}' >"${data}/flux/uploads/bad.json"
check "a JSON that is not a prospect is refused" "1" "$(status ic prospect-import "${data}/flux/uploads/bad.json")"
mkprospect "Empty" OpenWorld_Styx Easy "" >"${data}/flux/uploads/empty.json"
check "a prospect with no saved world is refused" "1" "$(status ic prospect-import "${data}/flux/uploads/empty.json")"
mkprospect "../evil" OpenWorld_Styx Easy >"${data}/flux/uploads/evil.json"
check "a name that walks out of the folder is refused" "1" "$(status ic prospect-import "${data}/flux/uploads/evil.json")"
check "deleting makes a backup first and removes the file" "1" "$(ic prospect-delete Imported >/dev/null 2>&1; ic backups | python3 -c 'import json,sys; print(sum("before deleting Imported" in b["why"] for b in json.load(sys.stdin)["backups"]))')"
check "the file is gone" "1" "$(status test -e "${prospects}/Imported.json")"
check "deleting the one that is running is refused" "1" "$(ic state running >/dev/null; ic apply >/dev/null; status ic prospect-delete "$(ic status | jget prospect)")"
ic state stopped >/dev/null
first="$(ic backups | python3 -c 'import json,sys; print([b for b in json.load(sys.stdin)["backups"] if b["why"]=="first"][0]["name"])')"
rm -f "${prospects}/Styx One.json"
check "restore asks for the next start" "False" "$(ic restore "${first}" | jget restarting)"
ic do-restore >/dev/null
check "the restore puts the prospects back" "0" "$(status test -f "${prospects}/Styx One.json")"
check "and records it" "True" "$(jget ok <"${data}/flux/last-restore.json")"
check "after a backup of what it replaced" "1" "$(ic backups | python3 -c 'import json,sys; print(sum("before restoring" in b["why"] for b in json.load(sys.stdin)["backups"]))')"
check "a restore of nothing is refused" "1" "$(status ic restore ../../x)"

# Players, from the game's log.
mkdir -p "${saved}/Logs"
cat >"${saved}/Logs/Icarus.log" <<'LOG'
[2026.10.29-12.00.00:000][  0]LogInit: Hello
[2026.10.29-12.00.10:000][100]LogIcarusPlayerData: AddConnectedPlayer - UserId: 76561198000000001 | PlayerName: Alice
[2026.10.29-12.00.11:000][101]LogIcarusPlayerData: AddConnectedPlayer - UserId: 76561198000000002 | PlayerName: Bob the Builder
[2026.10.29-12.00.12:000][102]LogIcarusStateRecorderComponent: Display: BeginRecording - OwningActor: BP_X
[2026.10.29-12.05.00:000][900]LogIcarusPlayerData: RemoveConnectedPlayer - UserId: 76561198000000001
LOG
ic state running >/dev/null
check "a player who joined and stayed is online" "Bob the Builder" "$(ic players-online >/dev/null; ic players | jget players.0.name)"
check "one who left is not" "1" "$(ic players | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["players"]))')"
check "the count" "1" "$(ic players-online)"
printf '[2026.10.29-12.06.00:000][901]LogIcarusPlayerData: AddConnectedPlayer - UserId: 76561198000000001 | PlayerName: Alice\n' >>"${saved}/Logs/Icarus.log"
check "the reader goes on from where it stopped" "2" "$(ic players-online)"
check "and the cursor is on the install volume" "0" "$(status test -f "${server}/flux-logs/players-cursor.json")"
printf '[2026.10.29-12.07.00:000][902]LogIcarusPlayerData: AddConnectedPlayer - UserId: 7656119800000000' >>"${saved}/Logs/Icarus.log"
check "a line still being written is not read" "2" "$(ic players-online)"
printf '3 | PlayerName: Carol\n' >>"${saved}/Logs/Icarus.log"
check "until it is complete" "3" "$(ic players-online)"
printf 'LogInit: new session\n' >"${saved}/Logs/Icarus.log"
check "a new, shorter log starts the count again" "0" "$(ic players-online)"
ic state stopped >/dev/null
check "no count while the server is not running" "" "$(ic players-online)"

# Status.
ic ICARUS_GAME_PORT=31000 ICARUS_QUERY_PORT=31001 apply >/dev/null
ic state running >/dev/null
check "status: the name" "Lab Two" "$(ic status | jget serverName)"
check "status: the slots" "6" "$(ic status | jget maxPlayers)"
check "status: passworded" "True" "$(ic status | jget passworded)"
check "status: the ports" "31000 31001" "$(ic status | jget ports.game) $(ic status | jget ports.query)"
check "status: the build" "26134457" "$(ic status | jget build)"
check "status: how many prospects" "2" "$(ic status | jget prospectCount)"
ic state stopped >/dev/null
check "status: a stopped server has no player count" "None" "$(ic status | jget playerCount)"
check "restart refuses when the server is not running" "1" "$(status ic restart)"
ic state running >/dev/null
check "restart writes the marker" "0" "$(ic restart >/dev/null; status test -f "${tmp}/restart-marker")"
rm -f "${tmp}/restart-marker"

# The Steam query: a stand-in that answers like the real server (bytes captured from it).
cat >"${tmp}/a2s_server.py" <<'PY'
import socket, sys
resp = b'\xff\xff\xff\xffI\x11Lab\x00\x00Icarus\x00Icarus\x00\x00\x00\x00\x08\x00dw\x00\x010.0.0.1\x00\xb1qE\x10\xc8\x98\xef\xca\xcb@\x01BUILDID:0,OWNINGID:90296065011009552,OWNINGNAME:Lab,SESSIONFLAGS:683,P_s:Lobby,G_s:3.0.33.159137-x-x\x00\x14\x8a\x11\x00\x00\x00\x00\x00'
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.bind(("127.0.0.1", int(sys.argv[1])))
print("up", flush=True)
while True:
    data, addr = s.recvfrom(100)
    if len(data) == 25:
        s.sendto(resp, addr)
PY
python3 "${tmp}/a2s_server.py" 31001 >"${tmp}/a2s.out" &
a2s_pid=$!
for _ in $(seq 1 25); do grep -q up "${tmp}/a2s.out" && break; sleep 0.2; done
check "the Steam query is read" "Lab Lobby 8" "$(ic a2s | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["name"], d["session"], d["maxPlayers"])')"
check "and the ready check passes" "0" "$(status ic ready)"
check "serving: a lobby answer is not enough when a prospect was expected" "1" "$(status ic serving)"
check "unless the timeout path accepts it" "0" "$(status ic FLUX_READY_LOBBY=1 serving)"
check "the session of a loaded prospect is its name" "Home" "$(python3 -c 'import sys; sys.path.insert(0,"scripts"); import fluxicarus as f; print(f.session_prospect("_Home"))')"
check "and the lobby is no prospect" "None" "$(python3 -c 'import sys; sys.path.insert(0,"scripts"); import fluxicarus as f; print(f.session_prospect("Lobby"))')"
kill "${a2s_pid}" 2>/dev/null
wait "${a2s_pid}" 2>/dev/null
check "no answer is a failure" "1" "$(status ic a2s)"
check "and the ready check fails" "1" "$(status ic ready)"

# Game updates that did not install.
ic update-target 26200000 >/dev/null
check "an update that did not install is noted" "1" "$(ic update-check | grep -c 'did not install')"
check "and not retried" "0" "$(status ic update-skip 26200000)"
check "another build is retried" "1" "$(status ic update-skip 26300000)"

# Old game logs are pruned.
mkdir -p "${saved}/Logs"
for i in 1 2 3 4 5 6 7; do : >"${saved}/Logs/Icarus-backup-2026.10.2${i}-10.00.00.log"; touch -d "2026-10-2${i} 10:00:00" "${saved}/Logs/Icarus-backup-2026.10.2${i}-10.00.00.log"; done
check "the newest five game logs are kept" "5" "$(FLUX_GAME_LOG_DAYS=9999 ic apply >/dev/null; find "${saved}/Logs" -name 'Icarus-backup-*' | wc -l)"

printf '\n%d passed, %d failed\n' "${pass}" "${fail}"
[ "${fail}" = "0" ]
