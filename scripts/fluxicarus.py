#!/usr/bin/env python3
"""Every decision about the Icarus server's files and launch line, and the hub's commands.

    fluxicarus.py apply             link the game's folders to the data volume, settle ServerSettings.ini
                                    (environment + panel), pick the prospect to start on, the admin and
                                    ban lists; "# " notes on stdout (the entrypoint logs them); exit 2 when
                                    the environment holds something the server cannot use (needs-fix)
    fluxicarus.py args              the server's arguments, one per line (the entrypoint reads them)
    fluxicarus.py state <state> [msg]  record what the supervisor is doing (flux/state.json)

    flux-icarus status              state, players, build, session (one JSON document)
    flux-icarus settings            every ServerSettings.ini key with its type, default and value
    flux-icarus set-settings '<json>'  {"values": {"SessionName": "My server", "MaxPlayers": 6}}
    flux-icarus list <name>         admins | banned
    flux-icarus set-list <name> '<json>'  {"ids": ["7656...", ...]}
    flux-icarus players             who is online (from the game's log) and the lists
    flux-icarus prospects           every prospect (name, mission, difficulty, size, time) and the active one
    flux-icarus prospect-start <name>   start the server on that prospect at the next start (restarts it now)
    flux-icarus prospect-delete <name>  a backup first, then the file is removed
    flux-icarus prospect-import <file>  a prospect JSON brought from a local game or another server
    flux-icarus restart             stop and start the server again (applies the above)
    flux-icarus backups | backup <why> | restore <backup>
    flux-icarus a2s                 the Steam query port's answer (name, players, session), exit 1 when none

    fluxicarus.py prospect-guard    before every start: no torn prospect file reaches the game
    fluxicarus.py backup <why> [force]  a backup (the supervisor)
    fluxicarus.py do-restore        apply a pending restore (the supervisor, before a start)
    fluxicarus.py note <key> [value]    a field of flux/state.json (nextRestart, updateWaiting)
    fluxicarus.py update-target <b> / update-check / update-skip <b>   game-update bookkeeping
    fluxicarus.py players-online    the number of players in the session, or nothing when not known

THE SAVES ARE JSON FILES, one per prospect (Saved/PlayerData/DedicatedServer/Prospects/<ProspectID>.json),
rewritten whole by the game at its autosave (ServerSettings.ini GameSaveFrequency, minutes) and, when
a player leaves, by its own rules. They live on the synced data volume. THE SERVER CANNOT BE TOLD TO SAVE
and ends at once on a stop (measured: SIGTERM and SIGINT end the process in under a second, no shutdown code
runs, SaveGameOnExit has no chance to act), so a stop loses at most GameSaveFrequency minutes. A stop that
cut a write short would leave a torn file for the other node, hence prospect-guard and the backups.

The ServerSettings.ini is the game's own file and the single source of truth: the panel and the
environment edit its lines in place (nothing else in the file moves, line endings stay).

ENVIRONMENT APPLIES WHEN IT CHANGES, as in our other images: a deploy's ICARUS_* value is written on the
first start and again whenever it is DIFFERENT from the value last applied (flux/env-applied.json), so
a redeploy with a new name renames the server while a name changed in the panel is not undone by the
same old env on every start. The ports are the exception: read on every start.
"""

import json
import os
import re
import shutil
import socket
import struct
import sys
import tarfile
import tempfile
import time

SERVER_DIR = os.environ.get("FLUX_SERVER_DIR", "/mnt/icarus/server")
DATA_DIR = os.environ.get("FLUX_DATA_DIR", "/mnt/icarus/data")
GAME_SAVED = os.environ.get("FLUX_GAME_SAVED", os.path.join(SERVER_DIR, "Icarus", "Saved"))
# The game's folders are symlinks to these on the data volume (synced).
CONFIG_DATA = os.path.join(DATA_DIR, "Config", "WindowsServer")
CONFIG_LINK = os.path.join(GAME_SAVED, "Config", "WindowsServer")
PLAYER_DATA = os.path.join(DATA_DIR, "PlayerData", "DedicatedServer")
PLAYER_LINK = os.path.join(GAME_SAVED, "PlayerData", "DedicatedServer")
PROSPECTS = os.path.join(PLAYER_DATA, "Prospects")
LISTS_DATA = os.path.join(DATA_DIR, "Lists")
# name -> file the game reads at start, in Saved/ (measured with Wine's file trace)
LISTS = {"admins": "AdminWhitelist.txt", "banned": "PlayerBanList.txt"}
GAME_LOG = os.path.join(GAME_SAVED, "Logs", "Icarus.log")
STATE_DIR = os.environ.get("FLUX_STATE_DIR", os.path.join(DATA_DIR, "flux"))
STATE_FILE = os.path.join(STATE_DIR, "state.json")
ENV_APPLIED_FILE = os.path.join(STATE_DIR, "env-applied.json")
CHOICE_FILE = os.path.join(STATE_DIR, "prospect-choice.json")
BACKUP_DIR = os.environ.get("FLUX_BACKUP_DIR", os.path.join(STATE_DIR, "backups"))
BACKUP_INDEX = os.path.join(BACKUP_DIR, "index.json")
UPLOAD_DIR = os.path.join(STATE_DIR, "uploads")
CORRUPT_DIR = os.path.join(STATE_DIR, "corrupt")
RESTORE_FILE = os.path.join(STATE_DIR, "restore-pending.json")
LAST_RESTORE_FILE = os.path.join(STATE_DIR, "last-restore.json")
# On the install volume (local, never synced): where the log reader stopped.
CURSOR_FILE = os.path.join(SERVER_DIR, "flux-logs", "players-cursor.json")
UPDATE_TARGET_FILE = os.path.join(STATE_DIR, "update-target.json")
UPDATE_FAILED_FILE = os.path.join(STATE_DIR, "update-failed.json")
UPDATE_FAILED_SKIP = 6 * 3600
RESTART_MARKER = os.environ.get("FLUX_RESTART_MARKER", "/tmp/flux-restart-requested")
STEAM_APP = os.environ.get("FLUX_STEAM_APP", "2089300")
BACKUP_NAME_RE = re.compile(r"^\d{8}-\d{6}-icarus\.tar\.gz$")
IMPORT_MAX_BYTES = int(os.environ.get("FLUX_IMPORT_MAX_BYTES", str(1024 * 1024 * 1024)))
SETTLE_SECONDS = 5
DEFAULTS_INI = os.environ.get("FLUX_DEFAULT_INI", os.path.join(os.path.dirname(os.path.realpath(__file__)), "data", "ServerSettings.default.ini"))
DATA_FILES_DIR = os.path.dirname(DEFAULTS_INI)
NAMES_FILE = os.path.join(DATA_FILES_DIR, "prospect-names.json")

