#!/usr/bin/env python3
"""KORKO mini — le cloud : sessions, tarif, SMS (imprimés), tubes, état du parc.

    python3 cloud.py                      # http://localhost:9001

Pages :
  /              tableau de bord exploitant (se rafraîchit seul)
  /m?station=A   page mobile ouverte par le QR code (inscription + armement en un tap)
  /app           interface web usager (connexion téléphone ou Google/Privy, surf en direct, messages, panneau simulateur)
  /parc          état brut en JSON
  POST /evenements   reçoit les événements des stations (liste ou objet, doublons ignorés)
  /api/...       API JSON utilisée par /app (voir la classe Api)

Privy (optionnel) :  python3 cloud.py --privy-app-id <APP_ID> [--privy-secret <APP_SECRET>]

Horloge = le "t" des stations (jamais time.time()).
"""
import argparse, base64, html, json, math, os, random, secrets, threading, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

MAISON = {"korko-01": "A", "korko-02": "A", "korko-03": "B",
          "korko-04": "B", "korko-05": "C", "korko-06": "C"}
PRIX_MINUTE = 0.20
PRIX_MAX = 12.00          # plafond de prix d'une session
PLAFOND_DUREE = 10 * 60   # démo : rappel SMS à 10 min (3 h en exploitation)
RESERVATION = 5 * 60      # une session armée non partie expire au bout de 5 min
TUBES_SESSION = 1

verrou = threading.RLock()
vus = set()                                   # ids d'événements déjà traités
stations = {}                                 # nom -> {"t": dernier t, "presentes": [...]}
planches = {b: {"maison": m, "lieu": m, "session": None, "sorties": 0} for b, m in MAISON.items()}
clients = {}                                  # clé (tel ou privy:...) -> {"tubes", "nom", "type", "wallet"}
jetons = {}                                   # jeton de connexion -> clé client
codes = {}                                    # tel -> code de connexion en attente
CONFIG = {"privy_app_id": "", "privy_secret": "", "sim": "http://localhost:8081"}
sessions = []                                 # liste de dicts
alertes = []
sms_log = []


FICHIER_ETAT = "cloud_etat.json"


def sauver():
    etat = {"vus": sorted(vus), "stations": stations, "planches": planches, "clients": clients,
            "sessions": sessions, "alertes": alertes, "sms": sms_log, "jetons": jetons}
    with open(FICHIER_ETAT + ".tmp", "w") as f:
        json.dump(etat, f)
    os.replace(FICHIER_ETAT + ".tmp", FICHIER_ETAT)


def charger():
    if not os.path.exists(FICHIER_ETAT):
        return
    with open(FICHIER_ETAT) as f:
        e = json.load(f)
    vus.update(e["vus"]); stations.update(e["stations"]); planches.update(e["planches"])
    clients.update(e["clients"]); sessions.extend(e["sessions"])
    alertes.extend(map(tuple, e["alertes"])); sms_log.extend(map(tuple, e["sms"]))
    jetons.update(e.get("jetons", {}))
    print(f"[cloud] état rechargé : {len(sessions)} session(s)")


def maintenant(station=None):
    if station and station in stations:
        return stations[station]["t"]
    return max((s["t"] for s in stations.values()), default=0.0)


def sms(tel, texte):
    sms_log.append((tel, texte))
    print(f"[SMS -> {tel}] {texte}")


def alerte(t, texte):
    alertes.append((t, texte))
    print(f"[ALERTE] {texte}")


def fmt_duree(s):
    s = int(s)
    return f"{s // 60} min {s % 60:02d} s"


# ---------- comptes ----------
def nouveau_client(cle, **infos):
    c = clients.setdefault(cle, {"tubes": 0})
    c.setdefault("type", "google" if cle.startswith("privy:") else "tel")
    c.setdefault("nom", cle)
    c.setdefault("wallet", None)
    for k, v in infos.items():
        if v:
            c[k] = v
    return c


def ouvrir_session_web(cle):
    jeton = secrets.token_urlsafe(24)
    jetons[jeton] = cle
    sauver()
    return jeton


