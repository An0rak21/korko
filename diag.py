#!/usr/bin/env python3
"""KORKO mini — diagnostic : montre ce que la station (le Pi) envoie vraiment.

    python3 diag.py --source 192.168.8.100:8420

Affiche les premières lignes brutes, puis un résumé après DUREE secondes :
format des messages, champs présents, appareils entendus, force du signal, rythme.
Copie-colle la sortie si quelque chose ne marche pas.
"""
import argparse, json, socket, statistics, time
from collections import defaultdict

ap = argparse.ArgumentParser()
ap.add_argument("--source", default="192.168.8.100:8420")
ap.add_argument("--duree", type=int, default=20)
ap.add_argument("--lignes", type=int, default=15, help="nombre de lignes brutes à afficher")
a = ap.parse_args()

hote, port = a.source.rsplit(":", 1)
print(f"Connexion à {a.source}…")
try:
    c = socket.create_connection((hote, int(port)), timeout=10)
except OSError as e:
    print(f"ÉCHEC de connexion : {e}")
    print("-> Es-tu bien sur le Wi-Fi de la maquette ? L'adresse et le port sont-ils les bons ?")
    raise SystemExit(1)
c.settimeout(2)
print("Connecté. Lignes brutes :\n")

debut, tampon, n, non_json = time.monotonic(), b"", 0, 0
cles = defaultdict(int)
appareils = defaultdict(list)
ts, silences, dernier = [], [], time.monotonic()
while time.monotonic() - debut < a.duree:
    try:
        bloc = c.recv(4096)
    except socket.timeout:
        continue
    if not bloc:
        print("\nLa station a fermé la connexion (un seul client à la fois ? un autre programme est branché ?)")
        break
    maintenant = time.monotonic()
    silences.append(maintenant - dernier); dernier = maintenant
    tampon += bloc
    while b"\n" in tampon:
        ligne, tampon = tampon.split(b"\n", 1)
        if not ligne.strip():
            continue
        n += 1
        if n <= a.lignes:
            print("  " + ligne.decode(errors="replace")[:200])
        try:
            m = json.loads(ligne)
        except ValueError:
            non_json += 1
            continue
        if not isinstance(m, dict):
            non_json += 1
            continue
        for k in m:
            cles[k] += 1
        ident = next((m[k] for k in ("balise", "id", "beacon", "nom", "name", "mac", "addr", "address") if m.get(k)), None)
        rssi = next((m[k] for k in ("rssi", "RSSI", "signal") if m.get(k) is not None), None)
        t = next((m[k] for k in ("t", "ts", "time", "timestamp") if m.get(k) is not None), None)
        if t is not None:
            try: ts.append(float(t))
            except (TypeError, ValueError): pass
        if ident is not None and rssi is not None:
            try: appareils[str(ident)].append(float(rssi))
            except (TypeError, ValueError): pass

print(f"\n===== Résumé sur {a.duree} s =====")
print(f"Lignes reçues : {n}  (dont {non_json} non JSON)")
if n == 0:
    print("-> Connexion ouverte mais rien reçu : la station n'émet pas, ou il faut lui envoyer quelque chose d'abord (voir LISEZ_MOI.md du kit).")
print("Champs vus : " + ", ".join(f"{k} ({v})" for k, v in sorted(cles.items(), key=lambda x: -x[1])))
if ts:
    print(f"Champ temps : de {ts[0]} à {ts[-1]}" + ("  -> en millisecondes" if ts[0] > 1e11 else "  -> en secondes"))
else:
    print("Aucun champ temps (t / ts / time / timestamp) trouvé  <-- station.py ne pourra rien faire sans lui")
if silences:
    print(f"Plus long silence entre deux paquets TCP : {max(silences):.1f} s")
print("\nAppareils entendus :")
if not appareils:
    print("  aucun (champ identifiant ou rssi introuvable, voir les lignes brutes ci-dessus)")
for ident, v in sorted(appareils.items(), key=lambda x: -len(x[1])):
    print(f"  {ident:24} {len(v):4d} mesures · médiane {statistics.median(v):5.0f} dBm · min {min(v):4.0f} · max {max(v):4.0f}")
print("\nNoms attendus par station.py : korko-01 … korko-06. S'ils apparaissent sous une autre forme (adresse MAC…),")
print("lance station.py avec --alias <identifiant>=korko-01,<identifiant>=korko-02")