DEFAULT_GAME_PORT = 17777
DEFAULT_QUERY_PORT = 27015
MAX_PLAYERS_CAP = 8   # the dedicated server wiki: "Minimum 1, Maximum 8"
STEAM_ID_RE = re.compile(r"^7656119\d{10}$")
PROSPECT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$")

INI = "ServerSettings.ini"
# env variable -> ServerSettings.ini key
ENV_KEYS = {
    "ICARUS_SERVER_NAME": "SessionName",
    "ICARUS_ADMIN_PASSWORD": "AdminPassword",
    "ICARUS_JOIN_PASSWORD": "JoinPassword",
    "ICARUS_MAX_PLAYERS": "MaxPlayers",
}
ENV_ADMINS = "ICARUS_ADMINS"
SECRET_KEYS = {"AdminPassword", "JoinPassword"}
# Keys the image owns: the panel's general form does not offer them (the start-up picks the prospect).
MANAGED_KEYS = {"LoadProspect", "CreateProspect", "ResumeProspect", "LastProspectName"}


# ------------------------------------------------------------------------------------------------
# Files
# ------------------------------------------------------------------------------------------------

def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data, indent=2):
    """Written beside and renamed, so a cut-off write never leaves half a file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".flux-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=indent, ensure_ascii=False)
        f.write("\n")
    os.chmod(tmp, 0o666)
    os.replace(tmp, path)


def write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".flux-")
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    os.chmod(tmp, 0o666)
    os.replace(tmp, path)


def read_text(path):
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            return f.read()
    except OSError:
        return None


def out(data):
    print(json.dumps(data, ensure_ascii=False))


def fail(message, code=1):
    out({"ok": False, "error": message})
    sys.exit(code)


def env_set(name):
    """A variable counts only with a value: the marketplace writes every declared parameter, the
    ones left blank as NAME=, and a blank one must not wipe what the panel set."""
    return str(os.environ.get(name, "")).strip() != ""


def truthy(value):
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def steam_id(value):
    """A SteamID64 from what was typed or pasted (a profile link too), or None."""
    value = str(value).strip()
    m = re.search(r"(?:^|/profiles/)(7656119\d{10})/?$", value)
    return m.group(1) if m else None


# ------------------------------------------------------------------------------------------------
# ServerSettings.ini: read and edited in place
# ------------------------------------------------------------------------------------------------

def parse_ini(text):
    """[(section, key, value, line_index)] and the lines (line endings kept as the file has them)."""
    lines = text.splitlines(keepends=True)
    rows, section = [], ""
    for i, raw in enumerate(lines):
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
        elif "=" in line and not line.startswith(("#", ";")):
            key, value = line.split("=", 1)
            rows.append((section, key.strip(), value.strip(), i))
    return rows, lines


def infer_type(default):
    if default in ("True", "False"):
        return "bool"
    if re.fullmatch(r"-?\d+", default):
        return "int"
    if re.fullmatch(r"-?\d+\.\d+", default):
        return "float"
    return "text"


def defaults_table():
    """{key: (section, default, type)} from the default file shipped with the image."""
    text = read_text(DEFAULTS_INI) or ""
    rows, _ = parse_ini(text)
    return {k: (s, v, infer_type(v)) for s, k, v, _ in rows}


def ini_path():
    return os.path.join(CONFIG_DATA, INI)


def load_ini():
    """(rows, lines, newline) of the live file, made from the default one when there is none yet."""
    path = ini_path()
    text = read_text(path)
    if text is None or not text.strip():
        text = read_text(DEFAULTS_INI) or ""
        os.makedirs(CONFIG_DATA, exist_ok=True)
        write_text(path, text)
    rows, lines = parse_ini(text)
    newline = "\r\n" if "\r\n" in text else "\n"
    return rows, lines, newline


def coerce(kind, value):
    """(text as the file holds it, None) or (None, why)."""
    if isinstance(value, bool):
        value = "True" if value else "False"
    value = str(value).strip()
    if kind == "bool":
        v = value.lower()
        if v in ("true", "1", "yes", "on"):
            return "True", None
        if v in ("false", "0", "no", "off"):
            return "False", None
        return None, "must be true or false"
    if kind == "int":
        if not re.fullmatch(r"-?\d+", value):
            return None, "must be a whole number"
        return str(int(value)), None
    if kind == "float":
        try:
            n = float(value)
        except ValueError:
            return None, "must be a number"
        return f"{n:.6f}", None
    if re.search(r"[\x00-\x1f\x7f]", value):
        return None, "cannot contain control characters"
    if len(value) > 400:
        return None, "is longer than 400 characters"
    return value, None


def check_key(key, kind, value, why=None):
    """Extra limits beyond the type."""
    if key == "MaxPlayers":
        if not 1 <= int(value) <= MAX_PLAYERS_CAP:
            return f"must be from 1 to {MAX_PLAYERS_CAP}"
    if key == "SessionName":
        if not value.strip():
            return "needs a name"
        if len(value) > 64:
            return "is longer than 64 characters (the Steam server name limit)"
        if '"' in value:
            return "cannot contain a double quote"
    if key == "GameSaveFrequency" and not 1 <= float(value) <= 1440:
        return "must be from 1 to 1440 minutes"
    if key in ("ShutdownIfNotJoinedFor", "ShutdownIfEmptyFor"):
        n = float(value)
        if n != -1 and not 0 <= n <= 604800:
            return "must be -1 (never) or from 0 to 604800 seconds"
    if key in SECRET_KEYS and value and (" " in value or '"' in value):
        return "cannot contain spaces or quotes"
    return why


def set_ini_values(changes, allow_managed=False):
    """changes: {key: value as given}. Returns (applied {key: text}, errors {key: why}). Unknown keys
    are refused (a typo must not become a line the game ignores)."""
    rows, lines, newline = load_ini()
    defaults = defaults_table()
    present = {k: (s, v, i) for s, k, v, i in rows}
    applied, errors = {}, {}
    for key, value in changes.items():
        if key in MANAGED_KEYS and not allow_managed:
            errors[key] = "is managed by the server (use the Prospects tab)"
            continue
        if key in present:
            kind = defaults.get(key, (None, present[key][1], infer_type(present[key][1])))[2]
        elif key in defaults:
            kind = defaults[key][2]
        else:
            errors[key] = "is not a setting of this server"
            continue
        text, why = coerce(kind, value)
        why = check_key(key, kind, text, why) if text is not None else why
        if why:
            errors[key] = why
            continue
        applied[key] = text
    if errors:
        return {}, errors
    for key, text in applied.items():
        if key in present:
            i = present[key][2]
            lines[i] = f"{key}={text}{newline}"
        else:
            section = defaults[key][0]
            at = next((n for n, raw in enumerate(lines) if raw.strip() == f"[{section}]"), None)
            if at is None:
                lines.append(f"[{section}]{newline}")
                at = len(lines) - 1
            lines.insert(at + 1, f"{key}={text}{newline}")
            rows2, lines2 = parse_ini("".join(lines))
            present = {k: (s, v, i) for s, k, v, i in rows2}
            lines = lines2
    if applied:
        write_text(ini_path(), "".join(lines))
    return applied, errors


def ini_value(key, default=None):
    rows, _, _ = load_ini()
    for _s, k, v, _i in rows:
        if k == key:
            return v
    return default


# ------------------------------------------------------------------------------------------------
# Admin and ban lists (Saved/AdminWhitelist.txt, Saved/PlayerBanList.txt: one SteamID64 per line)
# ------------------------------------------------------------------------------------------------

def list_path(name):
    return os.path.join(LISTS_DATA, LISTS[name])


def list_link(name):
    return os.path.join(GAME_SAVED, LISTS[name])


def read_list(name):
    text = read_text(list_path(name)) or ""
    ids = []
    for line in text.splitlines():
        m = re.match(r"^\s*(7656119\d{10})\b", line)
        if m and m.group(1) not in ids:
            ids.append(m.group(1))
    return ids


def write_list(name, ids):
    seen, lines = set(), []
    for i in ids:
        if i not in seen:
            seen.add(i)
            lines.append(i)
    write_text(list_path(name), "".join(f"{line}\n" for line in lines))


def parse_ids(values):
    ids, bad = [], []
    for v in values:
        sid = steam_id(v)
        (ids if sid else bad).append(sid or str(v))
    return ids, bad


def cmd_list(args):
    if not args or args[0] not in LISTS:
        fail(f"usage: list {{{'|'.join(LISTS)}}}", 64)
    out({"name": args[0], "entries": [{"id": i, "extra": ""} for i in read_list(args[0])]})


def cmd_set_list(args):
    if len(args) < 2 or args[0] not in LISTS:
        fail(f"usage: set-list {{{'|'.join(LISTS)}}} '<json>'", 64)
    try:
        data = json.loads(args[1])
        ids_in = data["ids"]
        assert isinstance(ids_in, list)
    except (ValueError, KeyError, AssertionError):
        fail('Expected {"ids": ["7656119..."]}.')
    ids, bad = parse_ids(ids_in)
    if bad:
        fail(f"Not a SteamID64: {', '.join(bad[:5])}. A SteamID64 is 17 digits starting 7656119.")
    if len(ids) > 5000:
        fail("Too many entries.")
    write_list(args[0], ids)
    out({"ok": True, "name": args[0], "count": len(set(ids)), "restartNeeded": True})


def link_lists(notes):
    """Saved/AdminWhitelist.txt and PlayerBanList.txt -> the data volume. The game may replace a link
    with a file of its own when it saves a ban: such a file is the newer truth, taken onto the volume."""
    os.makedirs(LISTS_DATA, exist_ok=True)
    os.makedirs(GAME_SAVED, exist_ok=True)
    for name in LISTS:
        data, link = list_path(name), list_link(name)
        if os.path.isfile(link) and not os.path.islink(link):
            text = read_text(link)
            if text is not None:
                write_text(data, text)
                notes.append(f"{LISTS[name]}: the game's own file taken onto the data volume")
            os.remove(link)
        if not os.path.exists(data):
            write_text(data, "")
        if os.path.islink(link) and os.readlink(link) == data:
            continue
        if os.path.lexists(link):
            os.remove(link)
        os.symlink(data, link)


def apply_admins_env(notes):
    """ICARUS_ADMINS adds admins on every start and never removes one."""
    if not env_set(ENV_ADMINS):
        return
    ids, bad = parse_ids(re.split(r"[\s,;]+", os.environ[ENV_ADMINS].strip()))
    for b in bad:
        if b:
            notes.append(f"WARN {ENV_ADMINS}: {b!r} is not a SteamID64; skipped")
    have = read_list("admins")
    add = [i for i in ids if i and i not in have]
    if add:
        write_list("admins", have + add)
        notes.append(f"{len(add)} admin(s) added from {ENV_ADMINS}")


# ------------------------------------------------------------------------------------------------
# State
# ------------------------------------------------------------------------------------------------

def load_state():
    data = read_json(STATE_FILE, {})
    return data if isinstance(data, dict) else {}


def cmd_state(args):
    state = load_state()
    new = args[0] if args else "unknown"
    if new == "starting" and state.get("state") != "starting":
        state["generation"] = int(state.get("generation") or 0) + 1
    state["state"] = new
    state["message"] = args[1] if len(args) > 1 else ""
    state["since"] = int(time.time())
    if new == "running":
        state["runningSince"] = int(time.time())
    write_json(STATE_FILE, state)


def cmd_note(args):
    if not args:
        fail("usage: note <key> [value]", 64)
    state = load_state()
    if len(args) > 1 and args[1] != "":
        state[args[0]] = int(args[1]) if args[1].isdigit() else args[1]
    else:
        state.pop(args[0], None)
    write_json(STATE_FILE, state)


# ------------------------------------------------------------------------------------------------
# Environment, ports and the launch line
# ------------------------------------------------------------------------------------------------

def env_ports(notes):
    """(game, query) from the environment, or None (with a note). Both are UDP and independent."""
    ports = []
    for name, default in (("ICARUS_GAME_PORT", DEFAULT_GAME_PORT), ("ICARUS_QUERY_PORT", DEFAULT_QUERY_PORT)):
        raw = os.environ.get(name, "").strip() or str(default)
        try:
            n = int(raw)
        except ValueError:
            notes.append(f"ERROR {name}={raw!r} is not a port")
            return None
        if not 1024 <= n <= 65535:
            notes.append(f"ERROR {name}={n} is not a port from 1024 to 65535")
            return None
        ports.append(n)
    if ports[0] == ports[1]:
        notes.append(f"ERROR ICARUS_GAME_PORT and ICARUS_QUERY_PORT are both {ports[0]}; they must differ")
        return None
    return ports[0], ports[1]


def apply_env(notes, created):
    """Each ICARUS_* value that is set, written when the file is new or the value differs from the
    one last applied."""
    applied = read_json(ENV_APPLIED_FILE, {})
    applied = applied if isinstance(applied, dict) else {}
    changed = False
    for name, key in ENV_KEYS.items():
        if not env_set(name):
            continue
        raw = os.environ[name]
        if not created and applied.get(name) == raw:
            continue
        done, errors = set_ini_values({key: raw})
        if errors:
            notes.append(f"WARN {name}: {errors[key]}; kept the current value")
        else:
            shown = "(set)" if key in SECRET_KEYS else done[key]
            notes.append(f"{key} = {shown} ({name}{', new file' if created else ', changed'})")
        applied[name] = raw
        changed = True
    if changed:
        write_json(ENV_APPLIED_FILE, applied)


def link_dir(link, target, notes, label):
    """<install>/.../<label> -> the data volume, made at every start. A real folder the game made there
    (a first run) is moved onto the volume first."""
    os.makedirs(target, exist_ok=True)
    os.makedirs(os.path.dirname(link), exist_ok=True)
    if os.path.islink(link):
        if os.readlink(link) == target:
            return
        os.remove(link)
    elif os.path.isdir(link):
        moved = 0
        for f in os.listdir(link):
            if not os.path.exists(os.path.join(target, f)):
                shutil.move(os.path.join(link, f), os.path.join(target, f))
                moved += 1
        shutil.rmtree(link, ignore_errors=True)
        notes.append(f"{moved} file(s) the game had made in its own {label} moved to the data volume")
    os.symlink(target, link)


def prune_game_files(notes):
    """The game starts a new Icarus.log at every start and renames the old one (Icarus-backup-<time>.log);
    it deletes none. Kept: the newest FLUX_GAME_LOG_FILES (5) and none older than FLUX_GAME_LOG_DAYS (14)."""
    keep = int(os.environ.get("FLUX_GAME_LOG_FILES", "5"))
    days = int(os.environ.get("FLUX_GAME_LOG_DAYS", "14"))
    folder = os.path.dirname(GAME_LOG)
    try:
        old = sorted((f for f in os.listdir(folder) if re.fullmatch(r"Icarus-backup-.*\.log", f)),
                     key=lambda f: os.path.getmtime(os.path.join(folder, f)), reverse=True)
    except OSError:
        return
    removed = 0
    for n, f in enumerate(old):
        path = os.path.join(folder, f)
        try:
            if n >= keep or os.path.getmtime(path) < time.time() - days * 86400:
                os.remove(path)
                removed += 1
        except OSError:
            pass
    if removed:
        notes.append(f"{removed} old game log(s) removed")


def choose_prospect(notes):
    """The prospect to start on: the panel's choice (used once), else the game's own LastProspectName,
    else the most recently played file; none means the lobby, where a player makes or picks one."""
    names = {p["name"] for p in prospect_files() if (read_info(os.path.join(PROSPECTS, p["file"])) or {}).get("ProspectID") == p["name"]}
    choice = read_json(CHOICE_FILE)
    if isinstance(choice, dict):
        try:
            os.remove(CHOICE_FILE)
        except OSError:
            pass
        if choice.get("name") in names:
            return choice["name"], "chosen in the panel"
        notes.append(f"WARN the prospect chosen in the panel ({choice.get('name')!r}) is gone")
    last = ini_value("LastProspectName", "")
    if last in names:
        return last, "the last one played"
    files = sorted((p for p in prospect_files() if p["name"] in names), key=lambda p: p["mtime"], reverse=True)
    if files:
        return files[0]["name"], "the most recently saved"
    return "", ""


def build_args(port, query):
    name = ini_value("SessionName", "") or "Icarus Server"
    max_players = ini_value("MaxPlayers", "8") or "8"
    args = ["-log", "-nullrhi", f"-Port={port}", f"-QueryPort={query}", f"-SteamServerName={name}", f"-MaxPlayers={max_players}"]
    extra = os.environ.get("ICARUS_EXTRA_ARGS", "").strip()
    if extra:
        args += [a for a in re.split(r"\s+", extra)
                 if a and not a.lower().startswith(("-port=", "-queryport=", "-steamservername=", "-maxplayers=", "-loadprospect", "-createprospect", "-resumeprospect"))]
    return args


def cmd_args(_args):
    ports = env_ports([])
    if ports is None:
        sys.exit(2)
    print("\n".join(build_args(*ports)))


def cmd_apply(_args):
    notes = []
    os.makedirs(STATE_DIR, exist_ok=True)
    link_dir(CONFIG_LINK, CONFIG_DATA, notes, "Config")
    link_dir(PLAYER_LINK, PLAYER_DATA, notes, "PlayerData")
    os.makedirs(PROSPECTS, exist_ok=True)
    ports = env_ports(notes)
    if ports is None:
        for line in notes:
            print(f"# {line}")
        sys.exit(2)
    prune_game_files(notes)
    created = not (read_text(ini_path()) or "").strip()
    load_ini()
    apply_env(notes, created)
    link_lists(notes)
    apply_admins_env(notes)
    target, why = choose_prospect(notes)
    changes = {"LoadProspect": target, "ResumeProspect": "True", "CreateProspect": ""}
    set_ini_values(changes, allow_managed=True)
    notes.append(f"prospect: {target} ({why})" if target else "prospect: none yet, the server starts in its lobby (join it to start one)")
    notes.append(f"{ini_value('MaxPlayers')} slots, game port {ports[0]} (UDP), query port {ports[1]} (UDP)")
    for line in notes:
        print(f"# {line}")
    state = load_state()
    state["gamePort"], state["queryPort"] = ports
    state["prospect"] = target or None
    write_json(STATE_FILE, state)


# ------------------------------------------------------------------------------------------------
# Settings (the panel's form)
# ------------------------------------------------------------------------------------------------

def cmd_settings(_args):
    rows, _, _ = load_ini()
    defaults = defaults_table()
    seen, items = set(), []
    for section, key, value, _i in rows:
        if key in MANAGED_KEYS:
            continue
        dsec, ddef, dkind = defaults.get(key, (section, value, infer_type(value)))
        items.append({"key": key, "section": section or dsec, "type": dkind, "default": ddef,
                      "value": "" if key in SECRET_KEYS else value, "known": key in defaults})
        seen.add(key)
    for key, (section, ddef, kind) in defaults.items():
        if key not in seen and key not in MANAGED_KEYS:
            items.append({"key": key, "section": section, "type": kind, "default": ddef, "value": ddef, "known": True})
    out({"settings": items, "maxPlayersCap": MAX_PLAYERS_CAP, "secrets": sorted(SECRET_KEYS),
         "passworded": bool(ini_value("JoinPassword", "")), "managed": sorted(MANAGED_KEYS)})


def cmd_set_settings(args):
    try:
        values = json.loads(args[0])["values"]
        assert isinstance(values, dict)
    except (IndexError, ValueError, KeyError, AssertionError):
        fail('Expected {"values": {"SessionName": "..."}}.')
    if len(values) > 100:
        fail("Too many settings at once.")
    applied, errors = set_ini_values(values)
    if errors:
        fail("; ".join(f"{k} {v}" for k, v in list(errors.items())[:5]))
    out({"ok": True, "applied": sorted(applied), "restartNeeded": True})


def cmd_join_password(_args):
    out({"joinPassword": ini_value("JoinPassword", "")})


# ------------------------------------------------------------------------------------------------
# Prospects
# ------------------------------------------------------------------------------------------------

def read_info(path):
    """ProspectInfo of a prospect file, or None. The game writes ProspectInfo first and the (large) blob
    after it, so only the head of the file is read; a small file is parsed whole."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            head = f.read(262144)
    except OSError:
        return None
    text = head.decode("utf-8-sig", errors="replace")
    cut = text.find('"ProspectBlob"')
    try:
        if cut >= 0:
            data = json.loads("{" + text[:cut].strip().lstrip("{").rstrip().rstrip(",") + "}")
        elif size <= len(head):
            data = json.loads(text)
        else:
            return None
    except ValueError:
        return None
    info = data.get("ProspectInfo") if isinstance(data, dict) else None
    return info if isinstance(info, dict) else None


