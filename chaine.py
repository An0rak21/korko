#!/usr/bin/env python3
"""KORKO mini — enregistrement des evenements sur Avalanche C-Chain (testnet Fuji).

Un seul compte "operateur" (cle privee dans .env, jamais commit) signe et paie le gas pour
tous les usagers : ils n'ont rien a signer, rien a payer. C'est un relais de confiance, pas
encore de la vraie abstraction de compte (ERC-4337) — voir contracts/KorkoEvents.sol pour
le point d'extension prevu (enregistrerWallet).

    python3 deploy_contrat.py         # a lancer une fois : compile + deploie, ecrit contrat.json

Ensuite cloud.py importe ce module (objet `chaine`) et appelle demarrer_session() /
terminer_session() / attribuer_points(). Chaque appel est mis en file et envoye par un
thread de fond avec retry, pour ne jamais faire attendre une requete HTTP le temps qu'un
bloc soit mine (meme principe que la file d'evenements de station.py).

Si OPERATOR_PRIVATE_KEY ou contrat.json est absent, la chaine est simplement desactivee
(cloud.py continue de fonctionner sans blockchain).
"""
import json, os, queue, threading, time
from web3 import Web3

RPC_FUJI = "https://api.avax-test.network/ext/bc/C/rpc"
CHAIN_ID_FUJI = 43113
ADRESSE_ZERO = "0x0000000000000000000000000000000000000000"
ICI = os.path.dirname(os.path.abspath(__file__))
FICHIER_CONTRAT = os.path.join(ICI, "contrat.json")


def lire_env():
    """Petit lecteur .env maison (pas de dependance supplementaire) : KEY=VALUE par ligne."""
    valeurs = dict(os.environ)
    chemin = os.path.join(ICI, ".env")
    if os.path.exists(chemin):
        with open(chemin) as f:
            for ligne in f:
                ligne = ligne.strip()
                if ligne and not ligne.startswith("#") and "=" in ligne:
                    k, v = ligne.split("=", 1)
                    valeurs.setdefault(k.strip(), v.strip())
    return valeurs


class Chaine:
    """Client vers KorkoEvents.sol. Toutes les ecritures passent par une file + thread de fond."""

    def __init__(self, rpc=RPC_FUJI, chain_id=CHAIN_ID_FUJI):
        self.actif = False
        self.file = queue.Queue()
        self.chain_id = chain_id
        env = lire_env()
        cle_operateur = env.get("OPERATOR_PRIVATE_KEY")
        if not cle_operateur:
            print("[chaine] OPERATOR_PRIVATE_KEY absent (.env) : enregistrement blockchain desactive")
            return
        if not os.path.exists(FICHIER_CONTRAT):
            print("[chaine] contrat.json absent : lance d'abord `python3 deploy_contrat.py`")
            return
        with open(FICHIER_CONTRAT) as f:
            info = json.load(f)
        self.w3 = Web3(Web3.HTTPProvider(rpc))
        self.compte = self.w3.eth.account.from_key(cle_operateur)
        self.contrat = self.w3.eth.contract(address=info["adresse"], abi=info["abi"])
        self.actif = True
        print(f"[chaine] active · operateur {self.compte.address} · contrat {info['adresse']}")
        threading.Thread(target=self._boucle_envoi, daemon=True).start()

    @staticmethod
    def id_usager(cle_client):
        """bytes32 stable a partir de la cle client (numero de tel ou 'privy:did:...')."""
        return Web3.keccak(text=cle_client)

    def _empiler(self, nom_fonction, args):
        if self.actif:
            self.file.put((nom_fonction, args))

    def demarrer_session(self, session_id, cle_client, wallet, station, balise):
        self._empiler("demarrerSession",
                      (session_id, self.id_usager(cle_client), wallet or ADRESSE_ZERO, station, balise))

    def terminer_session(self, session_id, duree_secondes, prix_centimes):
        self._empiler("terminerSession", (session_id, int(duree_secondes), int(prix_centimes)))

    def attribuer_points(self, cle_client, wallet, points, raison):
        self._empiler("attribuerPoints",
                      (self.id_usager(cle_client), wallet or ADRESSE_ZERO, int(points), raison))

    def enregistrer_wallet(self, cle_client, wallet):
        """A appeler quand un wallet (embedded Privy ou futur smart account) est cree pour un usager."""
        if wallet:
            self._empiler("enregistrerWallet", (self.id_usager(cle_client), wallet))

    # ---------- envoi ----------
    def _boucle_envoi(self):
        while True:
            nom_fonction, args = self.file.get()
            for tentative in range(5):
                try:
                    self._envoyer(nom_fonction, args)
                    break
                except Exception as e:
                    print(f"[chaine] echec {nom_fonction}{args} (essai {tentative + 1}/5) : {e}")
                    time.sleep(3)
            else:
                print(f"[chaine] abandon apres 5 essais : {nom_fonction}{args}")

    def _envoyer(self, nom_fonction, args):
        fonction = getattr(self.contrat.functions, nom_fonction)(*args)
        tx = fonction.build_transaction({
            "from": self.compte.address,
            "nonce": self.w3.eth.get_transaction_count(self.compte.address, "pending"),
            "chainId": self.chain_id,
            "gasPrice": self.w3.eth.gas_price,
        })
        signee = self.compte.sign_transaction(tx)
        h = self.w3.eth.send_raw_transaction(signee.raw_transaction)
        recu = self.w3.eth.wait_for_transaction_receipt(h, timeout=90)
        print(f"[chaine] {nom_fonction} -> https://testnet.snowtrace.io/tx/{h.hex()} (bloc {recu.blockNumber})")


chaine = Chaine()
