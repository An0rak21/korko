#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ma_station.py — le détecteur KORKO.

    python3 ma_station.py --sim --scenario journee
    python3 ma_station.py --sim --scenario sable --chaos
    python3 ma_station.py --source localhost:8420

La règle, en une phrase pour l'exploitant :

    Une planche est PARTIE quand elle s'est tue durablement APRÈS s'être
    affaiblie ; si elle se tait d'un coup alors qu'on l'entendait fort,
    c'est sa pile qui est morte, pas la planche qui est partie.
    Elle est REVENUE quand on l'entend de nouveau fort, sans interruption.

Pourquoi le signal faible ne suffit pas à décider d'un départ : une planche
posée sur le sable à neuf mètres est reçue vers -87 dBm, et un corps mouillé
devant la balise la fait tomber vers -85 dBm. Les deux sont indiscernables
d'un seuil, et dans les deux cas la planche est encore là. Ce qui distingue
vraiment un départ, c'est que la planche partie au large passe sous le
plancher de réception : elle n'émet plus rien du tout.
"""

import json
import os
import statistics
import sys
import urllib.request
from collections import deque

from korko import Detecteur, lancer, planches_de

# La console Windows est en cp1252 : sans ça, un diagnostic accentué fait
# tomber la station en pleine décision.
for _flux in (sys.stdout, sys.stderr):
    try:
        _flux.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

#: mettre KORKO_CLOUD="" désactive l'envoi (utile pour scorer vite)
CLOUD = os.environ.get("KORKO_CLOUD", "http://localhost:9000/evenements")

SILENCE_DEPART = 45.0   # s sans le moindre paquet avant de conclure au départ
FENETRE = 12.0          # s : largeur de la médiane glissante
PROCHE = -78            # dBm : au-dessus, la planche est au râtelier
LOIN = -84              # dBm : en dessous, elle n'y est pas (zone morte entre les deux)
CONFIRME = 8.0          # s de signal fort continu avant d'annoncer un retour
PILE = -80              # derniers paquets plus forts que ça avant le silence : c'est la pile
DERNIERS = 5            # nombre de paquets regardés pour ce diagnostic

# SILENCE_DEPART tient compte des deux silences légitimes : une balise
# mourante n'émet plus que toutes les 6 s, et --chaos coupe le réseau
# pendant 12 s. 45 s laisse de la marge sur les deux, et reste très loin
# des 300 s de tolérance du scoreur.


class Planche:
    def __init__(self):
        self.fenetre = deque()                  # (t, rssi) des FENETRE dernières secondes
        self.derniers = deque(maxlen=DERNIERS)  # derniers rssi reçus, pour juger la pile
        self.vue = None                         # t du dernier paquet reçu
        self.presente = None                    # None = jamais entendue
        self.forte_depuis = None                # début de la série continue au-dessus de PROCHE
        self.pile_signalee = False

    def mediane(self, t):
        limite = t - FENETRE
        while self.fenetre and self.fenetre[0][0] < limite:
            self.fenetre.popleft()
        return statistics.median([r for _, r in self.fenetre]) if self.fenetre else None


class MaStation(Detecteur):

    PERIODE_TIC = 1.0

    def __init__(self):
        self.planches = {}
        self.station = "A"
        self.journal = []      # événements que le cloud n'a pas encore reçus
        self.cloud_ok = None
        if CLOUD:
            print("ma_station : décisions envoyées à %s" % CLOUD, file=sys.stderr)

    # -- un paquet radio arrive -------------------------------------------
    def observation(self, o):
        self.station = o.station
        p = self.planches.setdefault(o.balise, Planche())
        p.fenetre.append((o.t, o.rssi))
        p.derniers.append(o.rssi)
        p.vue = o.t
        p.pile_signalee = False                  # elle réémet : sa pile va bien

        med = p.mediane(o.t)
        if med is not None:
            if med >= PROCHE:
                if p.forte_depuis is None:
                    p.forte_depuis = o.t
            elif med <= LOIN:
                p.forte_depuis = None            # la série de retour doit être continue

        chez_elle = o.balise in planches_de(self.station)
        if p.presente is None:
            # Au démarrage, mes planches sont supposées rangées : les entendre
            # n'est pas un retour. Une planche d'ailleurs, elle, n'a rien à
            # faire ici : l'entendre est un événement.
            p.presente = chez_elle
            if chez_elle:
                p.forte_depuis = None
                return

        if not p.presente and p.forte_depuis is not None \
                and o.t - p.forte_depuis >= CONFIRME:
            p.presente = True
            p.forte_depuis = None
            self.signaler("RETOUR" if chez_elle else "ETRANGERE", o.balise, o.t)

    # -- appelée même quand plus rien n'arrive ----------------------------
    def tic(self, t):
        for balise, p in self.planches.items():
            if not p.presente or p.vue is None or t - p.vue < SILENCE_DEPART:
                continue

            if balise not in planches_de(self.station):
                p.presente = False               # une étrangère repart : rien à annoncer
                p.forte_depuis = None
                continue

            forte_avant = statistics.median(p.derniers) if p.derniers else None
            if forte_avant is not None and forte_avant > PILE:
                # Elle s'est taue d'un coup en plein signal fort : personne ne
                # l'a emportée, c'est la balise qui ne répond plus.
                if not p.pile_signalee:
                    p.pile_signalee = True
                    print("ma_station : %s muette au râtelier à %.0f dBm — pile ou balise HS, "
                          "ce n'est pas un départ" % (balise, forte_avant), file=sys.stderr)
                continue

            p.presente = False
            p.forte_depuis = None
            self.signaler("DEPART", balise, p.vue)   # daté du dernier signe de vie

        if self.vider():                             # cloud à jour : il peut avancer
            self.envoyer({"t": t, "station": self.station, "evenement": "TIC"})

    # -- sortie -----------------------------------------------------------
    def signaler(self, type_, balise, t):
        {"DEPART": self.depart, "RETOUR": self.retour,
         "ETRANGERE": self.etrangere}[type_](balise, t, self.station)
        self.journal.append({"t": t, "station": self.station,
                             "balise": balise, "evenement": type_})
        self.vider()

    # -- si le réseau tombe : rien ne se perd -----------------------------
    def vider(self):
        """Envoie le journal dans l'ordre ; s'arrête au premier échec."""
        while self.journal:
            if not self.envoyer(self.journal[0]):
                return False                     # on réessaiera au prochain tic
            self.journal.pop(0)
        return True

    def envoyer(self, evenement):
        if not CLOUD:
            return True
        try:
            urllib.request.urlopen(
                urllib.request.Request(
                    CLOUD, json.dumps(evenement).encode("utf-8"),
                    {"Content-Type": "application/json"}), timeout=0.5)
            ok = True
        except Exception:
            ok = False
        if ok != self.cloud_ok:                  # on ne prévient qu'au changement
            print("ma_station : cloud %s" % ("joint" if ok else
                  "injoignable, les événements sont gardés au journal"),
                  file=sys.stderr)
            self.cloud_ok = ok
        return ok


if __name__ == "__main__":
    lancer(MaStation)