def prospect_files():
    rows = []
    try:
        names = os.listdir(PROSPECTS)
    except OSError:
        return rows
    for f in names:
        path = os.path.join(PROSPECTS, f)
        if f.endswith(".json") and os.path.isfile(path):
            st = os.stat(path)
            rows.append({"name": f[:-5], "file": f, "size": st.st_size, "mtime": int(st.st_mtime)})
    return rows


def mission_name(key):
    names = read_json(NAMES_FILE, {})
    return names.get(key, "") if isinstance(names, dict) else ""


def cmd_prospects(_args):
    state = load_state()
    active = state.get("prospect")
    rows = []
    for p in sorted(prospect_files(), key=lambda p: p["mtime"], reverse=True):
        info = read_info(os.path.join(PROSPECTS, p["file"])) or {}
        key = str(info.get("ProspectDTKey") or "")
        rows.append({
            "name": p["name"], "size": p["size"], "time": p["mtime"],
            # The game loads a prospect by FILE name but checks the ProspectID inside: they must agree.
            "valid": bool(info) and str(info.get("ProspectID")) == p["name"],
            "mission": key, "missionName": mission_name(key), "difficulty": info.get("Difficulty") or "",
            "state": info.get("ProspectState") or "", "hardcore": bool(info.get("NoRespawns")),
            "elapsed": info.get("ElapsedTime"), "active": p["name"] == active,
        })
    choice = read_json(CHOICE_FILE)
    out({"prospects": rows, "active": active, "pending": choice.get("name") if isinstance(choice, dict) else None})


