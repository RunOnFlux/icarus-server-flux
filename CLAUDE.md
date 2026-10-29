# CLAUDE.md

Guidance for Claude Code in this repository.

## What this is

A **Docker image repo**, not an application: the Icarus dedicated server for Flux,
`runonflux/icarus-server-flux`. The server is **Windows-only** (Steam app 2089300, anonymous), so it runs
under Wine 10.0 staging-tkg with fsync (pinned by checksum), **without a display** (`-nullrhi`), like
`~/work/scum-server-flux` (the shape this repo copies), `~/work/abioticfactor-server-flux` and
`~/work/sotf-server-flux`. Written from scratch: third-party Icarus images are reference only and are never shipped.

**PID 1 is `flux-entrypoint.sh`, never the game.** The game is a child, so a stop reaches us first.

## Commands

```bash
./tests/test-flux.sh                                   # unit tests (flux-lib.sh, fluxicarus.py)
docker build --build-arg FLUX_IMAGE_VERSION=1.0.0 -t icarus-server-flux:local .
./tests/test-image.sh icarus-server-flux:local         # the scripts INSIDE the image, stand-in steamcmd + server
docker run --rm -v "$PWD:/m" -w /m koalaman/shellcheck:stable -x scripts/*.sh tests/*.sh tests/image/*.sh .github/ci/*.sh
```

A change to what the image does bumps `VERSION` and adds a line under "Versions" in the README.
A real-game test needs the install volume (10.5 GB): see README "Measured".

## The files

- `scripts/flux-entrypoint.sh`: per generation SteamCMD (Windows build), restore, backup, prospect guard,
  `flux-icarus apply`, Wine prefix, server; watcher (query port + prospect loaded, Steam build follow, daily
  restart), backupper, log capper.
- `scripts/fluxicarus.py`: every rule about the server's files and the hub's commands.
- `scripts/flux-lib.sh`: helpers; sourcing it must stay side-effect free.
- `scripts/data/`: the shipped `ServerSettings.default.ini` (keys, order and types as the game writes them) and the
  mission names (`prospect-names.json`, from the dedicated server wiki). Refresh them when a game update changes them.
- `tests/image/in-container.sh`: stand-in server (a Python script named like the exe) that answers A2S like the real one.

## Things that will bite you (all measured on build 26134457, game 3.0.33)

- **Launch the Shipping exe directly with `-log -nullrhi`** (`Icarus/Binaries/Win64/IcarusServer-Win64-Shipping.exe`).
  `IcarusServer.exe` (the launcher) and a run without `-nullrhi` die with exit 41 ("tried to create a window") when
  there is no display. `-log` makes the console output go to stdout.
- **`-CreateProspect` / `CreateProspect=` never creates anything** (RPC response "not bound to a response handler");
  `-LoadProspect=<name>` of an existing file works. A new world is made by a player in the lobby.
- **The prospect file name must equal `ProspectInfo.ProspectID`**, or the game stays in its lobby with no error.
- **The game rewrites `ServerSettings.ini` at every start** (CRLF, floats as `5.000000`, `LastProspectName` filled in).
  The image edits lines in place and keeps whatever line ending the file has.
- **The server cannot be told to save** and ends in under a second on SIGTERM/SIGINT with no shutdown code
  (`SaveGameOnExit=True` did not change that). Only the autosave (`GameSaveFrequency`, minutes) saves.
- **Query port A2S** (`P_s:Lobby`, or `P_s:_<ProspectID>` when a prospect is loaded) is the readiness signal;
  the game port is not a separate listener you can probe.
- **The admin and ban lists** are `Saved/AdminWhitelist.txt` and `Saved/PlayerBanList.txt` (paths from Wine's
  `+file` trace; the line format, one SteamID64 per line, is NOT verified). Players: the game's log has
  `AddConnectedPlayer - UserId: <id> | PlayerName: <name>` / `RemoveConnectedPlayer - UserId: <id>` (taken from the exe's
  strings, NOT seen with a real player).
- **Max players is 8** (the wiki). Never cap it by plan.
- **The Wine must have fsync or esync** (TKG 10.16+ and WineHQ 11 are ntsync-only, `/dev/ntsync` is not available on Flux).
- **Empty env = unset.** Flux stops a container with Docker's default 10 seconds.
- **`flux.log` is local** (`$FLUX_SERVER_DIR/flux-logs/flux.log`, 5 MB then one `.1`), never on the synced data volume.
- **The scheduled CI run publishes only with something to apply** (`.github/ci/needs-rebuild.sh`).
