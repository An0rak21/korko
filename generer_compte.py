#!/usr/bin/env python3
"""KORKO mini — genere un compte Avalanche de test (operateur) pour l'equipe.

    python3 generer_compte.py

Cree une paire de cles et ajoute OPERATOR_PRIVATE_KEY a .env (jamais commit, voir
.gitignore). Affiche seulement l'adresse publique : va la financer en AVAX de testnet sur
https://core.app/tools/testnet-faucet/ avant de lancer deploy_contrat.py.

Ne relance ce script que si tu veux un NOUVEAU compte : sinon .env garde deja le tien.
"""
import os
from eth_account import Account

ICI = os.path.dirname(os.path.abspath(__file__))
FICHIER_ENV = os.path.join(ICI, ".env")

if __name__ == "__main__":
    if os.path.exists(FICHIER_ENV):
        with open(FICHIER_ENV) as f:
            if "OPERATOR_PRIVATE_KEY=" in f.read():
                raise SystemExit(".env contient deja OPERATOR_PRIVATE_KEY. Supprime cette ligne "
                                  "d'abord si tu veux vraiment en regenerer un nouveau.")
    compte = Account.create()
    with open(FICHIER_ENV, "a") as f:
        f.write(f"OPERATOR_PRIVATE_KEY={compte.key.hex()}\n")
    print(f"Compte cree : {compte.address}")
    print("Cle privee ecrite dans .env (ne JAMAIS la commit, .gitignore l'exclut deja).")
    print("Prochaine etape : la financer via https://core.app/tools/testnet-faucet/ puis lancer deploy_contrat.py")
