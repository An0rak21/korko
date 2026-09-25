"""
korko.py — la couche commune du kit hackathon KORKO.

Aucune dépendance : bibliothèque standard uniquement.
Fonctionne à l'identique sur macOS, Windows, Linux et Raspberry Pi.

Le contrat, une ligne JSON par observation :

    {"t": 1725873012.412, "station": "A", "balise": "korko-07", "rssi": -71}

Votre code décide, et émet :

    {"t": ..., "station": "A", "balise": "korko-07", "evenement": "DEPART"}

RÈGLE UNIQUE : ne lisez jamais time.time(). Utilisez le champ `t` du flux.
C'est ce qui permet de rejouer une journée en trente secondes, et de basculer
sur un Raspberry Pi réel sans changer une ligne.
"""

import argparse
import json
import socket
import statistics
import sys
import time
from collections import namedtuple

VERSION = "1.1"
PORT_DEFAUT = 8420

#: à quelle station appartient chaque planche. C'est une table de
#: configuration, pas une déduction faite sur le signal : une planche
#: appartient à un râtelier et doit y revenir.
#:
#: Six balises, trois stations, deux planches chacune. Le numéro dit à quel
#: râtelier la planche appartient. Le simulateur reproduit la station A :
#: ses deux planches, et les quatre autres qu'elle ne devrait jamais entendre.
STATIONS = {
    "A": {"korko-01", "korko-02"},
    "B": {"korko-03", "korko-04"},
    "C": {"korko-05", "korko-06"},
}


def planches_de(station):
    """Les planches rangées à cette station."""
    return STATIONS.get(station, set())


#: raccourci pour la station A, celle que simule korko_sim.py
MES_PLANCHES = STATIONS["A"]

Observation = namedtuple("Observation", "t station balise rssi")


# --------------------------------------------------------------------------
# La classe à hériter
# --------------------------------------------------------------------------

class Detecteur:
    """Hérite de cette classe, implémente observation() et tic()."""

    #: intervalle (secondes de flux) entre deux appels à tic()
    PERIODE_TIC = 1.0

    def observation(self, o):
        """Appelée à chaque paquet reçu. `o` est une Observation."""

    def tic(self, t):
        """Appelée régulièrement, MÊME quand plus aucun paquet n'arrive.

        C'est ici que se détectent les silences : une planche partie
        n'émet plus rien vers la station, donc observation() ne sera
        jamais rappelée pour elle.
        """

    # -- à appeler depuis ton code -----------------------------------------

    def depart(self, balise, t, station="A"):
        self._emettre(t, station, balise, "DEPART")

    def retour(self, balise, t, station="A"):
        self._emettre(t, station, balise, "RETOUR")

    def etrangere(self, balise, t, station="A"):
        """Une planche d'une autre station vient d'être raccrochée ici."""
        self._emettre(t, station, balise, "ETRANGERE")

    # -- plomberie ---------------------------------------------------------

    _sortie = None
    _journal = None

    def _emettre(self, t, station, balise, evenement):
        e = {"t": round(t, 3), "station": station,
             "balise": balise, "evenement": evenement}
        if self._journal is not None:
            self._journal.append(e)
        if self._sortie is not None:
            self._sortie.write(json.dumps(e) + "\n")
            self._sortie.flush()


# --------------------------------------------------------------------------
# Les sources
# --------------------------------------------------------------------------

def source_fichier(chemin):
    """Rejoue un fichier de traces au format contrat."""
    with open(chemin, encoding="utf-8") as f:
        for ligne in f:
            ligne = ligne.strip()
            if not ligne:
                continue
            d = json.loads(ligne)
            if "rssi" in d:
                yield Observation(d["t"], d.get("station", "A"),
                                  d["balise"], d["rssi"])


def source_reseau(adresse, timeout=0.25):
    """Se connecte à un simulateur ou à un Pi, et lit le flux.

    Émet None quand rien n'arrive : c'est ce qui permet à la boucle
    principale de continuer à appeler tic() pendant les silences.
    """
    hote, _, port = adresse.partition(":")
    port = int(port or PORT_DEFAUT)
    s = socket.create_connection((hote, port), timeout=5)
    s.settimeout(timeout)
    tampon = b""
    try:
        while True:
            try:
                bloc = s.recv(4096)
            except socket.timeout:
                yield None
                continue
            if not bloc:
                return
            tampon += bloc
            *lignes, tampon = tampon.split(b"\n")
            for ligne in lignes:
                ligne = ligne.strip()
                if not ligne:
                    continue
                try:
                    d = json.loads(ligne)
                except ValueError:
                    continue
                if "rssi" in d:
                    yield Observation(d["t"], d.get("station", "A"),
                                      d["balise"], d["rssi"])
    finally:
        s.close()


# --------------------------------------------------------------------------
# Le scoreur
# --------------------------------------------------------------------------

#: un DEPART prédit est juste s'il tombe entre l'instant réel et +5 minutes
TOLERANCE_DEPART = 300.0
TOLERANCE_RETOUR = 120.0
TOLERANCES = {"DEPART": TOLERANCE_DEPART}


