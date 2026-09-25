"""
korko_sim.py — le simulateur de station KORKO.

    python korko_sim.py
        page de contrôle : http://localhost:8080
        flux korko       : tcp://0.0.0.0:8420

Trois modes, un seul flux de sortie :

  Manuel      vous pilotez les planches à la main, bouton par bouton.
  Scénarios   des situations prêtes à jouer, avec vérité terrain connue.
  Mes traces  vos enregistrements, rejoués à l'identique.

Tout ce qui est joué peut être enregistré dans traces/, et tout ce qui est
dans traces/ peut être rejoué — ici, ou en ligne de commande avec --rejeu.
Une session enregistrée produit deux fichiers : les observations, et la
vérité terrain qui va avec.

Calibration : balise Feasycom FSC-BP108B à -19,5 dBm, intervalle 1000 ms.
RSSI_1M est le seul chiffre à corriger après une mesure réelle.
"""

import json
import math
import os
import random
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --------------------------------------------------------------------------
# Calibration — à ajuster après mesure terrain
# --------------------------------------------------------------------------

RSSI_1M = -62.0        # puissance reçue à 1 m, balise à -19,5 dBm
EXPOSANT = 2.6         # exposant de propagation (2 = espace libre)
SIGMA = 3.0            # bruit gaussien, en dB
DERIVE = 1.2           # amplitude de l'évanouissement lent, en dB
OCCLUSION = 18.0       # atténuation d'un corps mouillé devant la balise
ENVERS = 5.0           # atténuation d'une planche posée face contre terre
INTERVALLE = 1.0       # intervalle d'émission, en secondes
PLANCHER = -100.0      # en dessous, le paquet n'est pas reçu
PERTE = 0.03           # probabilité de perte d'un paquet, à courte distance

PAS = 0.25             # granularité de la simulation, en secondes
RACK = (1.5, 0.0)      # position du râtelier vu de la station
SABLE = (8.0, 4.0)     # la planche posée un peu plus loin
LARGE = (0.0, 90.0)    # au large, hors de portée

PORT_FLUX = 8420
PORT_PAGE = 8080
DOSSIER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "traces")

#: les planches de la station A : elles y sont rangées et doivent y revenir
PLANCHES = ["korko-01", "korko-02"]

#: les planches des stations B et C. Hors de portée par défaut : la station A
#: ne les entend jamais. Sauf le jour où quelqu'un en rapporte une ici.
ETRANGERES = ["korko-03", "korko-04", "korko-05", "korko-06"]

#: à quelle station appartient chaque planche étrangère
ORIGINES = {"korko-03": "B", "korko-04": "B",
            "korko-05": "C", "korko-06": "C"}


# --------------------------------------------------------------------------
# Le modèle physique
# --------------------------------------------------------------------------

class Balise:
    def __init__(self, ident, x=RACK[0], y=RACK[1], origine="A"):
        self.id = ident
        self.origine = origine
        self.attend_etrangere = False
        self.x, self.y = x, y
        self.cx, self.cy = x, y
        self.cible_nom = "rack"
        self.vitesse = 1.2
        self.occlusion = False
        self.envers = False
        self.vivante = True
        self.intervalle = INTERVALLE
        self.prochaine = 0.0
        self.derive = 0.0
        self.attend_retour = False
        self.dernier_rssi = None
        self.dernier_vu = None

    @property
    def distance(self):
        return math.hypot(self.x, self.y)

    @property
    def lieu(self):
        d = self.distance
        if self.cible_nom == "large":
            if self.origine != "A":
                return "chez elle" if d > 40 else "repart chez elle"
            return "au large" if d > 40 else "s'éloigne"
        if self.cible_nom == "sable":
            return "sur le sable" if d > 6 else "va sur le sable"
        return "au râtelier" if d < 2.5 else "revient"

    def avancer(self, dt, alea):
        dx, dy = self.cx - self.x, self.cy - self.y
        d = math.hypot(dx, dy)
        if d > 0.01:
            pas = min(d, self.vitesse * dt)
            self.x += dx / d * pas
            self.y += dy / d * pas
        self.derive = max(-3.0, min(3.0, self.derive + alea.gauss(0, 0.25)))

    def mesurer(self, alea):
        d = max(0.3, self.distance)
        r = RSSI_1M - 10.0 * EXPOSANT * math.log10(d)
        r += self.derive * DERIVE
        r += alea.gauss(0, SIGMA)
        if self.occlusion:
            r -= OCCLUSION
        if self.envers:
            r -= ENVERS
        if r < PLANCHER:
            return None
        perte = PERTE + min(0.5, max(0.0, (d - 8.0) / 40.0))
        if alea.random() < perte:
            return None
        return int(round(r))


# --------------------------------------------------------------------------
# Le simulateur
# --------------------------------------------------------------------------

