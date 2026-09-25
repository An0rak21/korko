#!/usr/bin/env python3
"""
korko_test.py — vérifie qu'une station KORKO fonctionne comme attendu.

Trois usages :

    python3 korko_test.py station-a.local
        Depuis un portable. Se branche sur le flux et contrôle tout :
        présence du flux, cadence, balises attendues, isolement.

    python3 korko_test.py --local A
        Sur le Pi lui-même, avant d'avoir installé le service.
        Scanne en direct et liste ce que la radio entend.

    python3 korko_test.py --trouver
        Balaie le réseau local et affiche l'adresse des stations, sans
        dépendre de la résolution des noms .local. C'est la commande à
        lancer après avoir connecté les Pi au wifi de la salle.

    python3 korko_test.py station-a.local --mesure korko-01
        Affiche la médiane glissante du RSSI d'une balise.
        C'est le mode pour calibrer : on se place à 20 cm, on note,
        on recule à 50 cm, on note, et ainsi de suite.

Bibliothèque standard uniquement, sauf pour --local qui a besoin de bleak.
"""

import argparse
import json
import socket
import statistics
import sys
import time
from collections import defaultdict

PORT = 8420
DUREE = 20          # secondes d'écoute par défaut
CADENCE_MIN = 0.8   # paquets par seconde et par balise, en dessous c'est mauvais
RSSI_PROCHE = -70   # une balise sur son propre râtelier doit être au dessus

STATIONS = {
    "A": {"korko-01", "korko-02"},
    "B": {"korko-03", "korko-04"},
    "C": {"korko-05", "korko-06"},
}

VERT, ROUGE, JAUNE, GRIS, RAZ = "\033[32m", "\033[31m", "\033[33m", "\033[90m", "\033[0m"
if not sys.stdout.isatty():
    VERT = ROUGE = JAUNE = GRIS = RAZ = ""

_echecs = []


def ok(texte):
    print("  %s✓%s %s" % (VERT, RAZ, texte))


def echec(texte):
    print("  %s✗%s %s" % (ROUGE, RAZ, texte))
    _echecs.append(texte)


def alerte(texte):
    print("  %s!%s %s" % (JAUNE, RAZ, texte))


def titre(texte):
    print("\n%s" % texte)
    print("-" * len(texte))


# --------------------------------------------------------------------------
# Écoute du flux
# --------------------------------------------------------------------------

def ecouter(hote, duree):
    """Se connecte au flux et collecte les observations pendant `duree`."""
    try:
        s = socket.create_connection((hote, PORT), timeout=8)
    except OSError as e:
        echec("connexion à %s:%d impossible — %s" % (hote, PORT, e))
        print("\n    Vérifiez que le Pi est allumé, sur le même réseau, et que")
        print("    le service tourne :  systemctl status korko-scan")
        sys.exit(1)

    ok("flux joignable sur %s:%d" % (hote, PORT))
    print("%s    écoute pendant %d secondes…%s" % (GRIS, duree, RAZ))

    s.settimeout(1.0)
    obs, tampon, fin = [], b"", time.monotonic() + duree
    while time.monotonic() < fin:
        try:
            bloc = s.recv(4096)
        except socket.timeout:
            continue
        if not bloc:
            break
        tampon += bloc
        *lignes, tampon = tampon.split(b"\n")
        for ligne in lignes:
            ligne = ligne.strip()
            if not ligne:
                continue
            try:
                d = json.loads(ligne)
            except ValueError:
                alerte("ligne illisible dans le flux : %r" % ligne[:60])
                continue
            if "rssi" in d:
                obs.append(d)
    s.close()
    return obs


