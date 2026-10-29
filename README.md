# icarus-server-flux

Icarus dedicated server for Flux: `runonflux/icarus-server-flux`.

RocketWerkz ships the dedicated server for Windows only (Steam app 2089300, anonymous download, no Icarus licence
needed), so the image runs it under Wine 10.0 staging-tkg with fsync (the Wine of our V Rising, Enshrouded, Sons of
the Forest, SCUM and Abiotic Factor images), **with no virtual display**: the server runs with `-nullrhi`. Base
`steamcmd/steamcmd:ubuntu-24`. PID 1 is a bash supervisor (`flux-entrypoint.sh`); `flux-icarus`
(`fluxicarus.py`) holds every decision about the game's files and is the hub's CLI. Written from scratch;
third-party Icarus images were only read for reference.

- SteamCMD update of the Windows build on every start (anonymous), retried when a fresh SteamCMD says "Missing configuration".
- `ServerSettings.ini` (12 keys, typed from the game's own file) and the admin and ban lists written from the
  environment and the hub's panel.
- **Prospects** (the saves, one JSON file each) live on the synced volume; the image lists them, starts the
  server on the one the owner picks (or the last one played), imports one from a local game, deletes one (after a backup).
- **The server cannot be told to save** and ends at once on a stop (see "The stop"); the game's own autosave
  (`GameSaveFrequency`, 5 minutes by default here) is what protects a world.
- Crash restart in place (5 in an hour, then exit 42), game-update following, optional daily restart.
- Backups (start when something was saved, every 6 h, before a restore or a delete), restore, a prospect guard
  that puts a torn file back from a backup, players (from the game's log), a JSON status for the hub.
- **Settings the environment cannot fix are not a crash**: state `needs-fix`, nothing restarts.

## Volumes

| Path | What | Flux mount |
|---|---|---|
| `/mnt/icarus/server` | game install (10.5 GB), Wine prefix (`flux-wine`), the game's logs, `flux-logs/` (our supervisor log, 5 MB then one `.1`, and the console copy) | `ml:` (local, never synced) |
| `/mnt/icarus/data` | `Config/WindowsServer/` (the settings), `PlayerData/DedicatedServer/` (`Prospects/*.json`, `Accolades.json`), `Lists/` (admins, bans), `flux/` (state, backups, uploads) | `g:` primary |

At every start `<install>/Icarus/Saved/Config/WindowsServer` and `.../PlayerData/DedicatedServer` are made symlinks
to the data volume, and `Saved/AdminWhitelist.txt` / `Saved/PlayerBanList.txt` (the paths the game reads, found with
Wine's file trace) symlinks to `Lists/`.

## Ports

Game **UDP** `ICARUS_GAME_PORT` (default 17777, `-Port=`) and Steam query **UDP** `ICARUS_QUERY_PORT` (default 27015,
`-QueryPort=`): independent ports, neither in a Flux-banned range. The query port answers A2S_INFO (name, players,
`P_s:Lobby` while waiting for a prospect, `P_s:_<ProspectID>` once one is loaded); the game port is advertised in its reply.

## Environment

Empty = unset. A value is written to `ServerSettings.ini` when the file is new or the value changed since the
last start, so the hub's panel edits survive restarts.

| Variable | Sets | Default |
|---|---|---|
| `ICARUS_GAME_PORT`, `ICARUS_QUERY_PORT` | `-Port=`, `-QueryPort=` (1024-65535, different) | `17777`, `27015` |
| `ICARUS_SERVER_NAME` | `SessionName` and `-SteamServerName=` (64 characters) | `Icarus Server` |
| `ICARUS_ADMIN_PASSWORD` | `AdminPassword` (`/AdminLogin <password>` in game chat) | none (see below) |
| `ICARUS_JOIN_PASSWORD` | `JoinPassword` | none |
| `ICARUS_MAX_PLAYERS` | `MaxPlayers` and `-MaxPlayers=` (1-8, the game's documented maximum; never capped by the plan) | `8` |
| `ICARUS_ADMINS` | `Saved/AdminWhitelist.txt`: SteamID64s, comma separated, added on every start, never removed | none |
| `ICARUS_EXTRA_ARGS` | more launch arguments (the image's own, and the prospect ones, are dropped) | none |
| `ICARUS_RESTART_TIME` | daily restart `HH:MM` (TZ, UTC if unset), on an empty server if one comes within 60 min | none |
| `SKIP_UPDATE` | do not run SteamCMD (an install must exist) | off |
| `FLUX_STOP_GRACE` / `FLUX_RESTART_GRACE` | seconds a stop / a planned restart may take | 8 / 60 |
| `FLUX_BACKUP_HOURS`, `FLUX_BACKUP_KEEP` | backup cadence and count | 6, 6 |
| `FLUX_RESTART_MAX_ATTEMPTS`, `FLUX_RESTART_WINDOW` | crash budget | 5 per 3600 s |

Passwords go to `ServerSettings.ini` (where the game reads them), never on the command line. The wiki says an
empty `AdminPassword` lets `/AdminLogin` give anyone admin: the hub's deployment requires one.

## `flux-icarus` (the hub's CLI)

`status`, `settings`, `set-settings '{"values": {...}}'`, `list|set-list <admins|banned>`, `players`,
`prospects`, `prospect-start <name>`, `prospect-delete <name>`, `prospect-import <upload> [overwrite]`,
`backups`, `backup`, `restore <name>`, `restart`, `join-password`, `a2s`. Every command answers one JSON document. Settings
are validated by the type of the game's own file (bool, int, float, text; `MaxPlayers` 1-8; `GameSaveFrequency` 1-1440
minutes; the shutdown timers -1 or 0-604800 s) and refused as a whole if one is wrong. Run `flux-icarus` for the list.

## Prospects

A prospect is `Saved/PlayerData/DedicatedServer/Prospects/<ProspectID>.json`: `ProspectInfo` first (id, mission
`ProspectDTKey`, difficulty, state, elapsed time, owner), then `ProspectBlob` (the world, zlib+base64).

- **The file name must equal `ProspectInfo.ProspectID`** (measured: a file whose name differs is not loaded and the
  server stays in its lobby). The image lists such a file as not valid and never starts it; an import names it by its ProspectID.
- **Which prospect starts**: the one chosen in the panel (used once), else the game's own `LastProspectName`, else the
  newest file, else none. The image writes `LoadProspect=<name>` and `ResumeProspect=True` before every start. With
  none, the server starts in its **lobby**; a player joins, uses the lobby UI to create or pick a prospect (anyone, unless
  `AllowNonAdminsToLaunchProspects` is off), and the game records it as `LastProspectName`.
- **`-CreateProspect` / `CreateProspect=` does not work in game 3.0.33 under Wine** (measured, five variants): the server
  logs `RPC: GenerateProspects response, OnResGenerateProspects was not bound to a response handler` and stays in its
  lobby, so the image never uses it. `-LoadProspect=<name>` with a real prospect file works. A prospect from a local
  single-player game (`%LOCALAPPDATA%\Icarus\Saved\PlayerData\<SteamID>\Prospects\`) can be imported.
- A new prospect has to be made by a player in the lobby (not testable without a game client: **nothing in this image
  has been run with a real player connected**).
- The server stays in its prospect when empty (`ShutdownIfEmptyFor=-1`, `ShutdownIfNotJoinedFor=-1` by default);
  with a timer it returns to the lobby and the next start of the container loads the last prospect again.

## The stop

**The server cannot be told to save.** SIGTERM and SIGINT end it at once (Wine's ExitProcess, under a second, the log
just stops, no shutdown code runs; `SaveGameOnExit=True` in the file did not make it save, tested). There is no console
input and no RCON outside the game. A stop therefore loses at most `GameSaveFrequency` minutes (default 5; the game's
own wording for the option is "minutes between autosaves"). `docker stop` on a loaded server returned in 0.9 s.
Do not promise "saved on stop" in copy. The prospect is rewritten whole at each autosave, so a kill during a write can
leave a torn file: the **prospect guard** before every start moves a file that does not parse to `flux/corrupt/` and
puts the newest backup's copy back.

## Versions

- 1.0.0 (2026-10-29): first release.

## Measured

Lab: WSL2 host, Linux 6.6.87, 20 cores, 15 GB RAM, Docker 28.3.2. Game 3.0.33 "Dangerous Horizons" (Steam build 26134457).
**No player ever connected: no game client was available.** The loaded world is an old community save of the Styx
open-world map (125 KB, the world regenerates around it), because a new prospect cannot be created without a client.

| What | Value |
|---|---|
| Install | 10.46 GB (Steam: 10,463,064,256 bytes, 7.07 GB download); Wine prefix about 0.5 GB |
| Lobby (no prospect), idle | **1.09 GB** RSS, 15-20 % of a core |
| Prospect loaded (Styx), idle, no player | **3.83 GB** RSS, flat over 15 min; CPU 90 % at load decaying to 15 % |
| Lobby answering A2S | about 25-40 s after the container starts (SteamCMD check 13 s + Wine prefix once) |
| Prospect loaded | about 30 s after the lobby |
| `docker stop` on a loaded server | 0.9 s, exit 143 from the supervisor's own stop |
| Threads | 35 |
| Ports | UDP game port (advertised), UDP query port (A2S) |

Figures with players are NOT measured. Reports found for the Windows server: 8.5 GB used physical at a few players in a
long session (RocketWerkz issue tracker, an out-of-memory crash), guides quote 6-8 GB for 2-4 players and 16-32 GB for 8.
The plans in the hub are sized from those, not from this lab.

The lab scripts and fixtures are in `~/work/game-drafts/icarus/lab/` (not part of the image).