def check_prospect_name(name):
    name = str(name)
    return PROSPECT_NAME_RE.match(name) is not None and ".." not in name


def request_restart(why):
    state = load_state()
    running = state.get("state") in ("running", "starting", "updating")
    if running and not os.path.exists(RESTART_MARKER):
        with open(RESTART_MARKER, "w", encoding="utf-8") as f:
            f.write(f"requested: {why}\nnotbefore=0\n")
    return running


def cmd_prospect_start(args):
    name = args[0] if args else ""
    if not check_prospect_name(name) or name not in {p["name"] for p in prospect_files()}:
        fail("No such prospect.")
    if not valid_prospect_file(os.path.join(PROSPECTS, name + ".json")):
        fail("That prospect file cannot be read (it is not a valid prospect).")
    info = read_info(os.path.join(PROSPECTS, name + ".json")) or {}
    if str(info.get("ProspectID")) != name:
        fail(f"The file is called {name} but the prospect inside is called {info.get('ProspectID')}. The game needs the two to match; rename the file.")
    write_json(CHOICE_FILE, {"name": name, "asked": int(time.time())})
    out({"ok": True, "name": name, "restarting": request_restart(f"prospect {name}")})


def cmd_prospect_delete(args):
    name = args[0] if args else ""
    if not check_prospect_name(name) or name not in {p["name"] for p in prospect_files()}:
        fail("No such prospect.")
    if load_state().get("state") in ("running", "starting") and load_state().get("prospect") == name:
        fail("The server is running on that prospect. Start another one first, then delete it.")
    for line in make_backup(f"before deleting {name}", force=True):
        print(line, file=sys.stderr)
    os.remove(os.path.join(PROSPECTS, name + ".json"))
    if ini_value("LastProspectName", "") == name:
        set_ini_values({"LastProspectName": ""}, allow_managed=True)
    out({"ok": True, "name": name})