def analyser(obs, duree, station_attendue=None):
    if not obs:
        echec("aucune observation reçue — la radio n'entend rien, "
              "ou le plancher est trop haut")
        return

    ok("%d observations reçues" % len(obs))

    champs = set(obs[0])
    if champs >= {"t", "station", "balise", "rssi"}:
        ok("format du contrat respecté")
    else:
        echec("champs manquants dans le flux : %s"
              % ", ".join({"t", "station", "balise", "rssi"} - champs))

    stations = {o["station"] for o in obs}
    if len(stations) == 1:
        nom = stations.pop()
        if station_attendue and nom != station_attendue:
            echec("la station se déclare « %s » au lieu de « %s »"
                  % (nom, station_attendue))
        else:
            ok("la station signe ses observations « %s »" % nom)
    else:
        echec("plusieurs identifiants de station dans le même flux : %s"
              % ", ".join(sorted(stations)))
        nom = None

    par_balise = defaultdict(list)
    for o in obs:
        par_balise[o["balise"]].append(o["rssi"])

    titre("Balises entendues")
    print("  %-14s %8s %8s %8s %10s" % ("balise", "paquets", "médiane",
                                        "écart", "cadence"))
    attendues = STATIONS.get(nom or "", set())
    for balise in sorted(par_balise):
        v = par_balise[balise]
        med = statistics.median(v)
        ecart = statistics.pstdev(v) if len(v) > 1 else 0.0
        cadence = len(v) / duree
        marque = "" if balise in attendues else "  %s← pas de cette station%s" % (JAUNE, RAZ)
        print("  %-14s %8d %8.0f %8.1f %8.2f/s%s"
              % (balise, len(v), med, ecart, cadence, marque))

    if attendues:
        titre("Ses planches à elle")
        for balise in sorted(attendues):
            if balise not in par_balise:
                echec("%s n'est pas entendue — balise éteinte, mal nommée, "
                      "ou hors plancher" % balise)
                continue
            med = statistics.median(par_balise[balise])
            cadence = len(par_balise[balise]) / duree
            if med < RSSI_PROCHE:
                alerte("%s entendue à %.0f dBm : plus faible qu'attendu pour "
                       "une planche sur son râtelier" % (balise, med))
            else:
                ok("%s à %.0f dBm" % (balise, med))
            if cadence < CADENCE_MIN:
                alerte("%s : %.2f paquet/s, c'est peu — intervalle d'émission "
                       "trop long, ou 2,4 GHz saturé" % (balise, cadence))

        intruses = set(par_balise) - attendues
        if intruses:
            alerte("balises d'ailleurs entendues : %s" % ", ".join(sorted(intruses)))
            print("    En salle, baissez le plancher de la station "
                  "(--plancher -50) pour les faire disparaître.")
            print("    Sur la plage, c'est normal et c'est même le cas à traiter.")
        else:
            ok("aucune balise étrangère : l'isolement est bon")

    titre("Repère de calibration")
    tous = [o["rssi"] for o in obs]
    print("  médiane générale : %.0f dBm" % statistics.median(tous))
    print("  minimum / maximum : %d / %d dBm" % (min(tous), max(tous)))
    print("%s  Reportez la puissance mesurée à 1 m dans RSSI_1M, "
          "en tête de korko_sim.py.%s" % (GRIS, RAZ))


# --------------------------------------------------------------------------
# Mode mesure — pour calibrer à la main
# --------------------------------------------------------------------------

def mesurer(hote, balise):
    print("Mesure de %s. Déplacez-vous, la médiane suit. Ctrl-C pour arrêter.\n"
          % balise)
    try:
        s = socket.create_connection((hote, PORT), timeout=8)
    except OSError as e:
        echec("connexion impossible — %s" % e)
        sys.exit(1)
    s.settimeout(1.0)
    fenetre, tampon = [], b""
    try:
        while True:
            try:
                bloc = s.recv(4096)
            except socket.timeout:
                continue
            if not bloc:
                break
            tampon += bloc
            *lignes, tampon = tampon.split(b"\n")
            for ligne in lignes:
                if not ligne.strip():
                    continue
                d = json.loads(ligne)
                if d.get("balise") != balise:
                    continue
                fenetre.append(d["rssi"])
                del fenetre[:-15]
                if len(fenetre) >= 3:
                    med = statistics.median(fenetre)
                    barre = "█" * max(0, int((med + 100) / 2))
                    print("\r  %4.0f dBm  (%2d mesures)  %-25s"
                          % (med, len(fenetre), barre), end="", flush=True)
    except KeyboardInterrupt:
        print("\n")
    finally:
        s.close()


# --------------------------------------------------------------------------
# Mode local — sur le Pi, sans le service
# --------------------------------------------------------------------------