def verifier_privy(user_id):
    """Vérifie auprès de Privy que l'utilisateur existe (si --privy-secret est fourni).
    Renvoie le JSON utilisateur, ou None si non vérifié (mode démo)."""
    if not CONFIG["privy_secret"]:
        return None
    auth = base64.b64encode(f"{CONFIG['privy_app_id']}:{CONFIG['privy_secret']}".encode()).decode()
    req = urllib.request.Request(f"https://auth.privy.io/api/v1/users/{user_id}",
                                 headers={"Authorization": f"Basic {auth}", "privy-app-id": CONFIG["privy_app_id"]})
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.load(r)


def session_courante(cle):
    en_cours = [s for s in sessions if s["client"] == cle and s["etat"] in ("armee", "en_cours")]
    if en_cours:
        return en_cours[-1]
    finies = [s for s in sessions if s["client"] == cle]
    return finies[-1] if finies else None


def vue_session(s):
    if s is None:
        return None
    v = dict(s)
    if s["etat"] == "en_cours":
        duree = max(0, maintenant(s["station"]) - s["t_depart"])
        v["duree"] = duree
        v["prix_courant"] = min(PRIX_MAX, math.ceil(duree / 60) * PRIX_MINUTE) if duree else 0.0
    elif s["etat"] == "terminee":
        v["duree"] = s["t_retour"] - s["t_depart"]
    return v


# ---------- logique métier ----------
def armer(tel, station):
    with verrou:
        t = maintenant(station)
        nouveau_client(tel)
        for s in sessions:
            if s["client"] == tel and s["etat"] in ("armee", "en_cours"):
                return s
        prises = {s["balise"] for s in sessions if s["etat"] == "armee"}
        libres = [b for b, p in planches.items()
                  if p["lieu"] == station and p["session"] is None and b not in prises]
        if not libres:
            return None
        b = min(libres, key=lambda x: planches[x]["sorties"])   # répartir l'usure
        s = {"id": len(sessions) + 1, "client": tel, "station": station, "balise": b,
             "etat": "armee", "t_arme": t, "t_depart": None, "t_retour": None,
             "prix": None, "rappel": False}
        sessions.append(s)
        sauver()
        sms(tel, f"C'est parti : prends la planche {b} au râtelier {station}. Le compteur démarre quand tu t'éloignes.")
        return s


def depart(evt):
    b, t, st = evt["balise"], evt["t"], evt["station"]
    p = planches[b]
    p["lieu"], p["sorties"] = "dehors", p["sorties"] + 1
    s = next((s for s in sessions if s["etat"] == "armee" and s["balise"] == b), None)
    if s is None:
        # croisement : quelqu'un d'armé ici a pris une autre planche
        s = next((s for s in sessions if s["etat"] == "armee" and s["station"] == st), None)
        if s:
            sms(s["client"], f"Tu as pris la {b} au lieu de la {s['balise']} ? Réponds NON si ce n'est pas toi.")
            s["balise"] = b
    if s:
        s["etat"], s["t_depart"] = "en_cours", t
        p["session"] = s["id"]
    else:
        alerte(t, f"Sortie non armée : {b} a quitté la station {st} sans session.")


def retour(evt, etrangere):
    b, t, st = evt["balise"], evt["t"], evt["station"]
    p = planches[b]
    p["lieu"] = st
    if etrangere:
        alerte(t, f"{b} (maison {p['maison']}) raccrochée à la station {st} : à rapatrier.")
    sid = p["session"]
    p["session"] = None
    if sid is None:
        return
    s = sessions[sid - 1]
    duree = max(0, t - s["t_depart"])
    s["etat"], s["t_retour"] = "terminee", t
    s["prix"] = min(PRIX_MAX, math.ceil(duree / 60) * PRIX_MINUTE)
    clients[s["client"]]["tubes"] += TUBES_SESSION
    sms(s["client"], f"Merci ! {b} rendue. {fmt_duree(duree)} = {s['prix']:.2f} €. Caution libérée. "
                     f"+{TUBES_SESSION} tube (total {clients[s['client']]['tubes']}).")