def cmd_prospect_import(args):
    """A prospect JSON (an upload in flux/uploads): the one a local game keeps under
    AppData/Local/Icarus/Saved/PlayerData/<SteamID>/Prospects, or another server's."""
    if not args:
        fail("usage: prospect-import <file> [overwrite]", 64)
    src = os.path.realpath(args[0])
    if not src.startswith(os.path.realpath(UPLOAD_DIR) + os.sep):
        fail("The file must be in the uploads folder.")
    overwrite = len(args) > 1 and args[1] == "overwrite"
    try:
        if os.path.getsize(src) > IMPORT_MAX_BYTES:
            fail("The file is too large.")
        with open(src, "rb") as f:
            data = json.loads(f.read().decode("utf-8-sig"))
    except OSError as e:
        fail(f"The file could not be read: {e}")
    except ValueError:
        fail("That is not a JSON file.")
    info = data.get("ProspectInfo") if isinstance(data, dict) else None
    blob = data.get("ProspectBlob") if isinstance(data, dict) else None
    if not isinstance(info, dict) or not isinstance(blob, dict) or not info.get("ProspectID"):
        fail("That is not an Icarus prospect (ProspectInfo and ProspectBlob are missing).")
    if not blob.get("BinaryBlob"):
        fail("That prospect has no saved world in it (an empty ProspectBlob).")
    name = str(info["ProspectID"])
    if not check_prospect_name(name):
        fail("The prospect's name has characters the server cannot use (letters, digits, spaces, . _ - only).")
    dest = os.path.join(PROSPECTS, name + ".json")
    if os.path.exists(dest):
        if not overwrite:
            fail(f"A prospect called {name} is already on this server.")
        for line in make_backup(f"before replacing {name}", force=True):
            print(line, file=sys.stderr)
    os.makedirs(PROSPECTS, exist_ok=True)
    shutil.copyfile(src, dest + ".part")
    os.replace(dest + ".part", dest)
    try:
        os.remove(src)
    except OSError:
        pass
    out({"ok": True, "name": name})