def local(station, duree):
    try:
        import asyncio
        from bleak import BleakScanner
    except ImportError:
        echec("bleak n'est pas installé — lancez install_station.sh d'abord")
        sys.exit(1)

    vus = defaultdict(list)

    def rappel(appareil, donnees):
        if donnees.rssi is not None:
            vus[(appareil.address, appareil.name or "")].append(donnees.rssi)

    async def marche():
        scan = BleakScanner(detection_callback=rappel, scanning_mode="active")
        await scan.start()
        await asyncio.sleep(duree)
        await scan.stop()

    print("Scan direct de la radio pendant %d secondes…\n" % duree)
    asyncio.run(marche())

    if not vus:
        echec("la radio n'entend rien du tout — vérifiez que le Bluetooth "
              "est actif (rfkill list) et que les balises sont alimentées")
        return

    titre("Tout ce que la radio entend")
    print("  %-20s %-18s %7s %8s" % ("adresse", "nom", "paquets", "médiane"))
    for (mac, nom), v in sorted(vus.items(), key=lambda x: -statistics.median(x[1])):
        print("  %-20s %-18s %7d %8.0f"
              % (mac, nom[:18], len(v), statistics.median(v)))
    print("\n%s  Reportez les adresses des balises de la station %s dans le"
          "\n  dictionnaire PLANCHES de korko_scan.py.%s" % (GRIS, station, RAZ))


# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# Mode trouver — balaie le réseau à la recherche des stations
# --------------------------------------------------------------------------

def _sonder(ip, resultats):
    """Teste le port du flux et lit une ligne, pour identifier la station."""
    try:
        s = socket.create_connection((ip, PORT), timeout=1.2)
    except OSError:
        return
    s.settimeout(3.0)
    tampon = b""
    nom = None
    try:
        while len(tampon) < 400 and b"\n" not in tampon:
            bloc = s.recv(256)
            if not bloc:
                break
            tampon += bloc
        for ligne in tampon.split(b"\n"):
            if not ligne.strip():
                continue
            try:
                nom = json.loads(ligne).get("station")
                break
            except ValueError:
                continue
    except (OSError, socket.timeout):
        pass
    finally:
        s.close()
    resultats.append((ip, nom))


def trouver():
    """Balaie le /24 local à la recherche de ce qui répond sur le port 8420."""
    import threading

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        moi = s.getsockname()[0]
    except OSError:
        moi = socket.gethostbyname(socket.gethostname())
    finally:
        s.close()

    base = moi.rsplit(".", 1)[0]
    titre("Recherche de stations sur %s.0/24" % base)
    print("%s    depuis %s, environ dix secondes…%s" % (GRIS, moi, RAZ))

    resultats, fils = [], []
    for n in range(1, 255):
        f = threading.Thread(target=_sonder, args=("%s.%d" % (base, n), resultats))
        f.daemon = True
        f.start()
        fils.append(f)
    for f in fils:
        f.join(timeout=6)

    if not resultats:
        echec("aucune station trouvée sur ce réseau")
        print("\n    Le Pi est-il bien connecté au même wifi que ce portable ?")
        print("    Si oui, le réseau isole peut-être ses clients les uns des")
        print("    autres — c'est fréquent sur un wifi public. Dans ce cas il")
        print("    faut un point d'accès à soi, ou un switch.")
        return

    for ip, nom in sorted(resultats):
        if nom:
            ok("station %s à l'adresse %s" % (nom, ip))
            print("      python3 korko_test.py %s" % ip)
        else:
            alerte("quelque chose répond sur %s, sans se déclarer" % ip)


def principal():
    p = argparse.ArgumentParser(description="Vérification d'une station KORKO.")
    p.add_argument("hote", nargs="?", default="localhost",
                   help="nom ou adresse du Pi (ex. station-a.local)")
    p.add_argument("--local", metavar="STATION",
                   help="scan direct sur le Pi, sans passer par le service")
    p.add_argument("--mesure", metavar="BALISE",
                   help="suit le RSSI d'une balise, pour calibrer")
    p.add_argument("--duree", type=int, default=DUREE)
    p.add_argument("--station", help="identifiant attendu (A, B ou C)")
    p.add_argument("--trouver", action="store_true",
                   help="cherche les stations sur le réseau local")
    a = p.parse_args()

    if a.trouver:
        trouver()
    elif a.local:
        local(a.local, a.duree)
    elif a.mesure:
        mesurer(a.hote, a.mesure)
    else:
        attendue = a.station
        if not attendue and a.hote.startswith("station-"):
            attendue = a.hote[8:9].upper()
        titre("Station %s" % a.hote)
        obs = ecouter(a.hote, a.duree)
        analyser(obs, a.duree, attendue)

    print()
    if _echecs:
        print("%s%d problème(s) à régler.%s" % (ROUGE, len(_echecs), RAZ))
        sys.exit(1)
    print("%sStation conforme.%s" % (VERT, RAZ))


if __name__ == "__main__":
    principal()
