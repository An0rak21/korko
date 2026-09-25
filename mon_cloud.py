#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mon_cloud.py — le cloud KORKO : sessions, tarif, SMS, tubes, blockchain.

    python3 mon_cloud.py                     puis http://localhost:9000
    python3 mon_cloud.py --privy-app-id <ID> pour activer la connexion Google

Pages :
    /           tableau de bord exploitant
    /app        interface usager (connexion, surf en direct, messages, panneau démo)
    /parc       état brut en JSON
    /arme?client=+336…&station=A     arme une session (compatible kit)
    POST /evenements                 les stations poussent ici (contrat du kit)
    /api/…      API JSON utilisée par /app

Horloge : le champ `t` des stations, jamais time.time(). Les stations envoient
un TIC chaque seconde de flux, c'est lui qui fait avancer le temps du cloud —
et donc la facturation — même en rejeu accéléré.

Les débuts/fins de session et les tubes gagnés sont aussi inscrits sur
Avalanche Fuji (voir chaine.py). Si la chaîne n'est pas configurée, le cloud
fonctionne exactement pareil, sans rien écrire dessus.
"""

import argparse
import base64
import hashlib
import html
import json
import math
import os
import random
import secrets
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from korko import STATIONS
from chaine import chaine, lire_env

# La console Windows est en cp1252 et ne sait pas écrire « → » ni les accents.
# Sans ça, un simple print du journal fait tomber le serveur en pleine requête.
for _flux in (sys.stdout, sys.stderr):
    try:
        _flux.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

PORT = 9000
TARIF_MIN = 0.20          # € la minute
PRIX_MAX = 12.00          # plafond d'une session
PLAFOND = 600             # s avant le SMS de rappel (3 h en exploitation)
PERDUE = 3 * PLAFOND      # au-delà : planche réputée perdue
RESERVATION = 300         # une session armée non partie expire au bout de 5 min
TUBES_SESSION = 1

ICI = os.path.dirname(os.path.abspath(__file__))
FICHIER_ETAT = os.path.join(ICI, "mon_cloud_etat.json")

#: quelle planche appartient à quelle station, d'après la config du kit
PARC = {b: s for s, planches_ in STATIONS.items() for b in planches_}

CONFIG = {"privy_app_id": "", "privy_secret": "", "sim": "http://localhost:8080"}

verrou = threading.RLock()
horloge = 0.0
stations = {}             # nom -> t de son dernier message
planches = {b: {"origine": s, "ou": s, "statut": "au râtelier",
                "sorties": 0, "session": None}
            for b, s in PARC.items()}
sessions = []             # liste de dicts ; l'id est l'index + 1
clients = {}              # clé (tel ou "privy:…") -> {tubes, nom, type, wallet}
jetons = {}               # jeton web -> clé client
codes = {}                # tel -> code de connexion en attente
attente = {}              # station -> [clés clients armés, pas encore partis]
sms_log = []              # (clé client, texte)
alertes = []              # (t, texte)
journal = []              # lignes de texte, la plus récente en tête


# ------------------------------------------------------------------ état

def sauver():
    etat = {"horloge": horloge, "stations": stations, "planches": planches,
            "sessions": sessions, "clients": clients, "jetons": jetons,
            "attente": attente, "sms": sms_log, "alertes": alertes,
            "journal": journal[:200]}
    with open(FICHIER_ETAT + ".tmp", "w", encoding="utf-8") as f:
        json.dump(etat, f, ensure_ascii=False)
    os.replace(FICHIER_ETAT + ".tmp", FICHIER_ETAT)


def charger():
    global horloge
    if not os.path.exists(FICHIER_ETAT):
        return
    with open(FICHIER_ETAT, encoding="utf-8") as f:
        e = json.load(f)
    horloge = e.get("horloge", 0.0)
    stations.update(e.get("stations", {}))
    planches.update(e.get("planches", {}))
    sessions.extend(e.get("sessions", []))
    clients.update(e.get("clients", {}))
    jetons.update(e.get("jetons", {}))
    attente.update(e.get("attente", {}))
    sms_log.extend(tuple(x) for x in e.get("sms", []))
    alertes.extend(tuple(x) for x in e.get("alertes", []))
    journal.extend(e.get("journal", []))
    print("  état rechargé : %d session(s)" % len(sessions))


# ----------------------------------------------------------------- outils

def duree_txt(s):
    s = int(max(0, s))
    return "%d min %02d s" % (s // 60, s % 60)


def note(texte):
    journal.insert(0, "[%7.1f] %s" % (horloge, texte))
    del journal[300:]
    print("  %s" % journal[0], flush=True)


def sms(cle, texte):
    """Ici on imprime. Demain : Twilio, une ligne de plus."""
    sms_log.append((cle, texte))
    note("SMS → %s : %s" % (cle, texte))


def alerte(texte):
    alertes.append((horloge, texte))
    note("ALERTE : %s" % texte)


def prix_de(duree):
    return round(min(PRIX_MAX, math.ceil(duree / 60) * TARIF_MIN), 2)


def id_chaine(s):
    """Identifiant de session unique et stable, pour la blockchain.

    L'id affiché par le cloud repart à 1 à chaque --reset : réutilisé tel quel,
    il finirait par heurter une session déjà inscrite et le contrat rejetterait
    la transaction. On le dérive donc de ce qui ne se répète pas.
    """
    graine = "%s|%s|%s|%.2f" % (s["client"], s["station"], s["balise"], s["t_depart"])
    return int.from_bytes(hashlib.sha256(graine.encode("utf-8")).digest()[:16], "big")


# ---------------------------------------------------------------- comptes

def nouveau_client(cle, **infos):
    c = clients.setdefault(cle, {"tubes": 0})
    c.setdefault("type", "google" if cle.startswith("privy:") else "tel")
    c.setdefault("nom", cle)
    c.setdefault("wallet", None)
    for k, v in infos.items():
        if v:
            c[k] = v
    return c


def ouvrir_jeton(cle):
    jeton = secrets.token_urlsafe(24)
    jetons[jeton] = cle
    sauver()
    return jeton


def verifier_privy(user_id):
    """Vérifie auprès de Privy que l'utilisateur existe (si --privy-secret fourni).
    Renvoie le JSON utilisateur, ou None si non vérifié (mode démo)."""
    if not CONFIG["privy_secret"]:
        return None
    auth = base64.b64encode(("%s:%s" % (CONFIG["privy_app_id"],
                                        CONFIG["privy_secret"])).encode()).decode()
    req = urllib.request.Request(
        "https://auth.privy.io/api/v1/users/%s" % user_id,
        headers={"Authorization": "Basic %s" % auth,
                 "privy-app-id": CONFIG["privy_app_id"]})
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.load(r)


def session_courante(cle):
    en_cours = [s for s in sessions if s["client"] == cle
                and s["etat"] in ("armee", "en_cours")]
    if en_cours:
        return en_cours[-1]
    finies = [s for s in sessions if s["client"] == cle]
    return finies[-1] if finies else None


def vue_session(s):
    if s is None:
        return None
    v = dict(s)
    if s["etat"] == "en_cours":
        duree = max(0, horloge - s["t_depart"])
        v["duree"] = duree
        v["prix_courant"] = prix_de(duree) if duree else 0.0
    elif s["etat"] == "terminee":
        v["duree"] = s["t_retour"] - s["t_depart"]
    return v


# --------------------------------------------------------------- décisions

def suggestion(station):
    """Quelle planche proposer : au râtelier, chez elle, la moins sortie."""
    prises = {s["balise"] for s in sessions if s["etat"] == "armee"}
    libres = [b for b, p in planches.items()
              if p["statut"] == "au râtelier" and p["ou"] == station
              and p["session"] is None and b not in prises]
    return min(libres, key=lambda b: planches[b]["sorties"]) if libres else None


def armer(cle, station):
    with verrou:
        nouveau_client(cle)
        for s in sessions:
            if s["client"] == cle and s["etat"] in ("armee", "en_cours"):
                return s
        b = suggestion(station)
        if b is None:
            return None
        s = {"id": len(sessions) + 1, "client": cle, "station": station,
             "balise": b, "etat": "armee", "t_arme": horloge,
             "t_depart": None, "t_retour": None, "prix": None, "rappel": False}
        sessions.append(s)
        attente.setdefault(station, []).append(cle)
        note("%s arme en station %s → on lui propose %s" % (cle, station, b))
        sms(cle, "C'est parti : prends la planche %s au râtelier %s. "
                 "Le compteur démarre quand tu t'éloignes." % (b, station))
        sauver()
        return s


def depart(balise, station, t):
    p = planches[balise]
    p["statut"], p["ou"] = "en mer", None
    p["sorties"] += 1

    s = next((s for s in sessions
              if s["etat"] == "armee" and s["balise"] == balise), None)
    if s is None:
        # Croisement : quelqu'un d'armé ici a pris une autre planche que celle
        # qu'on lui proposait. On rattache la session à la planche réellement
        # partie plutôt que de crier à la sortie sauvage.
        s = next((s for s in sessions
                  if s["etat"] == "armee" and s["station"] == station), None)
        if s is not None:
            sms(s["client"], "Tu as pris la %s au lieu de la %s ? "
                             "Réponds NON si ce n'est pas toi." % (balise, s["balise"]))
            s["balise"] = balise

    if s is None:
        p["statut"] = "sortie sans client"
        return alerte("%s a quitté la station %s sans session armée"
                      % (balise, station))

    s["etat"], s["t_depart"] = "en_cours", t
    p["session"] = s["id"]
    if s["client"] in attente.get(station, []):
        attente[station].remove(s["client"])
    note("DÉPART %s depuis %s — %s" % (balise, station, s["client"]))
    s["id_chaine"] = id_chaine(s)
    chaine.demarrer_session(s["id_chaine"], s["client"],
                            clients[s["client"]].get("wallet"), station, balise)


def retour(balise, station, t, etrangere):
    p = planches[balise]
    p["statut"], p["ou"] = "au râtelier", station
    note("%s %s en %s" % ("ETRANGERE" if etrangere else "RETOUR", balise, station))
    if etrangere:
        alerte("%s (base %s) raccrochée en station %s : à rapatrier"
               % (balise, p["origine"], station))

    sid = p["session"]
    p["session"] = None
    if sid is None:
        return
    s = sessions[sid - 1]
    if s["etat"] != "en_cours":
        return
    duree = max(0, t - s["t_depart"])
    s["etat"], s["t_retour"] = "terminee", t
    s["prix"] = prix_de(duree)
    c = clients[s["client"]]
    c["tubes"] += TUBES_SESSION
    sms(s["client"], "Merci ! %s rendue. %s = %.2f €. Caution libérée. "
                     "+%d tube (total %d)."
        % (balise, duree_txt(duree), s["prix"], TUBES_SESSION, c["tubes"]))
    if s.get("id_chaine") is not None:
        chaine.terminer_session(s["id_chaine"], round(duree), round(s["prix"] * 100))
    chaine.attribuer_points(s["client"], c.get("wallet"),
                            TUBES_SESSION, "session terminée")


def retards():
    """Appelée à chaque TIC. Le temps long, c'est le métier du cloud."""
    for s in sessions:
        if s["etat"] == "armee" and horloge - s["t_arme"] > RESERVATION:
            s["etat"] = "expiree"
            if s["client"] in attente.get(s["station"], []):
                attente[s["station"]].remove(s["client"])
            sms(s["client"], "Ta réservation a expiré. Réarme quand tu veux.")
        elif s["etat"] == "en_cours":
            duree = horloge - s["t_depart"]
            if duree > PERDUE:
                s["etat"] = "perdue"
                planches[s["balise"]]["statut"] = "perdue"
                planches[s["balise"]]["session"] = None
                sms(s["client"], "%s jamais rendue. Caution débitée : elle est à toi."
                    % s["balise"])
                alerte("%s réputée perdue" % s["balise"])
            elif duree > PLAFOND and not s["rappel"]:
                s["rappel"] = True
                sms(s["client"], "Ta session tourne depuis %s. Raccroche %s en sortant."
                    % (duree_txt(duree), s["balise"]))