def tic():
    t_glob = maintenant()
    for s in sessions:
        t = maintenant(s["station"]) or t_glob
        if s["etat"] == "armee" and t - s["t_arme"] > RESERVATION:
            s["etat"] = "expiree"
            sms(s["client"], "Ta réservation a expiré. Réarme quand tu veux.")
        if s["etat"] == "en_cours" and not s["rappel"] and t_glob - s["t_depart"] > PLAFOND_DUREE:
            s["rappel"] = True
            sms(s["client"], f"Ça fait {PLAFOND_DUREE // 60} min que tu surfes ! Pense à raccrocher la {s['balise']}.")


def traiter(evt):
    with verrou:
        if evt.get("id") in vus:
            return
        vus.add(evt.get("id"))
        st = evt["station"]
        info = stations.setdefault(st, {"t": 0.0, "presentes": []})
        info["t"] = max(info["t"], evt["t"])
        typ = evt["type"]
        if typ in ("VIE", "INVENTAIRE"):
            info["presentes"] = evt.get("presentes", [])
            if typ == "INVENTAIRE":
                for b, p in planches.items():
                    if b in info["presentes"]:
                        p["lieu"] = st
                    elif p["lieu"] == st:
                        p["lieu"] = "dehors"
        elif evt.get("balise") not in planches:
            print(f"[cloud] événement ignoré, balise inconnue : {evt.get('balise')}")
        elif typ == "DEPART":
            depart(evt)
        elif typ == "RETOUR":
            retour(evt, etrangere=False)
        elif typ == "ETRANGERE":
            retour(evt, etrangere=True)
        tic()
        sauver()


# ---------- pages ----------
CSS = """<style>body{font-family:system-ui;margin:16px;max-width:900px}table{border-collapse:collapse;width:100%;margin-bottom:18px}
td,th{padding:6px 8px;border-bottom:1px solid #ccc;text-align:left;font-size:14px}h2{margin-top:24px}
.big{font-size:20px;padding:14px 20px;width:100%;margin-top:10px}input{font-size:18px;padding:10px;width:100%}</style>"""


def prix_txt(s):
    return "" if s["prix"] is None else f"{s['prix']:.2f} €"


def tableau_de_bord():
    e = html.escape
    with verrou:
        st = "".join(f"<tr><td>{n}</td><td>{i['t']:.0f}</td><td>{', '.join(i['presentes']) or '—'}</td></tr>"
                     for n, i in sorted(stations.items())) or "<tr><td colspan=3>Aucune station branchée</td></tr>"
        pl = "".join(f"<tr><td>{b}</td><td>{p['maison']}</td><td>{p['lieu']}</td><td>{p['session'] or ''}</td><td>{p['sorties']}</td></tr>"
                     for b, p in planches.items())
        se = "".join(f"<tr><td>{s['id']}</td><td>{e(s['client'])}</td><td>{s['balise']}</td><td>{s['etat']}</td>"
                     f"<td>{prix_txt(s)}</td></tr>"
                     for s in reversed(sessions[-15:]))
        al = "".join(f"<li>{e(a)}</li>" for _, a in reversed(alertes[-10:])) or "<li>—</li>"
        sm = "".join(f"<li><b>{e(t)}</b> : {e(x)}</li>" for t, x in reversed(sms_log[-10:])) or "<li>—</li>"
    return f"""<!doctype html><meta charset=utf-8><meta http-equiv=refresh content=2><title>KORKO cloud</title>{CSS}
<h1>KORKO — exploitant</h1>
<form action=/arme><input name=client value="+33600000000" style="width:40%"> <select name=station><option>A<option>B<option>C</select>
<button>Armer</button></form>
<h2>Stations</h2><table><tr><th>Station</th><th>Dernier t</th><th>Planches entendues</th></tr>{st}</table>
<h2>Planches</h2><table><tr><th>Balise</th><th>Maison</th><th>Lieu</th><th>Session</th><th>Sorties</th></tr>{pl}</table>
<h2>Sessions</h2><table><tr><th>#</th><th>Client</th><th>Planche</th><th>État</th><th>Prix</th></tr>{se}</table>
<h2>Alertes</h2><ul>{al}</ul><h2>SMS envoyés</h2><ul>{sm}</ul>"""


