#!/usr/bin/env python3
"""KORKO mini — compile et deploie contracts/KorkoEvents.sol sur Avalanche Fuji (testnet).

    python3 deploy_contrat.py

Prerequis : OPERATOR_PRIVATE_KEY dans .env (voir generer_compte.py), et ce compte doit
avoir un peu d'AVAX de testnet (faucet : https://core.app/tools/testnet-faucet/).

Le meme compte sert de deployeur ET d'operateur du contrat (celui qui a le droit d'appeler
demarrerSession/terminerSession/attribuerPoints). Ecrit contrat.json (adresse + ABI) a la
racine du projet : ce fichier n'est pas secret, il peut etre commit pour que toute l'equipe
utilise le meme contrat deploye.
"""
import json, os
import solcx
from web3 import Web3
from chaine import RPC_FUJI, CHAIN_ID_FUJI, FICHIER_CONTRAT, lire_env

ICI = os.path.dirname(os.path.abspath(__file__))
FICHIER_SOL = os.path.join(ICI, "contracts", "KorkoEvents.sol")
VERSION_SOLC = "0.8.24"


def compiler():
    installees = solcx.get_installed_solc_versions()
    if VERSION_SOLC not in [str(v) for v in installees]:
        print(f"Installation du compilateur Solidity {VERSION_SOLC}…")
        solcx.install_solc(VERSION_SOLC)
    with open(FICHIER_SOL) as f:
        source = f.read()
    sortie = solcx.compile_source(source, output_values=["abi", "bin"], solc_version=VERSION_SOLC)
    _, contrat = next(iter(sortie.items()))
    return contrat["abi"], contrat["bin"]


def main():
    env = lire_env()
    cle = env.get("OPERATOR_PRIVATE_KEY")
    if not cle:
        raise SystemExit("OPERATOR_PRIVATE_KEY absent de .env — lance d'abord generer_compte.py")

    w3 = Web3(Web3.HTTPProvider(RPC_FUJI))
    compte = w3.eth.account.from_key(cle)
    solde = w3.eth.get_balance(compte.address)
    print(f"Deployeur : {compte.address} · solde {w3.from_wei(solde, 'ether')} AVAX (testnet)")
    if solde == 0:
        raise SystemExit("Solde nul : finance ce compte via https://core.app/tools/testnet-faucet/ puis relance.")

    print("Compilation de contracts/KorkoEvents.sol…")
    abi, bytecode = compiler()

    Contrat = w3.eth.contract(abi=abi, bytecode=bytecode)
    tx = Contrat.constructor(compte.address).build_transaction({
        "from": compte.address,
        "nonce": w3.eth.get_transaction_count(compte.address, "pending"),
        "chainId": CHAIN_ID_FUJI,
        "gasPrice": w3.eth.gas_price,
    })
    signee = compte.sign_transaction(tx)
    h = w3.eth.send_raw_transaction(signee.raw_transaction)
    print(f"Transaction de deploiement envoyee : https://testnet.snowtrace.io/tx/{h.hex()}")
    recu = w3.eth.wait_for_transaction_receipt(h, timeout=120)

    with open(FICHIER_CONTRAT, "w") as f:
        json.dump({"reseau": "avalanche-fuji", "chain_id": CHAIN_ID_FUJI,
                   "adresse": recu.contractAddress, "abi": abi, "deployeur": compte.address}, f, indent=1)

    print(f"\nContrat deploye : {recu.contractAddress}")
    print(f"Explorateur     : https://testnet.snowtrace.io/address/{recu.contractAddress}")
    print(f"Ecrit dans {FICHIER_CONTRAT} (peut etre commit, ne contient aucun secret).")


if __name__ == "__main__":
    main()
