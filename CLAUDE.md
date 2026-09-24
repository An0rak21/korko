# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

KORKO mini is a demo/prototype for a self-service surfboard rental system: beacons on boards emit
RSSI, stations infer DEPART (board left)/RETOUR (board back)/ETRANGERE (foreign board returned) from
signal strength, and a cloud service turns those events into billed sessions with a web UI. Pure
Python 3 standard library only — no dependencies, no build step, no package manager, no tests. The
codebase and UI text are in French.

## Running it

Each piece runs in its own terminal (or via the VS Code launch configs in [.vscode/launch.json](.vscode/launch.json),
which also has a "tout lancer (simulateur)" compound):

```
python3 sim.py --station A          # fake radio: beacon stream on :8421, control UI on http://localhost:8081
python3 cloud.py --reset            # backend + web UI: http://localhost:9001
python3 station.py --station A      # reads the radio stream, decides DEPART/RETOUR/ETRANGERE, pushes to cloud
```

Ports (8421/8081/9001) are intentionally offset from the real kit's (8420/8080/9000) so this can run
alongside it. User-facing app: http://localhost:9001/app. Operator dashboard: http://localhost:9001/.

Other entry points:
- `calibre.py --source host:port --balise korko-01` — interactive tool that records RSSI at several
  distances and prints suggested `SEUIL_HAUT`/`SEUIL_BAS` values for `station.py`.
- `diag.py --source host:port` — dumps raw messages from a radio source and summarizes fields/devices
  seen; use this first when a real Pi's message format is unclear.

Against real hardware (see [DEMARRAGE.md](DEMARRAGE.md) for the full walkthrough), point `station.py`
at `192.168.8.100:8420` (station A; B/C are `.101`/`.102`) instead of the simulator, add `--verbose` to
see live signal/state, and use `--alias MAC=korko-0X` if beacons show up as MAC addresses. Only one
client can be connected to the Pi at a time, so stop `sim.py`/`calibre.py`/`diag.py` before starting
`station.py` against it.

There is no test suite, linter, or build step in this repo.

## Architecture

Three long-running processes talk over plain TCP/HTTP, each independently restartable without losing
state:

- **sim.py** — stands in for the real Pi/kit. Streams NDJSON RSSI readings for 6 beacons
  (`korko-01`..`korko-06`) over TCP, one JSON object per line, plus a per-second clock tick. A small
  HTTP control page (`/`, `/etat`, `/bouge`) lets you move each beacon between named positions
  (`ici`/`sable`/`2m`/`loin`/`masque`) defined in `PROFILS` (mean RSSI, noise, drop probability).

- **station.py** — the edge logic, one instance per physical station (A/B/C). Connects to a radio
  source (real or simulated), smooths RSSI per beacon with an EMA (`ALPHA`), and applies a two-threshold
  state machine with hysteresis: below `SEUIL_BAS` continuously for `DELAI_DEPART` seconds → DEPART;
  above `SEUIL_HAUT` continuously for `DELAI_RETOUR` seconds → RETOUR (or ETRANGERE if the beacon
  belongs to a different station, per the `MAISON` map). Between the two thresholds nothing changes
  (dead zone). All timing is driven by the `t` field inside incoming messages, never `time.time()` —
  this keeps it compatible with accelerated replay (`--rejeu`) and doesn't drift with wall-clock hiccups;
  `avancer_horloge()` only advances the clock from real elapsed time when the radio goes fully silent.
  Emitted events are appended to a local NDJSON file (`file_station_<nom>.ndjson`) before being pushed
  to the cloud, so nothing is lost if the cloud or network is down — a background thread retries
  delivery and only trims the file once the cloud acknowledges a batch. `extraire()` tolerates several
  incoming message shapes/field names so it can adapt to what a real kit actually sends (see `diag.py`).

- **cloud.py** — stateful backend and web server (`ThreadingHTTPServer`, no framework). Receives
  station events at `POST /evenements` (idempotent via the `vus` event-id set), maintains per-beacon
  location/session state (`planches`), turns DEPART/RETOUR pairs into priced sessions (`sessions`,
  `PRIX_MINUTE`/`PRIX_MAX`), and "sends" SMS by printing/logging them (`sms_log`). All server state
  (`stations`, `planches`, `clients`, `sessions`, `alertes`, `sms_log`, `jetons`) is a module-level
  dict/list guarded by one `RLock`, persisted as a whole to `cloud_etat.json` after every mutation and
  reloaded on startup unless `--reset` is passed — treat this file as the database. Clock for pricing
  and timeouts is `maintenant()`, derived from the latest `t` reported by stations, not wall time.
  Serves three UIs from the same process: `/` (auto-refreshing operator dashboard, server-rendered
  HTML), `/m` (single-page mobile "arm a board" flow opened via QR code), and `/app` (serves
  [app.html](app.html), a client-rendered SPA polling the JSON `Api` endpoints under `/api/...`).

- **app.html** — the rider-facing SPA served at `/app`. Vanilla JS, polls `/api/stations` and
  `/api/moi` every 1.5s (`rafraichir()`) and re-renders from scratch (`afficher()`); the auth token
  (`jeton`) is kept in `localStorage`. Login is either phone+SMS-code (`/api/login/tel` +
  `/api/login/code` — the demo code is echoed back in the response since there's no real SMS) or
  Google via Privy (`/api/login/privy`), which creates an embedded Avalanche Fuji wallet client-side;
  Privy's React bundle is loaded from esm.sh at runtime and is a no-op if the cloud wasn't started
  with `--privy-app-id`. The demo panel in this same page proxies clicks straight to `sim.py`'s
  `/bouge` endpoint via `POST /api/sim/bouge` on the cloud (to avoid CORS), so it only works against
  the simulator, not real hardware.

### Shared conventions worth knowing before editing

- `MAISON` (beacon → home station letter) is duplicated verbatim in `sim.py`, `station.py`, and
  `cloud.py` — if you add/rename a beacon or station, update all three.
- Everywhere in this codebase, simulated/logical time (the `t` field) is authoritative, not
  `time.time()`/`datetime.now()`. Follow this pattern in any new code that touches event timing so
  replay and accelerated simulation (`sim.py --vitesse`) keep working.
- Tunable detection parameters live at the top of `station.py` (`SEUIL_HAUT`, `SEUIL_BAS`,
  `DELAI_DEPART`, `DELAI_RETOUR`, `ALPHA`) — see [DEMARRAGE.md](DEMARRAGE.md) for the one-sentence
  rule they encode and known calibration values (-56 dBm at the rack, -68 dBm at ~2m).