class Simulateur:

    SCENARIOS = {
        "depart": ("Un départ franc, puis rien", 420, [
            (60, "partir", "korko-01"),
        ]),
        "sable": ("La planche posée sur le sable — aucun départ", 420, [
            (60, "poser", "korko-01"),
            (240, "ranger", "korko-01"),
        ]),
        "corps": ("Un corps mouillé devant la balise — aucun départ", 300, [
            (60, "corps", "korko-02"),
            (140, "corps", "korko-02"),
        ]),
        "morte": ("Une balise qui faiblit puis se tait", 600, [
            (150, "mourante", "korko-02"),
            (330, "tuer", "korko-02"),
        ]),
        "foule": ("Deux départs rapprochés, deux retours", 900, [
            (60, "partir", "korko-01"),
            (75, "partir", "korko-02"),
            (600, "revenir", "korko-01"),
            (700, "revenir", "korko-02"),
        ]),
        "etrangere": ("Une planche de la station B rapportée ici", 700, [
            (60, "partir", "korko-01"),
            (180, "arrive", "korko-03"),
            (400, "revenir", "korko-01"),
            (560, "sen_va", "korko-03"),
        ]),
        "journee": ("Une matinée complète, avec tous les pièges", 2400, [
            (120, "partir", "korko-01"),
            (200, "poser", "korko-02"),
            (420, "ranger", "korko-02"),
            (500, "corps", "korko-02"),
            (580, "corps", "korko-02"),
            (700, "envers", "korko-02"),
            (900, "revenir", "korko-01"),
            (1100, "partir", "korko-02"),
            (1500, "arrive", "korko-04"),
            (1700, "revenir", "korko-02"),
            (1900, "sen_va", "korko-04"),
            (2100, "mourante", "korko-01"),
        ]),
    }

    def __init__(self, graine=7, chaos=False, station="A"):
        self.graine = graine
        self.station = station
        self.chaos = chaos
        self.vitesse = 1.0
        self.en_pause = False
        self.enr = None
        self.mode = "manuel"
        self.source = "manuel"
        self.titre = "Pilotage à la main"
        self.duree = None
        self.duree_prevue = 900
        self._tampon = []
        self._verrou = threading.Lock()
        self.reinitialiser()

    # -- remise à zéro -----------------------------------------------------

    def reinitialiser(self):
        # le tampon contient encore les observations de la scène précédente,
        # avec des horodatages bien plus grands que le nouveau t : il faut
        # le vider, sinon le graphe relie l'ancien flux au nouveau.
        with self._verrou:
            self._tampon = []
        self.alea = random.Random(self.graine)
        self.t = 0.0
        self.verite = []
        self.script = []
        self.termine = False
        self.rejeu = []
        self.rejeu_i = 0
        self.balises = {}
        for nom in PLANCHES:
            b = Balise(nom, RACK[0] + self.alea.uniform(-0.4, 0.4),
                       RACK[1] + self.alea.uniform(-0.5, 0.5), origine="A")
            b.prochaine = self.alea.uniform(0, INTERVALLE)
            self.balises[nom] = b
        for nom in ETRANGERES:
            b = Balise(nom, LARGE[0], LARGE[1], origine=ORIGINES.get(nom, "B"))
            b.cx, b.cy = LARGE
            b.cible_nom = "large"
            b.prochaine = self.alea.uniform(0, INTERVALLE)
            self.balises[nom] = b

    # -- les trois modes ---------------------------------------------------

    def mode_manuel(self):
        self.reinitialiser()
        self.mode = "manuel"
        self.source = "manuel"
        self.titre = "Pilotage à la main"
        self.duree = None
        self.en_pause = False

    def charger_scenario(self, nom):
        if nom not in self.SCENARIOS:
            raise SystemExit("scénario inconnu : %s\ndisponibles : %s"
                             % (nom, ", ".join(self.SCENARIOS)))
        titre, duree, script = self.SCENARIOS[nom]
        self.reinitialiser()
        self.mode = "scenario"
        self.source = nom
        self.titre = titre
        self.duree = duree
        self.duree_prevue = duree
        self.script = list(script)
        self.en_pause = False
        return duree

    def charger_trace(self, fichier):
        chemin = os.path.join(DOSSIER, os.path.basename(fichier))
        obs = []
        with open(chemin, encoding="utf-8") as f:
            for ligne in f:
                ligne = ligne.strip()
                if ligne:
                    d = json.loads(ligne)
                    if "rssi" in d:
                        obs.append(d)
        obs.sort(key=lambda o: o["t"])
        verite = []
        cote = os.path.splitext(chemin)[0] + ".verite.json"
        if os.path.exists(cote):
            with open(cote, encoding="utf-8") as f:
                verite = json.load(f)

        self.reinitialiser()
        self.mode = "rejeu"
        self.source = os.path.basename(chemin)
        self.titre = "Rejeu de " + self.source
        self.rejeu = obs
        self.verite = verite
        self.duree = obs[-1]["t"] if obs else 0
        self.balises = {}
        for o in obs:
            if o["balise"] not in self.balises:
                self.balises[o["balise"]] = Balise(o["balise"])
        self.en_pause = False

    # -- enregistrement ----------------------------------------------------

    def enr_debut(self, nom):
        nom = re.sub(r"[^A-Za-z0-9_-]+", "-", (nom or "essai").strip())[:40]
        self.enr = {"nom": nom.strip("-") or "essai", "obs": [], "t0": self.t}

    def enr_fin(self):
        if not self.enr:
            return None
        os.makedirs(DOSSIER, exist_ok=True)
        t0, nom = self.enr["t0"], self.enr["nom"]
        base = os.path.join(DOSSIER, nom)
        n = 1
        while os.path.exists(base + ".ndjson"):
            n += 1
            base = os.path.join(DOSSIER, "%s-%d" % (nom, n))

        with open(base + ".ndjson", "w", encoding="utf-8") as f:
            for o in self.enr["obs"]:
                d = dict(o)
                d["t"] = round(d["t"] - t0, 3)
                f.write(json.dumps(d) + "\n")
        verite = [dict(v, t=round(v["t"] - t0, 2))
                  for v in self.verite if v["t"] >= t0]
        with open(base + ".verite.json", "w", encoding="utf-8") as f:
            json.dump(verite, f, ensure_ascii=False, indent=1)

        infos = {"fichier": os.path.basename(base) + ".ndjson",
                 "observations": len(self.enr["obs"]),
                 "verites": len(verite),
                 "duree": round(self.t - t0, 1)}
        self.enr = None
        return infos

    def bibliotheque(self):
        os.makedirs(DOSSIER, exist_ok=True)
        traces = []
        for f in sorted(os.listdir(DOSSIER)):
            if not f.endswith(".ndjson"):
                continue
            chemin = os.path.join(DOSSIER, f)
            n, dernier, balises = 0, 0.0, set()
            try:
                with open(chemin, encoding="utf-8") as fh:
                    for ligne in fh:
                        if ligne.strip():
                            d = json.loads(ligne)
                            n += 1
                            dernier = max(dernier, d["t"])
                            balises.add(d["balise"])
            except (ValueError, KeyError, OSError):
                continue
            cote = os.path.splitext(chemin)[0] + ".verite.json"
            nv = 0
            if os.path.exists(cote):
                try:
                    with open(cote, encoding="utf-8") as fh:
                        nv = len(json.load(fh))
                except ValueError:
                    nv = 0
            traces.append({"fichier": f, "observations": n,
                           "duree": round(dernier), "balises": len(balises),
                           "verites": nv})
        scenarios = [{"id": k, "titre": v[0], "duree": v[1]}
                     for k, v in self.SCENARIOS.items()]
        return {"scenarios": scenarios, "traces": traces, "dossier": DOSSIER}

    # -- commandes ---------------------------------------------------------

    def cmd(self, action, balise=None, valeur=None):
        b = self.balises.get(balise) if balise else None
        if action == "partir" and b:
            b.cx, b.cy = LARGE
            b.cible_nom = "large"
            b.attend_retour = False
            self._verite(b.id, "DEPART")
        elif action == "revenir" and b:
            b.cx, b.cy = RACK
            b.cible_nom = "rack"
            b.attend_retour = True
        elif action == "poser" and b:
            b.cx, b.cy = SABLE
            b.cible_nom = "sable"
        elif action == "ranger" and b:
            b.cx = RACK[0] + self.alea.uniform(-0.4, 0.4)
            b.cy = RACK[1]
            b.cible_nom = "rack"
            b.attend_retour = False
        elif action == "arrive" and b:
            b.cx, b.cy = RACK
            b.cible_nom = "rack"
            b.attend_etrangere = True
        elif action == "sen_va" and b:
            b.cx, b.cy = LARGE
            b.cible_nom = "large"
            b.attend_etrangere = False
        elif action == "corps" and b:
            b.occlusion = not b.occlusion
        elif action == "envers" and b:
            b.envers = not b.envers
        elif action == "muette" and b:
            b.vivante = not b.vivante
            if b.vivante:
                b.intervalle = INTERVALLE
        elif action == "tuer" and b:
            b.vivante = False
        elif action == "reveiller" and b:
            b.vivante = True
            b.intervalle = INTERVALLE
        elif action == "mourante" and b:
            b.intervalle = 6.0
        elif action == "pause":
            self.en_pause = not self.en_pause
        elif action == "vitesse":
            self.vitesse = max(0.25, min(200.0, float(valeur)))
        elif action == "manuel":
            self.mode_manuel()
        elif action == "scenario":
            self.charger_scenario(valeur)
        elif action == "trace":
            self.charger_trace(valeur)
        elif action == "recommencer":
            if self.mode == "scenario":
                self.charger_scenario(self.source)
            elif self.mode == "rejeu":
                self.charger_trace(self.source)
            else:
                self.mode_manuel()
        elif action == "reglage":
            globals()[valeur["nom"]] = float(valeur["valeur"])

    def _verite(self, balise, evenement):
        self.verite.append({"t": round(self.t, 2), "station": self.station,
                            "balise": balise, "evenement": evenement})

    # -- avancement --------------------------------------------------------

    def pas(self):
        self.t += PAS
        obs = []

        if self.mode == "rejeu":
            while self.rejeu_i < len(self.rejeu) and \
                    self.rejeu[self.rejeu_i]["t"] <= self.t:
                o = self.rejeu[self.rejeu_i]
                self.rejeu_i += 1
                obs.append(o)
                b = self.balises.get(o["balise"])
                if b:
                    b.dernier_rssi = o["rssi"]
                    b.dernier_vu = o["t"]
            if self.rejeu_i >= len(self.rejeu):
                self.termine = True
                self.en_pause = True
        else:
            while self.script and self.script[0][0] <= self.t:
                _, action, cible = self.script.pop(0)
                self.cmd(action, cible)
            trou = self.chaos and (int(self.t) % 180) < 12
            for b in self.balises.values():
                avant = b.distance
                b.avancer(PAS, self.alea)
                if b.attend_retour and avant > 2.0 >= b.distance:
                    b.attend_retour = False
                    self._verite(b.id, "RETOUR")
                if b.attend_etrangere and avant > 2.0 >= b.distance:
                    b.attend_etrangere = False
                    self._verite(b.id, "ETRANGERE")
                if not b.vivante or trou:
                    continue
                if self.t >= b.prochaine:
                    b.prochaine = self.t + b.intervalle
                    r = b.mesurer(self.alea)
                    if r is not None:
                        b.dernier_rssi = r
                        b.dernier_vu = self.t
                        obs.append({"t": round(self.t, 3),
                                    "station": self.station,
                                    "balise": b.id, "rssi": r})
            if self.mode == "scenario" and self.duree and \
                    self.t >= self.duree and not self.script:
                self.termine = True
                self.en_pause = True

        if self.enr is not None and obs:
            self.enr["obs"].extend(obs)
        return obs

    def vider_tampon(self):
        with self._verrou:
            t, self._tampon = self._tampon, []
        return t

    def empiler(self, obs):
        with self._verrou:
            self._tampon.extend(obs)
            del self._tampon[:-400]

    def flux(self, duree=None):
        """Générateur utilisé par korko.lancer(--sim). Aussi vite que possible."""
        from korko import Observation
        fin = duree if duree is not None else self.duree_prevue
        while self.t < fin:
            for o in self.pas():
                yield Observation(o["t"], o["station"], o["balise"], o["rssi"])
        for _ in range(40):
            yield None

    def etat(self):
        rejeu = self.mode == "rejeu"
        return {
            "t": round(self.t, 1),
            "mode": self.mode,
            "source": self.source,
            "titre": self.titre,
            "duree": self.duree,
            "termine": self.termine,
            "pause": self.en_pause,
            "vitesse": self.vitesse,
            "enregistre": self.enr["nom"] if self.enr else None,
            "verites": len(self.verite),
            "reglages": {"RSSI_1M": RSSI_1M, "EXPOSANT": EXPOSANT,
                         "SIGMA": SIGMA, "PERTE": PERTE},
            "balises": [{
                "id": b.id,
                "origine": b.origine,
                "rssi": b.dernier_rssi,
                "muet": (b.dernier_vu is None or self.t - b.dernier_vu > 20)
                        if rejeu else (not b.vivante),
                "distance": None if rejeu else round(b.distance, 1),
                "lieu": "—" if rejeu else b.lieu,
                "occlusion": b.occlusion,
                "envers": b.envers,
                "muette": not b.vivante,
            } for b in self.balises.values()],
        }


