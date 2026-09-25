# KORKO — notre rendu du défi

Python 3, bibliothèque standard seulement (sauf la partie blockchain, voir plus bas).
Un terminal par commande.

```
python3 korko_sim.py                  # simulateur du kit : flux :8420, page de contrôle :8080
python3 mon_cloud.py                  # cloud : http://localhost:9000
python3 ma_station.py --source localhost:8420
```

Si le port 8080 est déjà pris chez toi — IPFS Desktop, Jenkins, Tomcat s'y installent
volontiers — le simulateur ne le dit pas : sa page de contrôle est simplement injoignable et
c'est l'autre programme qui répond à sa place. Dans ce cas :

```
python3 lancer_sim.py --page 8099
python3 mon_cloud.py --sim http://localhost:8099
```

## Nos fichiers, et ceux du kit

Du kit de l'organisateur, inchangés : `korko.py`, `korko_sim.py`, `korko_test.py`,
`station_exemple.py`, `cloud_exemple.py`. Ne les modifiez pas — on doit pouvoir les
remplacer par la version d'origine à tout moment.

Les nôtres :

| fichier | rôle |
|---|---|
| `ma_station.py` | le détecteur : DEPART / RETOUR / ETRANGERE |
| `mon_cloud.py` | sessions, tarif, SMS, tubes, comptes, blockchain |
| `app.html` | l'interface usager servie sur `/app` |
| `scorer_tout.py` | banc de test : tous les scénarios, plusieurs graines, avec et sans chaos |
| `lancer_sim.py` | lance le simulateur du kit sur un autre port de page |
| `chaine.py` + `contracts/` | l'inscription sur Avalanche Fuji |

## Le détecteur

La règle, en une phrase pour l'exploitant :

> Une planche est **partie** quand elle s'est tue durablement **après s'être affaiblie** ;
> si elle se tait d'un coup alors qu'on l'entendait fort, c'est sa **pile** qui est morte,
> pas la planche qui est partie. Elle est **revenue** quand on l'entend de nouveau fort,
> sans interruption.

Pourquoi ce n'est pas un seuil. Mesuré dans le simulateur :

| situation | signal reçu | la planche est-elle partie ? |
|---|---|---|
| au râtelier | ≈ -67 dBm | non |
| posée à l'envers | ≈ -72 dBm | non |
| corps mouillé devant la balise | ≈ **-85 dBm** | **non** |
| posée sur le sable à 9 m | ≈ **-87 dBm** | **non** |
| partie au large | **plus aucun paquet** | **oui** |

Le sable et le corps mouillé sont à 2 dB l'un de l'autre : aucun seuil ne les sépare, et
dans les deux cas la planche est encore là. C'est ce qui fait s'écrouler `station_exemple.py`.
Ce qui distingue vraiment un départ, c'est que la planche au large passe sous le plancher de
réception : elle ne produit plus rien du tout. Donc **un départ est un silence, pas un signal
faible**.

Restait à ne pas confondre un départ avec une balise à plat, qui se tait aussi. D'où la
deuxième moitié de la règle : on regarde le niveau des derniers paquets reçus avant le
silence. Faibles, la planche s'éloignait. Forts, elle était encore au râtelier — c'est la
pile, et le cloud reçoit une alerte au lieu d'un faux départ.

Les réglages sont en tête de `ma_station.py`. `SILENCE_DEPART = 45 s` tient compte des deux
silences légitimes : une balise mourante n'émet plus que toutes les 6 s, et `--chaos` coupe
le réseau pendant 12 s.

## Le score

```
python3 scorer_tout.py                  # notre détecteur
python3 scorer_tout.py station_exemple  # la référence à battre
```

Sur les 7 scénarios × 12 graines × avec et sans chaos :

| détecteur | justes | faux départs | faux retours | manqués |
|---|---|---|---|---|
| `ma_station.py` | **312** | **0** | **0** | **0** |
| `station_exemple.py` (3 graines) | 73 | 208 | 195 | 5 |

Latence médiane : ~13 s sur un retour, ~30 s sur un départ (tolérance du kit : 300 s).

## La page de test : http://localhost:9000/app

**Ce n'est pas l'interface finale** — elle viendra d'ailleurs. `app.html` est une page
volontairement brute qui sert à vérifier que le back tient : connexion (téléphone ou
Google/Privy), armement d'une session, chrono et prix en direct, SMS reçus, commandes du
simulateur planche par planche, et un journal de tous les appels API.

