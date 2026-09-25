#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scorer_tout.py — passe un détecteur sur tous les scénarios, plusieurs graines,
avec et sans chaos, et résume en un tableau.

    python3 scorer_tout.py                  # ma_station
    python3 scorer_tout.py station_exemple  # la référence à battre
    python3 scorer_tout.py --graines 7,8,9,10

Un seul scénario sur une seule graine ne prouve rien : le bruit radio est
tiré au hasard. Ce banc rejoue tout, et n'affiche qu'une chose — est-ce que
le détecteur se trompe, et où.
"""

import argparse
import importlib
import io
import sys

from korko import _boucle, scorer
from korko_sim import Simulateur


def passer(classe, scenario, graine, chaos):
    d = classe()
    d._sortie = None                 # on ne veut pas des événements sur stdout
    d._journal = []
    sim = Simulateur(graine=graine, chaos=chaos)
    sim.charger_scenario(scenario)
    _boucle(d, sim.flux(duree=None), False, False)
    return scorer(d._journal, sim.verite)


def principal():
    p = argparse.ArgumentParser()
    p.add_argument("module", nargs="?", default="ma_station",
                   help="module contenant le détecteur (défaut : ma_station)")
    p.add_argument("--graines", default="7,8,9",
                   help="graines de bruit, séparées par des virgules")
    p.add_argument("--scenarios", default=None,
                   help="sous-ensemble de scénarios, séparés par des virgules")
    a = p.parse_args()

    graines = [int(x) for x in a.graines.split(",")]
    scenarios = a.scenarios.split(",") if a.scenarios else list(Simulateur.SCENARIOS)

    # station_exemple.py appelle lancer() au niveau module : on l'intercepte
    # pour récupérer la classe sans lancer sa propre boucle.
    import korko
    prises = []
    vrai_lancer = korko.lancer
    korko.lancer = lambda classe, argv=None: prises.append(classe)
    try:
        mod = importlib.import_module(a.module)
    finally:
        korko.lancer = vrai_lancer

    classe = prises[0] if prises else next(
        v for v in vars(mod).values()
        if isinstance(v, type) and v.__module__ == a.module
        and hasattr(v, "observation") and hasattr(v, "tic"))

    print("Détecteur : %s.%s" % (a.module, classe.__name__))
    print("Graines   : %s\n" % ", ".join(map(str, graines)))
    print("  %-11s %-7s %6s %6s %6s %6s %9s" %
          ("scénario", "mode", "justes", "fauxD", "fauxR", "manq", "lat.méd"))
    print("  " + "-" * 62)

    total = {"justes": 0, "fd": 0, "fr": 0, "manques": 0}
    for scenario in scenarios:
        for chaos in (False, True):
            j = fd = fr = m = 0
            lats = []
            for graine in graines:
                # le détecteur écrit ses diagnostics sur stderr : on les met de côté
                garde, sys.stderr = sys.stderr, io.StringIO()
                try:
                    s = passer(classe, scenario, graine, chaos)
                finally:
                    sys.stderr = garde
                j += s["justes"]
                fd += len(s["faux_departs"])
                fr += len(s["faux_retours"])
                m += len(s["manques"])
                if s["latence_mediane"] is not None:
                    lats.append(s["latence_mediane"])
            total["justes"] += j
            total["fd"] += fd
            total["fr"] += fr
            total["manques"] += m
            marque = "" if (fd == 0 and fr == 0 and m == 0) else "   <-- à corriger"
            print("  %-11s %-7s %6d %6d %6d %6d %8s%s" %
                  (scenario, "chaos" if chaos else "normal", j, fd, fr, m,
                   "%.0f s" % (sum(lats) / len(lats)) if lats else "—", marque))

    print("\n  TOTAL : %d justes · %d faux départs · %d faux retours · %d manqués"
          % (total["justes"], total["fd"], total["fr"], total["manques"]))
    return 0 if (total["fd"] == 0 and total["fr"] == 0 and total["manques"] == 0) else 1


if __name__ == "__main__":
    sys.exit(principal())
