#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cloud_exemple.py — le cloud KORKO le plus bête qui marche.

Bibliothèque standard uniquement. Rien à installer.

    python3 cloud_exemple.py        puis http://localhost:9000

Il écoute les événements envoyés par les stations, tient l'état du parc,
ouvre et ferme les sessions clients, facture au temps, et envoie des SMS
(ici : il les imprime dans le terminal).

Copiez-le en mon_cloud.py, et remplacez les règles par les vôtres.
"""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

PORT = 9000

TARIF_MIN = 0.20          # € la minute
PLAFOND = 600             # secondes avant le rappel SMS.
                          # 3 h en exploitation réelle ; 10 min ici pour
                          # que ça se déclenche pendant une démo.
PERDUE = 3 * PLAFOND      # au-delà : planche réputée perdue, caution débitée

#: la flotte : quelle planche appartient à quelle station
PARC = {"korko-01": "A", "korko-02": "A",
        "korko-03": "B", "korko-04": "B",
        "korko-05": "C", "korko-06": "C"}

planches = {b: {"origine": s, "ou": s, "statut": "au râtelier", "sorties": 0}
            for b, s in PARC.items()}
sessions = {}          # balise -> {"client":…, "debut":…, "rappel":bool}
file_attente = {}      # station -> [clients armés, pas encore partis]
journal = []           # lignes de texte, la plus récente en tête

horloge = 0.0          # secondes de flux. JAMAIS time.time() : voir LISEZ_MOI.
signes = {}            # station -> t de son dernier message


# ---------------------------------------------------------------- outils

def duree_txt(s):
    s = int(s)
    return "%d min" % (s // 60) if s < 3600 else "%d h %02d" % (s // 3600, s % 3600 // 60)


def note(texte):
    journal.insert(0, "[%7.1f] %s" % (horloge, texte))
    print("  %s" % journal[0], flush=True)


def sms(client, texte):
    """Ici on imprime. Demain : Twilio, une ligne de plus."""
    note("SMS → %s : %s" % (client, texte))


def suggestion(station):
    """Quelle planche proposer : au râtelier, chez elle, la moins sortie."""
    dispo = [b for b, p in planches.items()
             if p["statut"] == "au râtelier" and p["ou"] == station
             and b not in sessions]
    return min(dispo, key=lambda b: planches[b]["sorties"]) if dispo else None


# ------------------------------------------------------------- décisions

def cloturer(balise, station, t, hors_base):
    p = planches[balise]
    p["statut"] = "au râtelier"
    p["ou"] = station
    s = sessions.pop(balise, None)
    if s:
        duree = t - s["debut"]
        sms(s["client"], "Merci ! %s, %s, %.2f €. Caution libérée."
            % (balise, duree_txt(duree), duree / 60 * TARIF_MIN))
    if hors_base:
        note("RÉÉQUILIBRAGE : %s rendue en %s, sa base est %s"
             % (balise, station, p["origine"]))


def retards():
    """Appelée à chaque TIC. Le temps long, c'est le métier du cloud."""
    for balise, s in list(sessions.items()):
        duree = horloge - s["debut"]
        if duree > PERDUE:
            sms(s["client"], "%s jamais rendue. Caution débitée : elle est à toi." % balise)
            planches[balise]["statut"] = "perdue"
            note("ALERTE : %s réputée perdue" % balise)
            del sessions[balise]
        elif duree > PLAFOND and not s["rappel"]:
            s["rappel"] = True
            sms(s["client"], "Ta session tourne depuis %s. Raccroche %s en sortant."
                % (duree_txt(duree), balise))


def traiter(ev):
    """Un événement de station. C'est ici que commence votre travail."""
    global horloge
    horloge = max(horloge, ev.get("t", horloge))
    type_ = ev.get("evenement")
    if ev.get("station") not in signes:
        note("station %s branchée" % ev.get("station"))
    signes[ev.get("station")] = ev.get("t", horloge)

    if type_ == "TIC":                      # battement d'horloge de la station
        retards()
        return

    balise, station = ev.get("balise"), ev.get("station")
    if balise not in planches:
        return note("balise inconnue : %s" % balise)

    if type_ == "DEPART":
        p = planches[balise]
        p["statut"], p["ou"] = "en mer", None
        p["sorties"] += 1
        attente = file_attente.setdefault(station, [])
        if not attente:                     # personne n'a armé de session
            p["statut"] = "sortie sans client"
            return note("ALERTE : %s sortie de %s sans session armée" % (balise, station))
        client = attente.pop(0)
        sessions[balise] = {"client": client, "debut": ev["t"], "rappel": False}
        note("DÉPART %s depuis %s — %s" % (balise, station, client))

    elif type_ in ("RETOUR", "ETRANGERE"):
        note("%s %s en %s" % (type_, balise, station))
        cloturer(balise, station, ev["t"], hors_base=(type_ == "ETRANGERE"))