def valid_prospect_file(path):
    """The file parses and has the two parts. The head check is cheap; a full parse settles it."""
    try:
        with open(path, "rb") as f:
            data = json.loads(f.read().decode("utf-8-sig"))
    except (OSError, ValueError, MemoryError):
        return False
    return isinstance(data, dict) and isinstance(data.get("ProspectInfo"), dict) and "ProspectBlob" in data


def newest_backup_with(member):
    for row in list_backups():
        try:
            with tarfile.open(os.path.join(BACKUP_DIR, row["name"])) as t:
                if member in t.getnames():
                    return row
        except (OSError, tarfile.TarError):
            continue
    return None


def cmd_prospect_guard(_args):
    """Before every start: a prospect file the game could not parse (a write cut short by a kill, or a
    half-synced file) is moved aside and put back from the newest backup that has it."""
    for p in prospect_files():
        path = os.path.join(PROSPECTS, p["file"])
        if valid_prospect_file(path):
            continue
        os.makedirs(CORRUPT_DIR, exist_ok=True)
        aside = os.path.join(CORRUPT_DIR, f"{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}-{p['file']}")
        shutil.move(path, aside)
        print(f"# WARN prospect {p['name']} is not valid JSON ({p['size']} bytes): moved to flux/corrupt")
        row = newest_backup_with(f"PlayerData/DedicatedServer/Prospects/{p['file']}")
        if row and restore_members(row, [f"PlayerData/DedicatedServer/Prospects/{p['file']}"]):
            print(f"# prospect {p['name']} restored from backup {row['name']}")
        else:
            print(f"# WARN prospect {p['name']} has no backup to restore")


# ------------------------------------------------------------------------------------------------
# Backups
# ------------------------------------------------------------------------------------------------

def load_index():
    data = read_json(BACKUP_INDEX, {})
    return data if isinstance(data, dict) else {}


def list_backups():
    index = load_index()
    rows = []
    if os.path.isdir(BACKUP_DIR):
        for name in sorted(os.listdir(BACKUP_DIR), reverse=True):
            if not BACKUP_NAME_RE.match(name):
                continue
            path = os.path.join(BACKUP_DIR, name)
            st = os.stat(path)
            meta = index.get(name, {})
            rows.append({"name": name, "size": st.st_size, "time": int(meta.get("time") or st.st_mtime),
                         "why": meta.get("why") or "", "keep": bool(meta.get("keep"))})
    return rows


def prune_backups(keep):
    """The newest `keep` are kept, plus every one marked keep (made before a restore or a delete), plus
    the newest made while the server was up: a crash loop that backs up on every start can never rotate
    the last good save away."""
    rows = list_backups()
    index = load_index()
    kept = 0
    good_seen = False
    for row in rows:
        meta = index.get(row["name"], {})
        if meta.get("keep"):
            continue
        if not good_seen and meta.get("good"):
            good_seen = True
            continue
        kept += 1
        if kept > keep:
            try:
                os.remove(os.path.join(BACKUP_DIR, row["name"]))
            except OSError:
                pass
            index.pop(row["name"], None)
    write_json(BACKUP_INDEX, index)


def newest_save_time():
    newest = 0
    for p in prospect_files():
        newest = max(newest, p["mtime"])
    return newest


def make_backup(why, force=False):
    """Returns the notes. Skipped when there is nothing to back up, when nothing was saved since the
    newest backup, or the newest is younger than FLUX_BACKUP_MIN_AGE_MINUTES (unless forced)."""
    if not prospect_files():
        return ["# backup skipped: nothing saved yet"]
    min_age = int(os.environ.get("FLUX_BACKUP_MIN_AGE_MINUTES", "60")) * 60
    rows = list_backups()
    if not force and rows:
        if time.time() - rows[0]["time"] < min_age:
            return [f"# backup skipped: the newest is {int((time.time() - rows[0]['time']) / 60)} min old"]
        if newest_save_time() <= rows[0]["time"]:
            return ["# backup skipped: nothing was saved since the newest one"]
    os.makedirs(BACKUP_DIR, exist_ok=True)
    name = time.strftime("%Y%m%d-%H%M%S", time.gmtime()) + "-icarus.tar.gz"
    while os.path.exists(os.path.join(BACKUP_DIR, name)):
        time.sleep(1)
        name = time.strftime("%Y%m%d-%H%M%S", time.gmtime()) + "-icarus.tar.gz"
    path = os.path.join(BACKUP_DIR, name)
    try:
        with tarfile.open(path + ".part", "w:gz", compresslevel=3) as t:
            if os.path.isdir(PLAYER_DATA):
                t.add(PLAYER_DATA, arcname="PlayerData/DedicatedServer")
            if os.path.isfile(ini_path()):
                t.add(ini_path(), arcname=f"Config/WindowsServer/{INI}")
            for n in LISTS:
                if os.path.isfile(list_path(n)):
                    t.add(list_path(n), arcname=f"Lists/{LISTS[n]}")
    except OSError as e:
        try:
            os.remove(path + ".part")
        except OSError:
            pass
        return [f"# WARN backup failed: {e}"]
    os.replace(path + ".part", path)
    index = load_index()
    state = load_state()
    index[name] = {"time": int(time.time()), "why": why, "keep": why.startswith("before"), "good": state.get("state") == "running"}
    write_json(BACKUP_INDEX, index)
    prune_backups(int(os.environ.get("FLUX_BACKUP_KEEP", "6")))
    return [f"# backup {name} ({why}, {os.path.getsize(path) // 1024} KB)"]


def cmd_backup(args):
    why = args[0] if args else "by hand"
    for line in make_backup(why, force=len(args) > 1 and args[1] == "force"):
        print(line)


def cmd_backups(_args):
    out({"backups": list_backups()})


def safe_members(t):
    for member in t.getmembers():
        parts = member.name.split("/")
        if member.name.startswith("/") or ".." in parts:
            raise ValueError(f"unsafe path {member.name}")
        if not any(member.name == p or member.name.startswith(p + "/") for p in ("PlayerData", "Config", "Lists")):
            raise ValueError(f"unexpected entry {member.name}")
        if not (member.isfile() or member.isdir()):
            raise ValueError(f"not a file: {member.name}")