def traiter(ev):
    """Un événement de station, au format du contrat du kit."""
    global horloge
    with verrou:
        t = ev.get("t", horloge)
        horloge = max(horloge, t)
        station = ev.get("station")
        if station not in stations:
            note("station %s branchée" % station)
        stations[station] = t

        type_ = ev.get("evenement")
        if type_ == "TIC":
            retards()
            sauver()
            return

        balise = ev.get("balise")
        if balise not in planches:
            return note("balise inconnue : %s" % balise)

        if type_ == "DEPART":
            depart(balise, station, t)
        elif type_ in ("RETOUR", "ETRANGERE"):
            retour(balise, station, t, etrangere=(type_ == "ETRANGERE"))
        else:
            return note("événement inconnu : %s" % type_)
        retards()
        sauver()


# ----------------------------------------------------------------- pages

def tableau():
    if not stations:
        l = ["AUCUNE STATION BRANCHÉE", "-----------------------",
             "Dans un autre terminal, avec le simulateur lancé :", "",
             "    python3 ma_station.py --source localhost:8420", ""]
    else:
        l = ["STATIONS", "--------"] + \
            ["%s   dernier message à t = %.0f s" % (n, t)
             for n, t in sorted(stations.items())] + [""]

    l += ["PARC", "----"]
    for b, p in sorted(planches.items()):
        s = sessions[p["session"] - 1] if p["session"] else None
        ou = "" if p["ou"] in (None, p["origine"]) else " (en %s)" % p["ou"]
        l.append("%-9s %-18s base %s%-6s sorties %-3d %s"
                 % (b, p["statut"], p["origine"], ou, p["sorties"],
                    "→ %s depuis %s" % (s["client"], duree_txt(horloge - s["t_depart"]))
                    if s else ""))

    recentes = [s for s in sessions if s["etat"] != "expiree"][-8:]
    if recentes:
        l += ["", "SESSIONS", "--------"]
        for s in reversed(recentes):
            l.append("#%-3d %-22s %-9s %-10s %s"
                     % (s["id"], s["client"], s["balise"], s["etat"],
                        "" if s["prix"] is None else "%.2f €" % s["prix"]))

    l += ["", "JOURNAL", "-------"] + journal[:15]
    return "\n".join(l)


