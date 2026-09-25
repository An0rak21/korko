#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lancer_sim.py — lance le simulateur du kit en déplaçant sa page de contrôle.

    python3 lancer_sim.py                # identique à korko_sim.py (page 8080)
    python3 lancer_sim.py --page 8099    # si 8080 est déjà pris

Le port 8080 est un grand classique : IPFS Desktop, Jenkins, Tomcat et bien
d'autres s'y installent. Quand il est occupé, korko_sim.py ne le dit pas —
sa page de contrôle est simplement injoignable, et c'est l'autre programme
qui répond à sa place.

On ne modifie pas korko_sim.py : c'est un fichier de l'organisateur, l'équipe
doit pouvoir le remplacer par la version d'origine à tout moment. On se
contente de repositionner la constante avant de démarrer.

Le port du flux (8420), lui, n'est pas déplaçable de l'extérieur : il est figé
comme valeur par défaut d'argument dans Diffuseur.__init__.
"""

import argparse

import korko_sim

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--page", type=int, default=korko_sim.PORT_PAGE,
                    help="port de la page de contrôle (défaut : %d)" % korko_sim.PORT_PAGE)
    a = ap.parse_args()
    korko_sim.PORT_PAGE = a.page
    korko_sim.principal()