def restore_members(row, members):
    """Extracts the named members of a backup over the live files. True when they were put back."""
    path = os.path.join(BACKUP_DIR, row["name"])
    tmp = tempfile.mkdtemp(dir=DATA_DIR, prefix=".flux-restore-")
    try:
        with tarfile.open(path) as t:
            safe_members(t)
            for m in members:
                t.extract(m, tmp)  # noqa: S202 - every member checked above
        for m in members:
            src = os.path.join(tmp, m)
            if not os.path.isfile(src):
                return False
            dest = os.path.join(DATA_DIR, m)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copyfile(src, dest + ".part")
            os.replace(dest + ".part", dest)
        return True
    except (OSError, ValueError, KeyError, tarfile.TarError):
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def restore_backup(row):
    """The whole backup over the live saves: prospects, ServerSettings.ini and the lists. Prospects
    that are not in the backup stay."""
    path = os.path.join(BACKUP_DIR, row["name"])
    try:
        with tarfile.open(path) as t:
            safe_members(t)
            files = [m.name for m in t.getmembers() if m.isfile()]
    except (OSError, ValueError, tarfile.TarError):
        return False
    bad = [m for m in files if m.startswith("PlayerData/DedicatedServer/Prospects/") and m.endswith(".json")]
    return bool(files) and restore_members(row, files) and all(valid_prospect_file(os.path.join(DATA_DIR, m)) for m in bad)


def cmd_restore(args):
    name = os.path.basename(args[0]) if args else ""
    if not BACKUP_NAME_RE.match(name) or not os.path.isfile(os.path.join(BACKUP_DIR, name)):
        fail("No such backup.")
    write_json(RESTORE_FILE, {"name": name, "asked": int(time.time())})
    running = request_restart(f"restore {name}")
    out({"ok": True, "restarting": running, "name": name})


def cmd_do_restore(_args):
    pending = read_json(RESTORE_FILE)
    if not isinstance(pending, dict):
        return
    try:
        os.remove(RESTORE_FILE)
    except OSError:
        pass
    name = os.path.basename(str(pending.get("name", "")))
    if not BACKUP_NAME_RE.match(name) or not os.path.isfile(os.path.join(BACKUP_DIR, name)):
        print(f"# WARN the backup to restore is gone: {name}")
        write_json(LAST_RESTORE_FILE, {"name": name, "ok": False, "error": "that backup is gone", "time": int(time.time())})
        return
    for line in make_backup(f"before restoring {name}", force=True):
        print(line)
    if restore_backup({"name": name}):
        print(f"# restored {name}")
        write_json(LAST_RESTORE_FILE, {"name": name, "ok": True, "error": None, "time": int(time.time())})
    else:
        print(f"# WARN the restore of {name} failed")
        write_json(LAST_RESTORE_FILE, {"name": name, "ok": False, "error": "the backup could not be read or failed its check", "time": int(time.time())})


# ------------------------------------------------------------------------------------------------
# Players: the game's log, and the Steam query
# ------------------------------------------------------------------------------------------------

ADD_RE = re.compile(r"AddConnectedPlayer - UserId: (\S+) \| PlayerName: (.*?)\s*$")
REMOVE_RE = re.compile(r"RemoveConnectedPlayer - UserId: (\S+)")


def online_players(path=None):
    """[{id, name}] connected according to the game's log (AddConnectedPlayer / RemoveConnectedPlayer),
    or None when there is no log yet. The reader remembers where it stopped (a cursor on the install
    volume): the log is large and a status is asked for often. A log that is shorter than the cursor, or
    a different file, is read again from its start."""
    path = path or GAME_LOG
    try:
        st = os.stat(path)
    except OSError:
        return None
    cursor = read_json(CURSOR_FILE, {})
    cursor = cursor if isinstance(cursor, dict) else {}
    who = cursor.get("who") if isinstance(cursor.get("who"), dict) else {}
    offset = int(cursor.get("offset") or 0)
    if cursor.get("path") != path or cursor.get("ino") != st.st_ino or offset > st.st_size:
        who, offset = {}, 0
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read()
    except OSError:
        return None
    end = data.rfind(b"\n")
    if end >= 0:
        for raw in data[:end + 1].decode("utf-8", errors="replace").splitlines():
            if "ConnectedPlayer" not in raw:
                continue
            m = ADD_RE.search(raw)
            if m:
                who[m.group(1)] = m.group(2).strip()
                continue
            m = REMOVE_RE.search(raw)
            if m:
                who.pop(m.group(1), None)
        offset += end + 1
    try:
        write_json(CURSOR_FILE, {"path": path, "ino": st.st_ino, "offset": offset, "who": who}, indent=None)
    except OSError:
        pass
    return [{"id": i, "name": n} for i, n in who.items()]


def a2s_info(port, host="127.0.0.1", timeout=2.0):
    """The Steam query's A2S_INFO answer: {name, players, maxPlayers, keywords, session} or None. The
    game puts its session state in the keywords (P_s:Lobby while it waits for a prospect)."""
    request = b"\xff\xff\xff\xffTSource Engine Query\x00"
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(request, (host, port))
        data, _ = s.recvfrom(4096)
        if len(data) > 5 and data[4] == 0x41:   # a challenge
            s.sendto(request + data[5:9], (host, port))
            data, _ = s.recvfrom(4096)
    except OSError:
        return None
    finally:
        s.close()
    try:
        if data[:5] != b"\xff\xff\xff\xffI":
            return None
        pos = 6   # header byte + protocol
        fields = []
        for _ in range(4):   # name, map, folder, game
            end = data.index(b"\x00", pos)
            fields.append(data[pos:end].decode("utf-8", errors="replace"))
            pos = end + 1
        pos += 2   # app id
        players, maxp = data[pos], data[pos + 1]
        pos += 6   # players, max, bots, type, environment, visibility
        pos += 1   # vac
        end = data.index(b"\x00", pos)   # version
        pos = end + 1
        keywords = ""
        if pos < len(data) and data[pos] & 0x20:   # extra data flag
            edf = data[pos]
            pos += 1
            if edf & 0x80:
                pos += 2
            if edf & 0x10:
                pos += 8
            if edf & 0x40:
                pos += 2
                end = data.index(b"\x00", pos)
                pos = end + 1
            end = data.index(b"\x00", pos)
            keywords = data[pos:end].decode("utf-8", errors="replace")
        m = re.search(r"P_s:([^,]*)", keywords)
        return {"name": fields[0], "players": players, "maxPlayers": maxp, "keywords": keywords, "session": m.group(1) if m else ""}
    except (ValueError, IndexError, struct.error):
        return None