def scorer(predits, verite):
    """Compare les décisions à la vérité terrain.

    Retourne un dictionnaire : justes, faux, manques, latences.
    """
    restants = [dict(e) for e in verite]
    for e in restants:
        e["pris"] = False

    justes, faux, latences = 0, [], []

    for p in predits:
        tol = TOLERANCES.get(p["evenement"], TOLERANCE_RETOUR)
        candidat = None
        for v in restants:
            if v["pris"] or v["balise"] != p["balise"]:
                continue
            if v["evenement"] != p["evenement"]:
                continue
            if v["t"] - 5.0 <= p["t"] <= v["t"] + tol:
                if candidat is None or v["t"] > candidat["t"]:
                    candidat = v
        if candidat is None:
            faux.append(p)
        else:
            candidat["pris"] = True
            justes += 1
            latences.append(max(0.0, p["t"] - candidat["t"]))

    manques = [v for v in restants if not v["pris"]]
    return {
        "justes": justes,
        "faux_departs": [f for f in faux if f["evenement"] == "DEPART"],
        "faux_retours": [f for f in faux if f["evenement"] != "DEPART"],
        "manques": manques,
        "latence_mediane": statistics.median(latences) if latences else None,
        "latence_max": max(latences) if latences else None,
    }


def afficher_score(s):
    print("", file=sys.stderr)
    print("  RÉSULTAT", file=sys.stderr)
    print("  --------", file=sys.stderr)
    print(f"  détections justes ....... {s['justes']}", file=sys.stderr)
    print(f"  faux départs ............ {len(s['faux_departs'])}", file=sys.stderr)
    print(f"  faux retours ............ {len(s['faux_retours'])}", file=sys.stderr)
    print(f"  événements manqués ...... {len(s['manques'])}", file=sys.stderr)
    if s["latence_mediane"] is not None:
        print(f"  latence médiane ......... {s['latence_mediane']:.1f} s",
              file=sys.stderr)
        print(f"  latence maximale ........ {s['latence_max']:.1f} s",
              file=sys.stderr)
    faux = s["faux_departs"] + s["faux_retours"]
    faux.sort(key=lambda e: e["t"])
    for f in faux[:6]:
        print(f"    ! faux {f['evenement'].lower()} : {f['balise']} "
              f"à t={f['t']:.0f}", file=sys.stderr)
    if len(faux) > 6:
        print(f"    ! … et {len(faux) - 6} autres", file=sys.stderr)
    for m in s["manques"][:6]:
        print(f"    ? manqué : {m['evenement'].lower()} de {m['balise']} "
              f"à t={m['t']:.0f}", file=sys.stderr)
    if len(s["manques"]) > 6:
        print(f"    ? … et {len(s['manques']) - 6} autres", file=sys.stderr)
    print("", file=sys.stderr)


# --------------------------------------------------------------------------
# La boucle
# --------------------------------------------------------------------------

def _boucle(detecteur, flux, temps_reel, verbeux):
    """Consomme un flux d'Observations (ou None) et cadence les tics."""
    prochain_tic = None
    dernier_t = None
    depart_mur = time.monotonic()

    for o in flux:
        if o is None:
            if dernier_t is None:
                continue
            if temps_reel:
                t = dernier_t + (time.monotonic() - depart_mur)
            else:
                continue
        else:
            t = o.t
            dernier_t = o.t
            depart_mur = time.monotonic()

        if prochain_tic is None:
            prochain_tic = t

        while t >= prochain_tic:
            detecteur.tic(prochain_tic)
            prochain_tic += detecteur.PERIODE_TIC

        if o is not None:
            if verbeux:
                print(f"    {o.t:9.1f}  {o.balise}  {o.rssi:4d} dBm",
                      file=sys.stderr)
            detecteur.observation(o)


def lancer(classe, argv=None):
    """Point d'entrée standard. Met `lancer(MonAlgo)` en bas de ton fichier."""
    p = argparse.ArgumentParser(
        description="Détecteur KORKO — départs et retours de planches.")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--sim", action="store_true",
                   help="simulateur intégré (avec score en fin de course)")
    g.add_argument("--rejeu", metavar="FICHIER",
                   help="rejoue un fichier de traces")
    g.add_argument("--source", metavar="HOTE:PORT",
                   help="se connecte à un simulateur ou à un Pi")
    p.add_argument("--scenario", default="journee",
                   help="scénario du simulateur (défaut : journee)")
    p.add_argument("--duree", type=float, default=None,
                   help="durée simulée en secondes")
    p.add_argument("--graine", type=int, default=7,
                   help="graine du bruit, pour des runs reproductibles")
    p.add_argument("--chaos", action="store_true",
                   help="ajoute des pertes de paquets et des trous réseau")
    p.add_argument("-v", "--verbeux", action="store_true",
                   help="affiche chaque paquet reçu")
    a = p.parse_args(argv)

    d = classe()
    d._sortie = sys.stdout
    d._journal = []
    verite = None

    if a.rejeu:
        _boucle(d, source_fichier(a.rejeu), False, a.verbeux)
        import os
        sidecar = os.path.splitext(a.rejeu)[0] + ".verite.json"
        if os.path.exists(sidecar):
            with open(sidecar, encoding="utf-8") as f:
                verite = json.load(f)

    elif a.source:
        _boucle(d, source_reseau(a.source), True, a.verbeux)

    else:
        from korko_sim import Simulateur
        sim = Simulateur(graine=a.graine, chaos=a.chaos)
        sim.charger_scenario(a.scenario)
        _boucle(d, sim.flux(duree=a.duree), False, a.verbeux)
        verite = sim.verite

    if verite is not None:
        afficher_score(scorer(d._journal, verite))
    return d