# --------------------------------------------------------------- serveur

PAGE = """<!doctype html><meta charset=utf-8>
<meta http-equiv=refresh content=2>
<title>KORKO cloud</title>
<body style="font:14px ui-monospace,monospace;background:#EDE3CE;color:#1F6B6B;padding:1.5rem">
<h2>KORKO — cloud · t = %.0f s</h2><pre>%s</pre></body>"""


def tableau():
    if not signes:
        l = ["AUCUNE STATION BRANCHÉE", "-----------------------",
             "Le cloud n'a encore reçu aucun message. Dans un autre terminal :",
             "",
             "    python3 station_exemple.py --source localhost:8420",
             "",
             "avec le simulateur déjà lancé, ou l'adresse d'une vraie station.", ""]
    else:
        l = ["STATIONS", "--------"] + ["%s   dernier message à t = %.0f s" % (st, t)
                                        for st, t in sorted(signes.items())] + [""]
    l += ["PARC", "----"]
    for b, p in sorted(planches.items()):
        s = sessions.get(b)
        ou = "" if p["ou"] in (None, p["origine"]) else " (en %s)" % p["ou"]
        l.append("%-9s %-18s base %s%-6s sorties %-3d %s"
                 % (b, p["statut"], p["origine"], ou, p["sorties"],
                    "→ %s depuis %s" % (s["client"], duree_txt(horloge - s["debut"])) if s else ""))
    l += ["", "JOURNAL", "-------"] + journal[:15]
    return "\n".join(l)


class Cloud(BaseHTTPRequestHandler):

    def repondre(self, corps, type_="text/plain; charset=utf-8", code=200):
        corps = corps.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", type_)
        self.send_header("Content-Length", str(len(corps)))
        self.end_headers()
        self.wfile.write(corps)

    def do_POST(self):
        """Les stations poussent ici. Une ligne JSON, ou plusieurs."""
        brut = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode("utf-8")
        for ligne in brut.strip().splitlines():
            try:
                traiter(json.loads(ligne))
            except Exception as e:
                note("événement illisible : %s" % e)
        self.repondre("ok")

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)

        if u.path == "/parc":                       # l'état, en JSON
            return self.repondre(json.dumps(
                {"t": horloge, "planches": planches, "sessions": sessions},
                ensure_ascii=False, indent=1), "application/json; charset=utf-8")

        if u.path == "/arme":                       # le client arme sa session
            client = q.get("client", ["+33600000000"])[0].strip()
            client = client if client.startswith("+") else "+" + client
            station = q.get("station", ["A"])[0]
            b = suggestion(station)
            if not b:
                return self.repondre("Plus de planche libre en station %s." % station)
            file_attente.setdefault(station, []).append(client)
            note("%s arme en station %s → on lui propose %s" % (client, station, b))
            return self.repondre("Prends la planche %s." % b)

        return self.repondre(PAGE % (horloge, tableau()), "text/html; charset=utf-8")

    def log_message(self, *a):                      # silence : le journal suffit
        pass


if __name__ == "__main__":
    print("Cloud KORKO sur http://localhost:%d" % PORT)
    print("  tableau de bord : /      armer un client : /arme?client=+33612&station=A")
    print("  état brut : /parc        les stations poussent sur : POST /evenements\n")
    HTTPServer(("", PORT), Cloud).serve_forever()


# Ce que cet exemple fait mal, et qui est tout le sujet :
#
#   - deux clients qui arment en même temps se voient proposer la même
#     planche, et le premier départ attrape le premier client de la file ;
#   - une planche rendue hors base reste comptée « au râtelier » : personne
#     ne décide quand ni comment on la rapatrie ;
#   - la rotation d'usure se limite à « la moins sortie » ; rien sur l'état
#     réel des planches, la météo, l'heure, l'affluence ;
#   - tout est en mémoire : si le cloud redémarrait, le parc serait amnésique ;
#   - le client n'existe que comme numéro de téléphone. Pas d'historique,
#     pas de confiance, pas de fidélité.