def query_port():
    return int(load_state().get("queryPort") or DEFAULT_QUERY_PORT)


def cmd_a2s(_args):
    info = a2s_info(query_port())
    if info is None:
        sys.exit(1)
    out(info)


def session_prospect(session):
    """The query's P_s keyword is "Lobby" while the server waits for a prospect and "_<ProspectID>" once one
    is loaded (measured)."""
    return session[1:] if session and session.startswith("_") else None


def cmd_serving(_args):
    """For the supervisor: 0 once the server answers its query port AND, when it was told to start on a
    prospect, that prospect is loaded (the session is no longer the lobby); with FLUX_READY_LOBBY=1 any
    answer counts (used after the start-up timeout)."""
    info = a2s_info(query_port())
    if info is None:
        sys.exit(1)
    if truthy(os.environ.get("FLUX_READY_LOBBY", "")) or not load_state().get("prospect"):
        return
    if session_prospect(info.get("session") or "") is None:
        sys.exit(1)


def cmd_players_online(_args):
    state = load_state()
    if state.get("state") != "running":
        return
    online = online_players()
    print(len(online) if online is not None else 0)


def cmd_players(_args):
    online = online_players()
    ids = {p["id"] for p in (online or [])}
    banned, admins = read_list("banned"), read_list("admins")
    players = [{"id": i, "name": p["name"], "online": True, "banned": i in banned, "admin": i in admins} for p in (online or []) for i in [p["id"]]]
    out({"players": players, "online": online, "admins": admins, "banned": banned, "onlineIds": sorted(ids)})


# ------------------------------------------------------------------------------------------------
# Status and restart
# ------------------------------------------------------------------------------------------------

def installed_build():
    path = os.path.join(SERVER_DIR, "steamapps", f"appmanifest_{STEAM_APP}.acf")
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            m = re.search(r'"buildid"\s+"(\d+)"', f.read())
            return m.group(1) if m else None
    except OSError:
        return None


def cmd_status(_args):
    state = load_state()
    running = state.get("state") == "running"
    online = online_players() if running else None
    query = a2s_info(query_port(), timeout=1.0) if running else None
    result = {
        "state": state.get("state") or "unknown",
        "message": state.get("message") or "",
        "since": state.get("since"),
        "runningSince": state.get("runningSince"),
        "generation": int(state.get("generation") or 0),
        "serverName": ini_value("SessionName", ""),
        "maxPlayers": int(ini_value("MaxPlayers", "0") or 0),
        "passworded": bool(ini_value("JoinPassword", "")),
        "ports": {"game": state.get("gamePort"), "query": state.get("queryPort")},
        "build": installed_build(),
        "image": os.environ.get("FLUX_IMAGE_VERSION", "dev"),
        "playerCount": (len(online) if online is not None else (query or {}).get("players", 0)) if running else None,
        "players": online if online is not None else ([] if running else None),
        "session": (query or {}).get("session") if query else None,
        "sessionProspect": session_prospect((query or {}).get("session") or "") if query else None,
        "prospect": state.get("prospect"),
        "nextRestart": state.get("nextRestart"),
        "updateWaiting": state.get("updateWaiting"),
        "restartPending": os.path.exists(RESTART_MARKER),
        "restorePending": os.path.exists(RESTORE_FILE),
        "lastRestore": read_json(LAST_RESTORE_FILE),
        "lastBackup": (list_backups() or [{}])[0].get("time"),
        "saveFrequency": float(ini_value("GameSaveFrequency", "0") or 0),
        "prospectCount": len(prospect_files()),
    }
    out(result)


def cmd_restart(_args):
    state = load_state()
    if state.get("state") not in ("running", "starting", "updating", "restarting"):
        fail("The server is not running.")
    if not os.path.exists(RESTART_MARKER):
        not_before = 0
        if state.get("state") == "running" and state.get("runningSince"):
            not_before = int(state["runningSince"]) + SETTLE_SECONDS
        with open(RESTART_MARKER, "w", encoding="utf-8") as f:
            f.write(f"requested: from the panel\nnotbefore={not_before}\n")
    out({"ok": True, "restarting": True})


def cmd_ready(_args):
    """For a HEALTHCHECK: the supervisor says the server is up and the query port answers."""
    if load_state().get("state") != "running" or a2s_info(query_port()) is None:
        sys.exit(1)


# ------------------------------------------------------------------------------------------------
# Game updates that did not install
# ------------------------------------------------------------------------------------------------

def cmd_update_target(args):
    if args:
        write_json(UPDATE_TARGET_FILE, {"build": args[0], "time": int(time.time())})


def cmd_update_check(_args):
    target = read_json(UPDATE_TARGET_FILE)
    if not isinstance(target, dict):
        return
    try:
        os.remove(UPDATE_TARGET_FILE)
    except OSError:
        pass
    build = installed_build()
    if build != str(target.get("build")):
        write_json(UPDATE_FAILED_FILE, {"build": str(target.get("build")), "time": int(time.time())})
        print(f"# WARN the update to build {target.get('build')} did not install (still {build}); not retried for 6 hours")


def cmd_update_skip(args):
    failed = read_json(UPDATE_FAILED_FILE)
    if isinstance(failed, dict) and args and str(failed.get("build")) == args[0] \
            and time.time() - float(failed.get("time") or 0) < UPDATE_FAILED_SKIP:
        sys.exit(0)
    sys.exit(1)


COMMANDS = {
    "apply": cmd_apply, "args": cmd_args, "state": cmd_state, "note": cmd_note, "status": cmd_status,
    "settings": cmd_settings, "set-settings": cmd_set_settings, "join-password": cmd_join_password,
    "list": cmd_list, "set-list": cmd_set_list,
    "players": cmd_players, "players-online": cmd_players_online, "restart": cmd_restart,
    "prospects": cmd_prospects, "prospect-start": cmd_prospect_start, "prospect-delete": cmd_prospect_delete,
    "prospect-import": cmd_prospect_import, "prospect-guard": cmd_prospect_guard,
    "backups": cmd_backups, "backup": cmd_backup, "restore": cmd_restore, "do-restore": cmd_do_restore,
    "a2s": cmd_a2s, "serving": cmd_serving, "update-target": cmd_update_target, "update-check": cmd_update_check, "update-skip": cmd_update_skip,
    "ready": cmd_ready,
}


def main(argv):
    if len(argv) < 2 or argv[1] not in COMMANDS:
        print(__doc__, file=sys.stderr)
        return 64
    COMMANDS[argv[1]](argv[2:])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
