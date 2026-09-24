# KORKO mini — version simple qui marche

Python 3, bibliothèque standard seulement. Un terminal par commande.

```
python3 sim.py --station A          # radio simulée : flux :8421, contrôle http://localhost:8081
python3 cloud.py --reset            # cloud : http://localhost:9001
python3 station.py --station A      # décide DEPART / RETOUR / ETRANGERE et pousse au cloud
```

Ports décalés (8421 / 8081 / 9001) pour tourner à côté du kit officiel (8420 / 8080 / 9000).

## L'interface web : http://localhost:9001/app

Une seule page pour tout faire :
- **Connexion** par numéro de téléphone (code à 4 chiffres, pré-rempli en démo car pas de vrai SMS)
  ou **Google via Privy** (wallet Avalanche Fuji créé automatiquement).
- **Mon surf** : choix du râtelier, « Je veux surfer », planche à prendre, chrono et prix en direct, reçu.
- **Mes messages** : les SMS simulés de ce compte.
- **Panneau démo** : les boutons du simulateur (ici / sable / 2m / loin / masque) pour chaque planche.

Le tableau exploitant reste sur http://localhost:9001/.

### Activer Privy

1. Sur dashboard.privy.io : créer une app, activer **Google** (et SMS si voulu), activer les **embedded wallets**
   (EVM), ajouter `http://localhost:9001` aux **allowed origins**.
2. Lancer le cloud avec l'App ID :
   `python3 cloud.py --privy-app-id <APP_ID>`
3. Option : ajouter `--privy-secret <APP_SECRET>` pour que le cloud vérifie chaque connexion auprès de Privy.
   Sans secret, le cloud fait confiance à ce que le navigateur envoie (acceptable en démo locale seulement).

Privy est chargé depuis esm.sh (internet nécessaire). Si le chargement échoue, la raison s'affiche sous le bouton
et la connexion par téléphone reste disponible.

## Sur la vraie maquette (Wi-Fi GL-SFT1200-3ae)

1. Diagnostic : `python3 diag.py --source 192.168.8.100:8420`
   (montre le format réel des messages et les appareils entendus)
2. Station : `python3 station.py --station A --source 192.168.8.100:8420 --verbose`
   `--verbose` affiche toutes les 5 s le signal lissé et l'état de chaque planche.
3. Si les balises arrivent sous forme d'adresse MAC :
   `--alias AA:BB:CC:DD:EE:01=korko-01,AA:BB:CC:DD:EE:02=korko-02`
4. Ferme le simulateur, calibre.py et diag.py avant de lancer la station : le Pi n'accepte peut-être qu'un client à la fois.

Stations : A = 192.168.8.100, B = .101, C = .102 (port 8420).

## Démo en 5 minutes

1. Ouvrir `http://localhost:9001/app`, se connecter avec un numéro, taper « Je veux surfer ».
2. Panneau démo : passer la planche indiquée sur **2m** ou **loin**, et au bout de 25 s le chrono démarre.
3. La mettre sur **sable** : pas de retour (signal trop faible).
4. Mettre une autre planche en **masque** : pas de faux départ.
5. Couper le cloud (Ctrl+C), remettre la planche sur **ici**, relancer `python3 cloud.py` : la station renvoie sa file et le reçu SMS arrive avec la bonne durée.
6. Mettre `korko-05` sur **ici** : alerte « à rapatrier ».

## Réglages (en tête de station.py)

`SEUIL_HAUT`, `SEUIL_BAS`, `DELAI_DEPART`, `DELAI_RETOUR`, `ALPHA`.
Règle en une phrase : « partie quand on ne l'entend plus fort depuis 25 s, revenue quand on l'entend fort sans interruption depuis 20 s ; les heures retenues sont celles du dernier/premier signal fort ».

## Blockchain (Avalanche Fuji, testnet)

Les débuts/fins de session et les points de gamification ("tubes") sont aussi enregistrés
sur la C-Chain d'Avalanche (testnet Fuji) via un petit smart contract (`contracts/KorkoEvents.sol`).
C'est la seule partie du projet qui sort de "bibliothèque standard seulement" (voir `requirements.txt`).

1. `pip install -r requirements.txt`
2. `python3 generer_compte.py` — crée un compte opérateur, écrit sa clé privée dans `.env`
   (jamais commit) et affiche son adresse publique.
3. Financer cette adresse en AVAX de testnet : https://core.app/tools/testnet-faucet/
4. `python3 deploy_contrat.py` — compile et déploie le contrat, écrit `contrat.json`
   (adresse + ABI, pas secret, peut être commit pour que toute l'équipe partage le même contrat).
5. Relancer `python3 cloud.py` : le tableau exploitant affiche un lien vers le contrat sur
   Snowtrace, et chaque DEPART/RETOUR/tube gagné devient une transaction (visible dans le
   terminal du cloud avec son lien `https://testnet.snowtrace.io/tx/...`).

Sans `.env`/`contrat.json`, `cloud.py` fonctionne normalement, juste sans écrire sur la chaîne.

Ce qu'un seul compte "opérateur" fait à la place de chaque usager (il signe et paie le gas
pour tout le monde) est une simplification : ce n'est pas encore de la vraie abstraction de
compte (ERC-4337). Le contrat garde une fonction `enregistrerWallet` prête pour le jour où
chaque usager aura son propre smart wallet.

## Limites connues (prochaines étapes)

- Pas de vraie mesure des faux départs et faux retours sur des traces : écrire `eval.py`.
- Pas de détection « toute la station devient sourde » (mode commun).
- Paiement, caution et SMS sont simulés. Pas de photo ni de barème des tubes.
- Tableau de bord brut, sans ordres de mission.
- Un seul compte opérateur signe tout : pas encore d'abstraction de compte par usager (ERC-4337).