def page_mobile(station, tel=None, msg=""):
    e = html.escape
    return f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>KORKO {e(station)}</title>{CSS}<h1>KORKO · râtelier {e(station)}</h1>
<p>{PRIX_MINUTE:.2f} € la minute, {PRIX_MAX:.0f} € maximum. Caution de 300 € pré-autorisée, jamais débitée si tu rends la planche.</p>
{f'<p style="font-size:20px"><b>{e(msg)}</b></p>' if msg else ''}
<form action=/m method=get><input type=hidden name=station value="{e(station)}">
<input name=client type=tel placeholder="Ton numéro" value="{e(tel or '')}" required>
<button class=big name=go value=1>Je veux surfer</button></form>
<p><small>Démo : pas de vraie carte bancaire. Rien à installer, rien à confirmer au retour.</small></p>"""


class Api(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def repondre(self, code, corps, type_="text/html; charset=utf-8"):
        data = corps.encode() if isinstance(corps, str) else corps
        self.send_response(code); self.send_header("Content-Type", type_); self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/":
            return self.repondre(200, tableau_de_bord())
        if u.path == "/parc":
            with verrou:
                etat = {"stations": stations, "planches": planches, "sessions": sessions,
                        "clients": clients, "alertes": alertes}
                return self.repondre(200, json.dumps(etat, indent=1), "application/json")
        if u.path == "/arme":
            s = armer(q.get("client", ""), q.get("station", "A"))
            self.send_response(303); self.send_header("Location", "/"); self.end_headers(); return
        if u.path == "/app":
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.html"), encoding="utf-8") as f:
                page = f.read().replace("__PRIVY_APP_ID__", CONFIG["privy_app_id"])
            return self.repondre(200, page)
        if u.path == "/api/stations":
            with verrou:
                prises = {s["balise"] for s in sessions if s["etat"] == "armee"}
                res = []
                for n in "ABC":
                    info = stations.get(n, {"t": None})
                    libres = [b for b, p in planches.items()
                              if p["lieu"] == n and p["session"] is None and b not in prises]
                    res.append({"nom": n, "branchee": n in stations, "t": info["t"], "libres": len(libres)})
                return self.json(200, {"stations": res, "planches": planches,
                                       "prix_minute": PRIX_MINUTE, "prix_max": PRIX_MAX})
        if u.path == "/api/moi":
            with verrou:
                cle = jetons.get(q.get("jeton", ""))
                if cle is None:
                    return self.json(401, {"erreur": "non connecté"})
                c = clients[cle]
                return self.json(200, {
                    "cle": cle, "nom": c["nom"], "type": c["type"], "wallet": c.get("wallet"),
                    "tubes": c["tubes"], "session": vue_session(session_courante(cle)),
                    "historique": [vue_session(s) for s in reversed(sessions) if s["client"] == cle][:20],
                    "messages": [x for d, x in reversed(sms_log) if d == cle][:30]})
        if u.path == "/api/sim/etat":
            try:
                with urllib.request.urlopen(CONFIG["sim"] + "/etat", timeout=2) as r:
                    return self.repondre(200, r.read(), "application/json")
            except OSError:
                return self.json(502, {"erreur": "simulateur injoignable"})
        if u.path == "/m":
            station, tel = q.get("station", "A"), q.get("client")
            msg = ""
            if tel and q.get("go"):
                s = armer(tel, station)
                if s is None:
                    msg = "Plus de planche libre ici pour le moment, désolé."
                elif s["etat"] == "en_cours":
                    msg = f"Session en cours avec la {s['balise']}. Bon surf !"
                else:
                    msg = f"Prends la planche {s['balise']}. Le compteur démarre quand tu t'éloignes."
            return self.repondre(200, page_mobile(station, tel, msg))
        self.repondre(404, "introuvable")

    def json(self, code, obj):
        return self.repondre(code, json.dumps(obj), "application/json")

    def do_POST(self):
        chemin = urlparse(self.path).path
        n = int(self.headers.get("Content-Length", 0))
        try:
            corps = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self.json(400, {"erreur": "JSON invalide"})
        if chemin == "/evenements":
            for evt in corps if isinstance(corps, list) else [corps]:
                traiter(evt)
            return self.json(200, {"ok": True})

        if chemin == "/api/login/tel":           # étape 1 : on "envoie" un code par SMS
            tel = str(corps.get("tel", "")).strip().replace(" ", "")
            if len(tel) < 6:
                return self.json(400, {"erreur": "numéro invalide"})
            with verrou:
                code = f"{random.randint(0, 9999):04d}"
                codes[tel] = code
                nouveau_client(tel, nom=tel)
                sms(tel, f"Ton code KORKO : {code}")
                sauver()
            return self.json(200, {"ok": True, "code_demo": code})
        if chemin == "/api/login/code":          # étape 2 : on vérifie le code
            tel = str(corps.get("tel", "")).strip().replace(" ", "")
            with verrou:
                if not tel or codes.get(tel) != str(corps.get("code", "")).strip():
                    return self.json(401, {"erreur": "code incorrect"})
                codes.pop(tel, None)
                return self.json(200, {"jeton": ouvrir_session_web(tel)})
        if chemin == "/api/login/privy":
            user_id = str(corps.get("user_id", ""))
            if not user_id.startswith("did:privy:"):
                return self.json(400, {"erreur": "identifiant Privy invalide"})
            wallet, nom = corps.get("wallet"), corps.get("email") or corps.get("tel") or user_id
            try:
                u = verifier_privy(user_id)
            except OSError as e:
                return self.json(401, {"erreur": f"vérification Privy impossible : {e}"})
            if u is not None:                    # vérifié côté Privy : on prend ses données à lui
                comptes = u.get("linked_accounts", [])
                wallet = next((x.get("address") for x in comptes if x.get("type") == "wallet"), wallet)
                nom = next((x.get("email") for x in comptes if x.get("type") == "google_oauth"), nom)
            with verrou:
                cle = "privy:" + user_id
                nouveau_client(cle, nom=nom, wallet=wallet, type="google")
                print(f"[cloud] connexion Privy {'vérifiée' if u else '(mode démo, non vérifiée)'} : {nom} · wallet {wallet}")
                return self.json(200, {"jeton": ouvrir_session_web(cle)})
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
        if chemin == "/api/sim/bouge":           # relais vers le simulateur (évite les soucis CORS)
            b, pos = corps.get("b", ""), corps.get("pos", "")
            try:
                urllib.request.urlopen(f"{CONFIG['sim']}/bouge?b={b}&pos={pos}", timeout=2).read()
                return self.json(200, {"ok": True})
            except OSError:
                return self.json(502, {"erreur": "simulateur injoignable"})
        return self.json(404, {"erreur": "introuvable"})


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9001)
    ap.add_argument("--reset", action="store_true", help="repartir d'un état vide")
    ap.add_argument("--privy-app-id", default=os.environ.get("PRIVY_APP_ID", ""))
    ap.add_argument("--privy-secret", default=os.environ.get("PRIVY_APP_SECRET", ""),
                    help="si fourni, chaque connexion Privy est vérifiée auprès de Privy")
    ap.add_argument("--sim", default="http://localhost:8081", help="page de contrôle du simulateur")
    a = ap.parse_args()
    CONFIG.update(privy_app_id=a.privy_app_id, privy_secret=a.privy_secret, sim=a.sim)
    if a.reset and os.path.exists(FICHIER_ETAT):
        os.remove(FICHIER_ETAT)
    charger()
    print(f"[cloud] interface usager  http://localhost:{a.port}/app")
    print(f"[cloud] tableau exploitant http://localhost:{a.port}/")
    print(f"[cloud] Privy : {'activé (app ' + a.privy_app_id[:6] + '…)' if a.privy_app_id else 'désactivé (ajoute --privy-app-id)'}"
          + (" · vérification serveur ON" if a.privy_secret else ""))
    ThreadingHTTPServer(("0.0.0.0", a.port), Api).serve_forever()
