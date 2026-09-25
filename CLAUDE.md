# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A hackathon entry for the KORKO challenge: beacons on surfboards emit BLE advertisements, a
station infers DEPART / RETOUR / ETRANGERE from the signal, and a cloud turns those into billed
sessions. Session start/end and gamification points are mirrored on Avalanche C-Chain (Fuji
testnet). Everything is French — code, comments, docs, UI.

**The organizer ships a kit. Do not modify those files** — the team must be able to restore the
pristine version at any time: `korko.py`, `korko_sim.py`, `korko_test.py`, `station_exemple.py`,
`cloud_exemple.py`. Our work lives in separate files alongside them.

| ours | role |
|---|---|
| `ma_station.py` | the detector — the actual substance of the entry |
| `mon_cloud.py` | sessions, pricing, SMS, tubes, accounts, blockchain |
| `app.html` | throwaway test page served at `/app` (real UI comes from someone else) |
| `scorer_tout.py` | scoring harness across all scenarios / seeds / chaos |
| `lancer_sim.py` | runs the kit's simulator with a relocatable control-page port |
| `chaine.py`, `contracts/`, `deploy_contrat.py`, `generer_compte.py` | Avalanche layer |

## Running it

Three terminals:

```
python3 lancer_sim.py --page 8099          # kit simulator: stream :8420, control page :8099
python3 mon_cloud.py                       # cloud: http://localhost:9000
python3 ma_station.py --source localhost:8420
```

`--page 8099` because port 8080 (the kit's hardcoded control-page port) is commonly taken —
IPFS Desktop holds it on this machine. When 8080 is free, plain `python3 korko_sim.py` works.
The stream port 8420 is **not** relocatable from outside: `Diffuseur.__init__(self, port=PORT_FLUX)`
captures it as a default argument at def time, so reassigning the module constant has no effect.

Scoring (this is how the challenge is judged):

```
python3 scorer_tout.py                     # ours
python3 scorer_tout.py station_exemple     # the baseline to beat
python3 scorer_tout.py --graines 1,2,3,4,5 --scenarios sable,corps
```

Set `KORKO_CLOUD=""` to stop the detector POSTing to the cloud — scoring runs are ~50× faster
without an HTTP round-trip per tic. `scorer_tout.py` runs in-process, no ports needed.

There is no unit-test suite; `scorer_tout.py` is the regression test that matters.

## The detector — why it works

The kit's `Detecteur` base class gives you two methods: `observation(o)` per packet and `tic(t)`
called on a schedule *even when nothing arrives*. All timing comes from the stream's `t` field,
never `time.time()` — that is what makes accelerated replay work, and it is a hard rule
throughout the kit and our code.

Measured from `korko_sim.py`'s propagation model (`RSSI_1M=-62`, `EXPOSANT=2.6`):

| situation | RSSI | departed? |
|---|---|---|
| at the rack (1.5 m) | ≈ -67 | no |
| face down (`envers`, -5 dB) | ≈ -72 | no |
| wet body in front (`corps`, -18 dB) | ≈ **-85** | **no** |
| on the sand (`poser`, ~9 m) | ≈ **-87** | **no** |
| out at sea (`partir`, 90 m) | **below the -100 floor → no packets at all** | **yes** |

Sand and occlusion sit 2 dB apart, so **no threshold can separate them** — that is precisely
what makes `station_exemple.py` (a flat `SEUIL = -80`) produce hundreds of false departures.
The real signature of a departure is **silence**, not weakness.

So: DEPART = prolonged total silence. That single change fixes `sable`, `corps` and most of
`journee`. The second half of the rule separates a departure from a flat battery, which also
goes silent: look at the median of the last packets *before* the silence. Weak → the board was
being carried away. Strong → the beacon died at the rack; emit a maintenance warning, never a
DEPART. (`morte` and `mourante` carry **no** ground-truth events at all.)

`SILENCE_DEPART = 45 s` is bounded below by the two legitimate silences — a `mourante` beacon
drops to one packet per 6 s, and `--chaos` blacks out the network for 12 s every 180 s — and
bounded above by the scorer's 300 s tolerance, which it sits far inside.

Ground-truth timing details that constrain the emitted timestamps: a predicted event matches
only within `[v.t - 5, v.t + tol]` (tol = 300 for DEPART, 120 otherwise). So DEPART is stamped
`p.vue` (the last packet heard, ~23 s after the true event) — closer to truth than "now" and
still safely inside. RETOUR is stamped *now* rather than when the strong signal began, because
the latter can land more than 5 s *before* ground truth and would score as a false positive.

Only boards belonging to this station can DEPART. A foreign board leaving (`sen_va`) records no
ground-truth event, so it must emit nothing.

Current result: **312 justes / 0 false / 0 missed** over 7 scenarios × 12 seeds × {normal, chaos}.
Any change to the detector must be re-scored — `scorer_tout.py` exits non-zero on any error.

## The cloud

`mon_cloud.py` follows the kit's contract: port 9000, `POST /evenements` taking
**newline-delimited JSON** (not a JSON array), events keyed `evenement` (not `type`), with a
`TIC` heartbeat that drives the clock and therefore billing. All state is module-level dicts
under one `RLock`, persisted whole to `mon_cloud_etat.json` after each mutation.

Two non-obvious things that were bugs, found by running it:

- **Console encoding**: both `mon_cloud.py` and `ma_station.py` reconfigure stdout/stderr to
  UTF-8 with `errors="replace"` at import. Without it, printing `→` or an accent crashes the
  request thread on a cp1252 Windows console — a log line must never take down the server.
  (`cloud_exemple.py` has this latent bug too.)
- **On-chain session ids**: the cloud's session id restarts at 1 after `--reset`, and
  `KorkoEvents.demarrerSession` rejects an id already recorded. So the chain id is derived —
  `id_chaine()` hashes client+station+board+t_depart — and stored on the session as `id_chaine`
  so `terminerSession` reuses it. Never pass the display id to the chain.

## Blockchain layer

`chaine.py` exposes a module-level `chaine` object; `mon_cloud.py` calls `demarrer_session` /
`terminer_session` / `attribuer_points` / `enregistrer_wallet`. Calls are queued and sent from a
background thread with retry, so a slow RPC never blocks an HTTP request.

Riders are identified on-chain by `keccak256(cle_client)` — the phone number or `"privy:did:…"`
string — not by wallet address, because phone-only accounts have no wallet. `enregistrerWallet`
is the seam for linking a real per-rider smart account later. One operator EOA relays and pays
gas for everyone: that is a deliberate simplification, not ERC-4337, and not a bug to "fix"
casually.

If `.env` (needs `OPERATOR_PRIVATE_KEY`) or `contrat.json` is missing, `chaine.actif` is False
and the cloud runs identically with no chain writes. `contrat.json` holds a deployed address +
ABI and is committed; **`.env` must never be** — this repo is public.

Setup: `pip install -r requirements.txt`, `python3 generer_compte.py`, fund the printed address
at https://core.app/tools/testnet-faucet/ (Fuji C-Chain), `python3 deploy_contrat.py`.

## Conventions

- Logical time (`t` from the stream) is authoritative everywhere. Never `time.time()`.
- Board→station config comes from `korko.STATIONS`; don't redeclare it. Station A owns
  korko-01/02 only — the simulator plays station A, and B/C boards should normally never be heard.
- Secrets (`PRIVY_APP_ID`, `PRIVY_APP_SECRET`, `OPERATOR_PRIVATE_KEY`) go through environment
  variables or `.env`, never command-line arguments — argv is world-readable via `tasklist`/`wmic`.