# --------------------------------------------------------------------------
# Le serveur de flux (le même contrat que le Pi)
# --------------------------------------------------------------------------

class Diffuseur:
    def __init__(self, port=PORT_FLUX):
        self.clients = []
        self.verrou = threading.Lock()
        self.port = port

    def demarrer(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", self.port))
        s.listen(16)

        def boucle():
            while True:
                c, _ = s.accept()
                with self.verrou:
                    self.clients.append(c)
        threading.Thread(target=boucle, daemon=True).start()

    def envoyer(self, obs):
        if not obs:
            return
        blob = ("".join(json.dumps(o) + "\n" for o in obs)).encode()
        with self.verrou:
            morts = []
            for c in self.clients:
                try:
                    c.sendall(blob)
                except OSError:
                    morts.append(c)
            for c in morts:
                self.clients.remove(c)
                try:
                    c.close()
                except OSError:
                    pass

    @property
    def nombre(self):
        with self.verrou:
            return len(self.clients)


# --------------------------------------------------------------------------
# La page de contrôle
# --------------------------------------------------------------------------

PAGE = r"""<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>KORKO — simulateur de station</title><style>
:root{color-scheme:light dark;--bg:#E9EEE8;--sf:#F8FAF5;--ink:#08211F;
--soft:#37524E;--rule:#BCCAC0;--teal:#0E5852;--sig:#E9A800;--dgr:#96311A}
@media(prefers-color-scheme:dark){:root{--bg:#0B1615;--sf:#152322;--ink:#E6EDE5;
--soft:#A5B9B3;--rule:#2D403D;--teal:#6FC4BA;--sig:#F5C13B;--dgr:#E68062}}
*{box-sizing:border-box}
body{margin:0 auto;max-width:1120px;background:var(--bg);color:var(--ink);
font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;padding:16px}
h1{font-size:17px;margin:0 0 2px}
h2{font-size:13px;color:var(--teal);margin:24px 0 8px;font-weight:600}
.sub{color:var(--soft);font-size:13px;margin:0 0 14px}
button{font:inherit;font-size:13px;padding:5px 10px;background:var(--sf);
color:var(--ink);border:1px solid var(--rule);cursor:pointer}
button:hover:not(:disabled){border-color:var(--teal)}
button:disabled{opacity:.4;cursor:not-allowed}
button.on{background:var(--sig);border-color:var(--sig);color:#3a2c00}
button.pr{background:var(--teal);border-color:var(--teal);color:var(--sf)}
input{font:inherit;font-size:13px;padding:4px 6px;background:var(--sf);
color:var(--ink);border:1px solid var(--rule)}
.ong{display:flex;border-bottom:1px solid var(--rule);margin-top:6px}
.ong button{border:1px solid transparent;border-bottom:none;background:none;
padding:9px 16px;font-size:14px;color:var(--soft);margin-bottom:-1px}
.ong button.act{background:var(--sf);border-color:var(--rule);
border-bottom:1px solid var(--sf);color:var(--ink);font-weight:600}
.pan{background:var(--sf);border:1px solid var(--rule);border-top:none;
padding:14px 16px}
.pan p{margin:0 0 10px;font-size:13px;color:var(--soft)}
.pan p b{color:var(--ink)}
.bar{display:flex;gap:16px;align-items:center;flex-wrap:wrap;
background:var(--sf);border:1px solid var(--rule);padding:9px 14px;margin-top:14px}
.t{font-variant-numeric:tabular-nums;font-size:19px;font-weight:600}
.etq{font-size:12px;color:var(--soft)}.etq b{color:var(--ink);font-weight:600}
.rg{display:flex;gap:8px;align-items:center;font-size:13px;color:var(--soft)}
.prog{height:4px;background:var(--rule)}
.prog i{display:block;height:100%;background:var(--teal)}
.lst{width:100%;border-collapse:collapse;font-size:13px}
.lst td{padding:7px 8px;border-bottom:1px solid var(--rule);vertical-align:middle}
.lst tr:last-child td{border-bottom:none}
.lst .nom{font-weight:600;color:var(--ink)}
.lst .meta{color:var(--soft);font-size:12px}
.lst td:last-child{text-align:right;white-space:nowrap}
.vide{color:var(--soft);font-size:13px;font-style:italic}
.note{margin:10px 0 0;font-size:13px;color:var(--soft);background:var(--sf);
border:1px solid var(--rule);border-left:3px solid var(--sig);padding:9px 13px}
.tag{display:inline-block;font-size:10px;font-weight:400;padding:1px 6px;
margin-left:7px;border:1px solid var(--rule);color:var(--soft);border-radius:2px}
.badge{display:inline-block;font-size:11px;padding:2px 7px;margin-right:5px;
background:var(--sig);color:#3a2c00;border-radius:2px}
.rien{color:var(--soft)}
table.pl{width:100%;border-collapse:collapse;margin-top:8px}
.pl th{text-align:left;font-size:11px;color:var(--teal);font-weight:600;
padding:4px 6px;border-bottom:1px solid var(--teal)}
.pl td{padding:5px 6px;border-bottom:1px solid var(--rule);vertical-align:middle}
.pl td button{margin:2px 4px 2px 0}
.id{font-weight:600}
.n{font-variant-numeric:tabular-nums;text-align:right;width:72px}
.faible{color:var(--dgr)}.muet{color:var(--soft);font-style:italic}
.lieu{color:var(--soft);font-size:13px;white-space:nowrap}
button.bsc i{display:inline-block;width:8px;height:8px;border-radius:50%;
margin-right:6px;background:var(--rule);border:1px solid var(--soft)}
button.bsc.on i{background:#c0392b;border-color:#8e2b20}
canvas{width:100%;height:132px;border:1px solid var(--rule);background:var(--sf);
margin-top:8px}
.leg{margin-top:12px;font-size:13px;color:var(--soft);background:var(--sf);
border:1px solid var(--rule);padding:12px 16px}
.leg b{color:var(--ink)}.leg p{margin:0 0 6px}
.leg ul{margin:0 0 12px;padding-left:18px}.leg li{margin-bottom:3px}
.leg ul:last-child{margin-bottom:0}
.pied{margin-top:22px;font-size:12px;color:var(--soft);
border-top:1px solid var(--rule);padding-top:10px}
code{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px}
</style></head><body>

<h1>Simulateur de station KORKO</h1>
<p class="sub">Le flux est servi sur <code>tcp://localhost:8420</code> quel que
soit le mode. Branchez votre algorithme avec <code>--source localhost:8420</code>.
<span id="cl"></span></p>

<div class="ong">
  <button id="o-manuel" class="act">Manuel</button>
  <button id="o-scen">Scénarios</button>
  <button id="o-trac">Mes traces</button>
</div>

<div class="pan" id="p-manuel">
  <p><b>Vous pilotez les planches vous-même</b>, avec les boutons du tableau
  plus bas. Rien ne se déclenche tout seul. C'est le mode pour fabriquer un cas
  tordu à la demande, ou pour montrer quelque chose à quelqu'un.</p>
  <button id="reprendre">Reprendre la main et remettre les planches au râtelier</button>
</div>

<div class="pan" id="p-scen" hidden>
  <p><b>Des situations prêtes à jouer</b>, avec leur vérité terrain. Elles se
  déroulent seules, et votre algorithme peut être noté dessus. Vous gardez la
  main sur les boutons pendant qu'un scénario tourne.</p>
  <table class="lst" id="l-scen"></table>
</div>

<div class="pan" id="p-trac" hidden>
  <p><b>Vos enregistrements</b>, rejoués à l'identique. Aucune simulation :
  ce qui sort est exactement ce qui avait été observé. Les mêmes fichiers
  s'utilisent en ligne de commande avec <code>--rejeu</code>.</p>
  <table class="lst" id="l-trac"></table>
  <p style="margin-top:12px">Dossier : <code id="dos"></code>
  &nbsp; <button id="rafraichir">Rafraîchir</button></p>
</div>

<div class="bar">
  <span class="t" id="temps">0 s</span>
  <span class="etq">mode <b id="e-mode"></b> · <span id="e-titre"></span></span>
  <button id="pause">Pause</button>
  <button id="recom">Recommencer</button>
  <label class="rg">vitesse
    <input type="range" id="vit" min="1" max="60" value="1" step="1">
    <span id="vitv">1x</span></label>
  <span style="flex:1"></span>
  <input id="nom" placeholder="nom de l'enregistrement" size="18">
  <button id="rec">Enregistrer</button>
</div>
<div class="prog"><i id="prog" style="width:0"></i></div>

<h2>Signal reçu par la station</h2>
<canvas id="cv" width="1200" height="260"></canvas>


<h2>Les planches</h2>
<table class="pl"><thead id="th"></thead><tbody id="tb"></tbody></table>
<p class="note" id="note" hidden></p>

<div class="leg" id="leg">
<p><b>Les quatre premiers boutons déplacent la planche.</b> Un clic donne une
destination, et la planche s'y rend à 1,2 m/s — le signal évolue
progressivement, comme sur le terrain.</p>
<ul>
<li><b>Part surfer</b> — elle quitte le râtelier et s'en va au large, hors de
portée. Un <code>DEPART</code> est inscrit dans la vérité terrain à l'instant
du clic : c'est ce que votre algorithme doit retrouver.</li>
<li><b>Revient</b> — elle revient vers le râtelier. Le <code>RETOUR</code> est
inscrit au moment où elle arrive, pas au moment du clic.</li>
<li><b>Sur le sable</b> — posée à neuf mètres, elle y reste. Aucun événement
n'est inscrit : ce n'est pas un départ, et c'est le piège principal.</li>
<li><b>Au râtelier</b> — raccrochée, sans rien inscrire. Sert à remettre la
scène en ordre entre deux essais.</li>
</ul>
<p><b>Les trois dernières lignes du tableau sont des planches d'une autre
station.</b> Elles sont chez elles, donc hors de portée : cette station ne les
entend jamais. <b>Arrive ici</b> simule quelqu'un qui rapporte une planche au
mauvais râtelier — une <code>ETRANGERE</code> est inscrite dans la vérité
terrain à son arrivée. Une planche appartient à un râtelier et doit y revenir :
c'est la seule règle imposée à l'usager.</p>
<p><b>Les trois interrupteurs</b> : ils restent allumés tant
qu'on ne les éteint pas, et n'inscrivent jamais rien dans la vérité terrain.
Aucun d'eux n'est un départ.</p>
<ul>
<li><b>Corps devant</b> — un corps mouillé entre la balise et la station.
Environ 18 dB en moins, autant qu'un éloignement de plusieurs mètres.</li>
<li><b>Planche à l'envers</b> — face contre terre, balise plaquée au sol.
Environ 5 dB en moins.</li>
<li><b>Planche muette</b> — la balise n'émet plus rien. Pile morte, ou planche
dans un coffre de voiture.</li>
</ul>
<p>La station connaît la liste de ses cinq planches : c'est une ligne de
configuration, pas une déduction faite sur le signal. Émettre un
<code>RETOUR</code> pour une planche qui n'est pas de chez elle compte comme
une erreur.</p>
</div>

<h2>Conditions de propagation</h2>
<div class="bar">
<label class="rg">bruit <input type="range" id="SIGMA" min="0" max="10" step="0.5">
<span id="SIGMAv"></span> dB</label>
<label class="rg">exposant <input type="range" id="EXPOSANT" min="1.6" max="4" step="0.1">
<span id="EXPOSANTv"></span></label>
<label class="rg">pertes <input type="range" id="PERTE" min="0" max="0.4" step="0.01">
<span id="PERTEv"></span></label>
<span class="etq" id="norg" hidden>sans effet en mode rejeu</span>
</div>

<p class="pied">Calibration Feasycom FSC-BP108B à -19,5 dBm, intervalle 1000 ms.
Puissance reçue à 1 m : <span id="a1m"></span> dBm — à corriger dans
<code>korko_sim.py</code> après mesure sur le râtelier.</p>

<script>
const COUL=["#0E5852","#B8823A","#96311A","#3C817B","#6a4fb5","#4d7d1f"];
const hist={};let idx={},btns={},modeVu=null,nbVu=-1;
const ETRANGER=[
 ["arrive","Arrive ici","Quelqu'un rapporte cette planche au mauvais râtelier. Inscrit une ETRANGERE à son arrivée."],
 ["sen_va","Repart chez elle","Elle quitte cette station et rentre à la sienne."]];
const DEPLACE=[
 ["partir","Part surfer","Quitte le râtelier et s'en va au large. Inscrit un DEPART dans la vérité terrain."],
 ["revenir","Revient","Revient vers le râtelier. Le RETOUR est inscrit à l'arrivée."],
 ["poser","Sur le sable","Posée à neuf mètres et y reste. Aucun événement : c'est le piège."],
 ["ranger","Au râtelier","Raccrochée, sans rien inscrire. Remet la scène en ordre."]];
const BASCULE=[
 ["corps","Corps devant","occlusion","Interrupteur. Un corps mouillé devant la balise : environ 18 dB en moins."],
 ["envers","Planche à l'envers","envers","Interrupteur. Face contre terre : environ 5 dB en moins."],
 ["muette","Planche muette","muette","Interrupteur. La balise n'émet plus rien du tout."]];

function q(a,b,c){return fetch("/cmd",{method:"POST",
  body:JSON.stringify({action:a,balise:b,valeur:c})});}
function vider(){for(const k in hist)delete hist[k];}

const ONG={manuel:["o-manuel","p-manuel"],scen:["o-scen","p-scen"],
           trac:["o-trac","p-trac"]};
function onglet(n){for(const k in ONG){
  document.getElementById(ONG[k][0]).classList.toggle("act",k===n);
  document.getElementById(ONG[k][1]).hidden=(k!==n);}}
for(const k in ONG)document.getElementById(ONG[k][0]).onclick=()=>{
  onglet(k);if(k!=="manuel")bibliotheque();};

function ligne(nom,meta,libelle,fn){
  const tr=document.createElement("tr"),a=document.createElement("td"),
        b=document.createElement("td");
  a.innerHTML='<div class="nom"></div><div class="meta"></div>';
  a.children[0].textContent=nom;a.children[1].textContent=meta;
  const bt=document.createElement("button");bt.className="pr";
  bt.textContent=libelle;bt.onclick=fn;b.appendChild(bt);
  tr.appendChild(a);tr.appendChild(b);return tr;
}
function bibliotheque(){
  fetch("/bibliotheque").then(r=>r.json()).then(d=>{
    document.getElementById("dos").textContent=d.dossier;
    const s=document.getElementById("l-scen");s.innerHTML="";
    d.scenarios.forEach(x=>s.appendChild(ligne(x.titre,
      x.id+" · "+Math.round(x.duree/60)+" min simulées",
      "Jouer",()=>{vider();q("scenario",null,x.id);})));
    const t=document.getElementById("l-trac");t.innerHTML="";
    if(!d.traces.length){
      const tr=document.createElement("tr");
      tr.innerHTML='<td class="vide">Aucune trace pour le moment. Lancez un '+
        'enregistrement depuis la barre ci-dessous, puis revenez ici.</td>';
      t.appendChild(tr);return;}
    d.traces.forEach(x=>t.appendChild(ligne(x.fichier,
      x.observations+" observations · "+x.duree+" s · "+x.balises+" balises · "+
      (x.verites?x.verites+" événements de vérité":"pas de vérité terrain"),
      "Rejouer",()=>{vider();q("trace",null,x.fichier);})));
  });
}
document.getElementById("rafraichir").onclick=bibliotheque;
document.getElementById("reprendre").onclick=()=>{vider();q("manuel");};
document.getElementById("recom").onclick=()=>{vider();q("recommencer");};
document.getElementById("pause").onclick=()=>q("pause");
const vit=document.getElementById("vit");
vit.oninput=()=>{document.getElementById("vitv").textContent=vit.value+"x";
  q("vitesse",null,vit.value);};

document.getElementById("rec").onclick=()=>{
  const b=document.getElementById("rec");
  if(b.classList.contains("on")){
    fetch("/enregistrement",{method:"POST",body:JSON.stringify({action:"stop"})})
      .then(r=>r.json()).then(i=>{
        if(i.fichier)alert("Enregistré : "+i.fichier+"\n"+i.observations+
          " observations, "+i.verites+" événements de vérité, "+i.duree+" s.");
        bibliotheque();});
  }else{
    fetch("/enregistrement",{method:"POST",body:JSON.stringify(
      {action:"start",nom:document.getElementById("nom").value})});
  }};

for(const n of ["SIGMA","EXPOSANT","PERTE"]){
  const el=document.getElementById(n);
  el.oninput=()=>{document.getElementById(n+"v").textContent=el.value;
    q("reglage",null,{nom:n,valeur:el.value});};
}

const es=new EventSource("/flux");
let dernierT=0,derniereScene=null;
es.onmessage=m=>{
  const d=JSON.parse(m.data);
  // changement de scène : l'historique affiché ne correspond plus à rien.
  // On compare la scène elle-même, et pas seulement le temps : à vitesse
  // élevée, deux images se suivent de plusieurs dizaines de secondes.
  const scene=d.etat.mode+"/"+d.etat.source;
  if(scene!==derniereScene||d.etat.t<dernierT-1)vider();
  derniereScene=scene;
  dernierT=d.etat.t;
  for(const o of d.obs){
    if(o.t>d.etat.t+1)continue;      // reliquat de la scène précédente
    (hist[o.balise]=hist[o.balise]||[]).push([o.t,o.rssi]);
    if(hist[o.balise].length>900)hist[o.balise].shift();}
  maj(d.etat);dessiner(d.etat.t);
};

function maj(e){
  document.getElementById("temps").textContent=Math.round(e.t)+" s";
  document.getElementById("cl").textContent=e.clients+" client(s) branché(s).";
  document.getElementById("e-mode").textContent=
    {manuel:"manuel",scenario:"scénario",rejeu:"rejeu"}[e.mode];
  document.getElementById("e-titre").textContent=
    e.titre+(e.termine?" — terminé":"")+" · "+e.verites+" événements de vérité";
  document.getElementById("prog").style.width=
    e.duree?Math.min(100,100*e.t/e.duree)+"%":"0";
  const p=document.getElementById("pause");
  p.classList.toggle("on",e.pause);p.textContent=e.pause?"Reprendre":"Pause";
  const r=document.getElementById("rec");
  r.classList.toggle("on",!!e.enregistre);
  r.textContent=e.enregistre?"Arrêter l'enregistrement":"Enregistrer";
  document.getElementById("nom").disabled=!!e.enregistre;
  document.getElementById("a1m").textContent=e.reglages.RSSI_1M;
  for(const n of ["SIGMA","EXPOSANT","PERTE"]){
    const el=document.getElementById(n);
    if(document.activeElement!==el){el.value=e.reglages[n];
      document.getElementById(n+"v").textContent=e.reglages[n];}}
  document.getElementById("norg").hidden=(e.mode!=="rejeu");
  if(modeVu!==e.mode||nbVu!==e.balises.length){
    modeVu=e.mode;nbVu=e.balises.length;construire(e);}
  for(const b of e.balises){
    const rs=document.getElementById("r_"+b.id),
          ds=document.getElementById("d_"+b.id),
          ls=document.getElementById("l_"+b.id),
          es_=document.getElementById("e_"+b.id);
    if(!rs)continue;
    if(b.muet){rs.textContent="silence";rs.className="n muet";}
    else if(b.rssi===null){rs.textContent="—";rs.className="n muet";}
    else{rs.textContent=b.rssi;rs.className="n"+(b.rssi<-92?" faible":"");}
    if(ds)ds.textContent=b.distance===null?"—":b.distance+" m";
    if(ls)ls.textContent=b.lieu;
    if(es_){
      const etats=[];
      if(b.occlusion)etats.push("corps devant");
      if(b.envers)etats.push("à l'envers");
      if(b.muette)etats.push("muette");
      if(etats.length){es_.innerHTML="";
        etats.forEach(x=>{const sp=document.createElement("span");
          sp.className="badge";sp.textContent=x;es_.appendChild(sp);});}
      else{es_.innerHTML='<span class="rien">—</span>';}
    }
    const m=btns[b.id];if(!m||!m.occlusion)continue;
    m.occlusion.classList.toggle("on",b.occlusion);
    m.envers.classList.toggle("on",b.envers);
    m.muette.classList.toggle("on",b.muette);
  }
}

function construire(e){
  const manuel=e.mode==="manuel",rejeu=e.mode==="rejeu";
  const th=document.getElementById("th");th.innerHTML="";
  const tr0=document.createElement("tr");
  let cols='<th>Balise</th><th class="n">RSSI</th>';
  if(!rejeu)cols+='<th class="n">Distance</th><th>Où elle est</th>';
  cols+=manuel?'<th>Ce qu\'on lui fait faire</th><th>État permanent</th>'
              :'<th>État</th>';
  tr0.innerHTML=cols;th.appendChild(tr0);

  const tb=document.getElementById("tb");tb.innerHTML="";btns={};
  e.balises.forEach((b,i)=>{idx[b.id]=i;btns[b.id]={};
    const tr=document.createElement("tr");
    let h='<td class="id" style="color:'+COUL[i%6]+'"></td>'+
          '<td class="n" id="r_'+b.id+'"></td>';
    if(!rejeu)h+='<td class="n" id="d_'+b.id+'"></td>'+
                 '<td class="lieu" id="l_'+b.id+'"></td>';
    h+=manuel?'<td></td><td></td>':'<td id="e_'+b.id+'"></td>';
    tr.innerHTML=h;
    tr.children[0].textContent=b.id;
    if(b.origine!=="A"){
      const tg=document.createElement("span");tg.className="tag";
      tg.textContent="station "+b.origine;tr.children[0].appendChild(tg);}
    if(manuel){
      const tdA=tr.children[4],tdB=tr.children[5];
      (b.origine==="A"?DEPLACE:ETRANGER).forEach(([act,lab,aide])=>{
        const bt=document.createElement("button");bt.textContent=lab;
        bt.title=aide;bt.onclick=()=>q(act,b.id);tdA.appendChild(bt);});
      BASCULE.forEach(([act,lab,clef,aide])=>{
        const bt=document.createElement("button");bt.className="bsc";
        bt.title=aide;bt.onclick=()=>q(act,b.id);
        bt.innerHTML='<i></i>';bt.appendChild(document.createTextNode(lab));
        btns[b.id][clef]=bt;tdB.appendChild(bt);});
    }
    tb.appendChild(tr);});

  document.getElementById("leg").hidden=!manuel;
  const note=document.getElementById("note");
  note.hidden=manuel;
  note.textContent=rejeu
    ? "Rejeu d'un fichier : rien n'est simulé, les observations sortent telles "+
      "qu'elles ont été enregistrées. Passez en Manuel pour piloter les planches."
    : "Scénario en cours : le déroulement est scripté. Passez en Manuel pour "+
      "reprendre la main sur les planches.";
}

const cv=document.getElementById("cv"),cx=cv.getContext("2d");
function dessiner(t){
  const W=cv.width,H=cv.height,T0=Math.max(0,t-240),T1=Math.max(240,t);
  const hi=-55,lo=-105;
  cx.clearRect(0,0,W,H);
  cx.strokeStyle="#8a988f";cx.lineWidth=1;cx.font="16px system-ui";
  cx.fillStyle="#8a988f";
  for(let v=-60;v>=-100;v-=20){
    const y=H*(hi-v)/(hi-lo);cx.globalAlpha=.3;cx.beginPath();
    cx.moveTo(34,y);cx.lineTo(W,y);cx.stroke();cx.globalAlpha=1;
    cx.fillText(v,2,y+5);}
  // une planche partie cesse d'émettre : entre son dernier paquet et le
  // premier de son retour, il n'y a rien à tracer. Sans cette coupure, le
  // graphe relie les deux et dessine une longue diagonale.
  const TROU=8;
  let i=0;
  for(const k in hist){
    // (idx[k]||i) était faux : l'index 0 de korko-01 est falsy, elle
    // héritait donc de la couleur de sa voisine.
    const c=(k in idx)?idx[k]:i;
    cx.strokeStyle=COUL[c%6];cx.lineWidth=2;cx.beginPath();
    let prec=null;
    for(const [tt,rr] of hist[k]){
      if(tt<T0||tt>T1)continue;
      const x=34+(W-34)*(tt-T0)/(T1-T0),y=H*(hi-rr)/(hi-lo);
      if(prec===null||tt-prec>TROU)cx.moveTo(x,y);else cx.lineTo(x,y);
      prec=tt;}
    cx.stroke();i++;}
}
bibliotheque();
</script></body></html>"""


def servir(sim, diffuseur):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _entete(self, ct, code=200):
            self.send_response(code)
            self.send_header("Content-Type", ct)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

        def _json(self, obj):
            self._entete("application/json")
            self.wfile.write(json.dumps(obj, ensure_ascii=False).encode())

        def do_GET(self):
            if self.path == "/":
                self._entete("text/html; charset=utf-8")
                self.wfile.write(PAGE.encode("utf-8"))
            elif self.path == "/bibliotheque":
                self._json(sim.bibliotheque())
            elif self.path == "/verite":
                self._json(sim.verite)
            elif self.path == "/flux":
                self._entete("text/event-stream")
                try:
                    while True:
                        time.sleep(0.2)
                        etat = sim.etat()
                        etat["clients"] = diffuseur.nombre
                        charge = json.dumps({"obs": sim.vider_tampon(),
                                             "etat": etat}, ensure_ascii=False)
                        self.wfile.write(b"data: " + charge.encode() + b"\n\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
            else:
                self._entete("text/plain", 404)

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            d = json.loads(self.rfile.read(n) or b"{}")
            if self.path == "/enregistrement":
                if d.get("action") == "start":
                    sim.enr_debut(d.get("nom"))
                    self._json({"ok": True})
                else:
                    self._json(sim.enr_fin() or {"ok": False})
                return
            try:
                sim.cmd(d.get("action"), d.get("balise"), d.get("valeur"))
                self._json({"ok": True})
            except Exception as e:                       # noqa: BLE001
                self._json({"ok": False, "erreur": str(e)})

    srv = ThreadingHTTPServer(("0.0.0.0", PORT_PAGE), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def principal():
    sim = Simulateur()
    diffuseur = Diffuseur()
    diffuseur.demarrer()
    servir(sim, diffuseur)
    os.makedirs(DOSSIER, exist_ok=True)

    print("  simulateur KORKO")
    print("  page de contrôle : http://localhost:%d" % PORT_PAGE)
    print("  flux korko       : tcp://localhost:%d" % PORT_FLUX)
    print("  traces           : %s" % DOSSIER)
    print("  (Ctrl-C pour arrêter)")

    prochain = time.monotonic()
    try:
        while True:
            if not sim.en_pause:
                obs = sim.pas()
                if obs:
                    diffuseur.envoyer(obs)
                    sim.empiler(obs)
            prochain += PAS / max(sim.vitesse, 0.01)
            retard = prochain - time.monotonic()
            if retard > 0:
                time.sleep(retard)
            else:
                prochain = time.monotonic()
    except KeyboardInterrupt:
        print("\n  arrêt.")


if __name__ == "__main__":
    principal()