PAGE = """<!doctype html><meta charset=utf-8><meta http-equiv=refresh content=2>
<title>KORKO cloud</title>
<body style="font:14px ui-monospace,monospace;background:#EDE3CE;color:#1F6B6B;padding:1.5rem">
<h2>KORKO — cloud · t = %.0f s</h2>
<p>%s · <a href="/app">interface usager</a></p><pre>%s</pre></body>"""


def ligne_chaine():
    if not chaine.actif:
        return "blockchain désactivée (generer_compte.py puis deploy_contrat.py)"
    return ('blockchain : <a target=_blank href="https://testnet.snowtrace.io/address/%s">'
            'contrat %s sur Avalanche Fuji ↗</a>' % (chaine.contrat.address,
                                                     chaine.contrat.address))


# ---------------------------------------------------------------- serveur

class Cloud(BaseHTTPRequestHandler):

    def log_message(self, *a):
        pass

    def repondre(self, corps, type_="text/plain; charset=utf-8", code=200):
        corps = corps.encode("utf-8") if isinstance(corps, str) else corps
        self.send_response(code)
        self.send_header("Content-Type", type_)
        self.send_header("Content-Length", str(len(corps)))
        self.end_headers()
        self.wfile.write(corps)

    def json(self, code, obj):
        self.repondre(json.dumps(obj, ensure_ascii=False),
                      "application/json; charset=utf-8", code)

    # -- lectures ---------------------------------------------------------
    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}

        if u.path == "/":
            with verrou:
                return self.repondre(PAGE % (horloge, ligne_chaine(), html.escape(tableau())),
                                     "text/html; charset=utf-8")

        if u.path == "/parc":
            with verrou:
                return self.json(200, {"t": horloge, "stations": stations,
                                       "planches": planches, "sessions": sessions,
                                       "alertes": alertes})

        if u.path == "/arme":                     # forme texte, compatible kit
            cle = q.get("client", "+33600000000").strip()
            cle = cle if cle.startswith("+") else "+" + cle
            s = armer(cle, q.get("station", "A"))
            if s is None:
                return self.repondre("Plus de planche libre en station %s."
                                     % q.get("station", "A"))
            return self.repondre("Prends la planche %s." % s["balise"])

        if u.path == "/app":
            with open(os.path.join(ICI, "app.html"), encoding="utf-8") as f:
                page = f.read().replace("__PRIVY_APP_ID__", CONFIG["privy_app_id"])
            return self.repondre(page, "text/html; charset=utf-8")

        if u.path == "/api/stations":
            with verrou:
                res = []
                for n in sorted(STATIONS):
                    libre = suggestion(n)
                    res.append({"nom": n, "branchee": n in stations,
                                "t": stations.get(n), "libre": libre})
                return self.json(200, {"stations": res, "planches": planches,
                                       "prix_minute": TARIF_MIN, "prix_max": PRIX_MAX,
                                       "t": horloge})

        if u.path == "/api/moi":
            with verrou:
                cle = jetons.get(q.get("jeton", ""))
                if cle is None:
                    return self.json(401, {"erreur": "non connecté"})
                c = clients[cle]
                return self.json(200, {
                    "cle": cle, "nom": c["nom"], "type": c["type"],
                    "wallet": c.get("wallet"), "tubes": c["tubes"],
                    "session": vue_session(session_courante(cle)),
                    "historique": [vue_session(s) for s in reversed(sessions)
                                   if s["client"] == cle][:20],
                    "messages": [x for d, x in reversed(sms_log) if d == cle][:30]})

        return self.repondre("introuvable", code=404)

    # -- écritures --------------------------------------------------------
    def do_POST(self):
        chemin = urlparse(self.path).path
        brut = self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0)

        if chemin == "/evenements":
            # Le contrat du kit : une ligne JSON par événement, ou plusieurs.
            for ligne in brut.decode("utf-8").strip().splitlines():
                if not ligne.strip():
                    continue
                try:
                    traiter(json.loads(ligne))
                except Exception as e:                     # noqa: BLE001
                    note("événement illisible : %s" % e)
            return self.repondre("ok")

        try:
            corps = json.loads(brut or b"{}")
        except ValueError:
            return self.json(400, {"erreur": "JSON invalide"})

        if chemin == "/api/login/tel":            # étape 1 : on « envoie » un code
            tel = str(corps.get("tel", "")).strip().replace(" ", "")
            if len(tel) < 6:
                return self.json(400, {"erreur": "numéro invalide"})
            with verrou:
                code = "%04d" % random.randint(0, 9999)
                codes[tel] = code
                nouveau_client(tel, nom=tel)
                sms(tel, "Ton code KORKO : %s" % code)
                sauver()
            return self.json(200, {"ok": True, "code_demo": code})

        if chemin == "/api/login/code":           # étape 2 : on vérifie le code
            tel = str(corps.get("tel", "")).strip().replace(" ", "")
            with verrou:
                if not tel or codes.get(tel) != str(corps.get("code", "")).strip():
                    return self.json(401, {"erreur": "code incorrect"})
                codes.pop(tel, None)
                return self.json(200, {"jeton": ouvrir_jeton(tel)})

        if chemin == "/api/login/privy":
            user_id = str(corps.get("user_id", ""))
            if not user_id.startswith("did:privy:"):
                return self.json(400, {"erreur": "identifiant Privy invalide"})
            wallet = corps.get("wallet")
            nom = corps.get("email") or corps.get("tel") or user_id
            try:
                u = verifier_privy(user_id)
            except OSError as e:
                return self.json(401, {"erreur": "vérification Privy impossible : %s" % e})
            if u is not None:                     # vérifié côté Privy : ses données à lui
                comptes = u.get("linked_accounts", [])
                wallet = next((x.get("address") for x in comptes
                               if x.get("type") == "wallet"), wallet)
                nom = next((x.get("email") for x in comptes
                            if x.get("type") == "google_oauth"), nom)
            with verrou:
                cle = "privy:" + user_id
                nouveau_client(cle, nom=nom, wallet=wallet, type="google")
                chaine.enregistrer_wallet(cle, wallet)
                note("connexion Privy %s : %s"
                     % ("vérifiée" if u else "(démo, non vérifiée)", nom))
                return self.json(200, {"jeton": ouvrir_jeton(cle)})

        if chemin == "/api/armer":
            with verrou:
                cle = jetons.get(corps.get("jeton", ""))
            if cle is None:
                return self.json(401, {"erreur": "non connecté"})
            s = armer(cle, corps.get("station", "A"))
            if s is None:
                return self.json(409, {"erreur": "Plus de planche libre à ce râtelier."})
            return self.json(200, {"session": vue_session(s)})

        if chemin == "/api/deconnexion":
            with verrou:
                jetons.pop(corps.get("jeton", ""), None)
                sauver()
            return self.json(200, {"ok": True})

        if chemin == "/api/sim":                  # relais vers le simulateur (CORS)
            try:
                req = urllib.request.Request(
                    CONFIG["sim"] + "/", json.dumps(corps).encode(),
                    {"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=2) as r:
                    return self.repondre(r.read(), "application/json")
            except OSError as e:
                return self.json(502, {"erreur": "simulateur injoignable : %s" % e})

        return self.json(404, {"erreur": "introuvable"})


if __name__ == "__main__":
    # Les secrets se lisent dans l'environnement ou dans .env (jamais commité) :
    # passés en argument, ils seraient lisibles par n'importe quel autre
    # processus de la machine (tasklist, wmic, ps).
    env = lire_env()

    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--reset", action="store_true", help="repartir d'un état vide")
    ap.add_argument("--privy-app-id", default=env.get("PRIVY_APP_ID", ""))
    ap.add_argument("--privy-secret", default=env.get("PRIVY_APP_SECRET", ""),
                    help="mieux : mettre PRIVY_APP_SECRET dans .env plutôt qu'ici")
    ap.add_argument("--sim", default="http://localhost:8080",
                    help="page de contrôle du simulateur")
    a = ap.parse_args()

    CONFIG.update(privy_app_id=a.privy_app_id, privy_secret=a.privy_secret, sim=a.sim)
    if a.reset and os.path.exists(FICHIER_ETAT):
        os.remove(FICHIER_ETAT)
    charger()

    print("Cloud KORKO sur http://localhost:%d" % a.port)
    print("  tableau de bord : /        interface usager : /app")
    print("  état brut : /parc          les stations poussent sur : POST /evenements")
    print("  Privy : %s" % ("activé (app %s…)" % a.privy_app_id[:6]
                            if a.privy_app_id else "désactivé (--privy-app-id)"))
    ThreadingHTTPServer(("", a.port), Cloud).serve_forever()