Elle affiche aussi, en bas, **la liste complète des endpoints** : c'est le contrat sur lequel
brancher la vraie UI. En résumé :

```
GET  /api/stations            { stations:[{nom,branchee,t,libre}], planches, prix_minute, prix_max, t }
GET  /api/moi?jeton=…         { cle, nom, type, wallet, tubes, session, historique, messages }
POST /api/login/tel   {tel}               -> { code_demo }
POST /api/login/code  {tel, code}         -> { jeton }
POST /api/login/privy {user_id, wallet}   -> { jeton }
POST /api/armer       {jeton, station}    -> { session }
POST /api/deconnexion {jeton}
POST /api/sim         {action, balise}    relais vers le simulateur
```

Le tableau exploitant est sur http://localhost:9000/.

### Activer Privy

1. Sur dashboard.privy.io : créer une app, activer **Google**, activer les **embedded wallets**
   (EVM), ajouter `http://localhost:9000` aux **allowed origins**.
2. `python3 mon_cloud.py --privy-app-id <APP_ID>`
3. Option : `--privy-secret <APP_SECRET>` pour que le cloud vérifie chaque connexion auprès
   de Privy. Sans secret, il fait confiance à ce que le navigateur envoie — acceptable en
   démo locale seulement.

Ces deux valeurs se passent aussi par les variables d'environnement `PRIVY_APP_ID` et
`PRIVY_APP_SECRET`, ce qui évite de les laisser traîner dans l'historique du terminal.

## Blockchain (Avalanche Fuji, testnet)

Les débuts/fins de session et les tubes gagnés sont inscrits sur la C-Chain d'Avalanche via
`contracts/KorkoEvents.sol`. C'est la seule partie du projet qui sort de « bibliothèque
standard seulement » (voir `requirements.txt`).

Un contrat est **déjà déployé** et son adresse est dans `contrat.json`, versionné : pour
seulement lire ce qui s'y trouve, il n'y a rien à faire. Pour écrire dessus, il faut un
compte opérateur à soi :

1. `pip install -r requirements.txt`
2. `python3 generer_compte.py` — écrit une clé privée dans `.env` (jamais commité) et affiche
   l'adresse publique.
3. Financer cette adresse : https://core.app/tools/testnet-faucet/ (choisir Fuji C-Chain).
4. `python3 deploy_contrat.py` pour déployer ton propre contrat, ou demander à celui qui a
   déployé `contrat.json` de t'ajouter comme opérateur (`changerOperateur`).

Sans `.env` ni `contrat.json`, `mon_cloud.py` tourne exactement pareil, sans rien écrire.

Un seul compte opérateur signe et paie le gas pour tout le monde : les usagers n'ont rien à
signer. C'est un relais de confiance, **pas** encore de la vraie abstraction de compte
(ERC-4337). Le contrat garde `enregistrerWallet` prêt pour le jour où chaque usager aura son
propre smart wallet.

## Sur la vraie maquette

```
python3 korko_test.py --trouver                  # trouve les Pi sur le réseau
python3 korko_test.py station-a.local            # vérifie le flux d'une station
python3 korko_test.py station-a.local --mesure korko-01   # calibrer : médiane glissante
python3 ma_station.py --source 192.168.8.100:8420
```

Si le RSSI mesuré au râtelier diffère nettement de -67 dBm, c'est `RSSI_1M` en tête de
`korko_sim.py` qu'il faut corriger (le kit le dit), et les seuils `PROCHE` / `LOIN` de
`ma_station.py` qui suivent. Le seuil de silence, lui, ne dépend d'aucune calibration :
c'est sa force.

## Limites connues (prochaines étapes)

- Un seul compte opérateur signe tout : pas encore d'abstraction de compte par usager.
- Paiement, caution et SMS sont simulés. Pas de photo ni de barème des tubes.
- Pas de détection « toute la station devient sourde » (mode commun) : si le Pi lui-même
  perd sa radio, toutes les planches se taisent en même temps. On le verrait au fait que
  les derniers paquets étaient forts — donc diagnostiqués « pile » — mais rien ne recoupe
  encore les balises entre elles pour conclure que c'est la station, pas six piles.
- Le tableau exploitant est brut, sans ordres de mission de rééquilibrage.
