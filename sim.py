#!/usr/bin/env python3
"""KORKO mini — simulateur radio d'UNE station (bibliothèque standard seulement).

Diffuse en TCP (NDJSON) des mesures RSSI bruitées pour les 6 balises,
et propose une page de contrôle pour déplacer les planches.

    python3 sim.py                    # station A, flux sur 8421, contrôle sur http://localhost:8081
    python3 sim.py --vitesse 10       # le temps simulé passe 10x plus vite

Message émis :  {"t": 1727.0, "station": "A", "balise": "korko-01", "rssi": -58}
Tick d'horloge: {"t": 1727.0, "station": "A"}   (chaque seconde simulée)
"""
import argparse, json, random, socket, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

BALISES = [f"korko-0{i}" for i in range(1, 7)]
MAISON = {"korko-01": "A", "korko-02": "A", "korko-03": "B",
          "korko-04": "B", "korko-05": "C", "korko-06": "C"}

# Profil radio de chaque position : (rssi moyen, écart, probabilité de perte du paquet)
# Calé sur les mesures de la maquette : -56 dBm au râtelier, -68 dBm à ~2 m.
PROFILS = {
    "ici":    (-56, 3, 0.15),   # accrochée au râtelier
    "sable":  (-63, 2, 0.25),   # posée à ~1 m (zone morte : ni départ, ni retour)
    "2m":     (-69, 3, 0.30),   # à ~2 m : doit déclencher un départ
    "masque": (-66, 4, 0.50),   # un corps mouillé devant la balise, 15 s
    "loin":   (None, 0, 1.00),  # à l'eau : silence (hors d'un paquet parasite rare)
}

etat = {}          # balise -> position
fin_masque = {}    # balise -> t de fin du masquage
clients = []
verrou = threading.Lock()
horloge = {"t": 0.0}


def diffuser(msg):
    ligne = (json.dumps(msg) + "\n").encode()
    with verrou:
        for c in clients[:]:
            try:
                c.sendall(ligne)
            except OSError:
                clients.remove(c)
                print(f"[sim] client débranché ({len(clients)} branché(s))")


def boucle_radio(station, vitesse):
    t = time.time()
    while True:
        time.sleep(1.0 / vitesse)
        t += 1.0
        horloge["t"] = t
        diffuser({"t": round(t, 1), "station": station})
        for b in BALISES:
            pos = etat[b]
            if pos == "masque" and t >= fin_masque.get(b, 0):
                etat[b] = pos = "ici"
            moy, ecart, perte = PROFILS[pos]
            if moy is None:
                if random.random() < 0.03:   # paquet parasite très faible
                    diffuser({"t": round(t, 1), "station": station, "balise": b, "rssi": random.randint(-99, -92)})
                continue
            if random.random() < perte:
                continue
            rssi = moy + random.gauss(0, ecart)
            if random.random() < 0.03:       # évanouissement brutal ponctuel
                rssi -= random.randint(6, 12)
            diffuser({"t": round(t, 1), "station": station, "balise": b, "rssi": round(rssi)})


def serveur_flux(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", port))
    s.listen()
    while True:
        c, _ = s.accept()
        with verrou:
            clients.append(c)
        print(f"[sim] {len(clients)} client(s) branché(s)")


PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width">
<title>Simulateur KORKO</title><meta http-equiv=refresh content=3>
<style>body{font-family:system-ui;margin:16px}td,th{padding:6px 10px;border-bottom:1px solid #ccc}
a{margin-right:8px}</style><h2>Simulateur — station %s</h2><p>%d client(s) branché(s) · t = %.0f</p>
<table><tr><th>Balise</th><th>Maison</th><th>Position</th><th>Actions</th></tr>%s</table>
<p><small>ici = accrochée · sable = posée à ~1 m · 2m = à 2 m · loin = à l'eau · masque = un corps devant pendant 15 s</small></p>"""


class Controle(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/etat":
            data = json.dumps({"station": self.server.station, "t": horloge["t"], "positions": etat,
                               "clients": len(clients), "maison": MAISON}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
            self.wfile.write(data); return
        if u.path == "/bouge":
            b, pos = q["b"][0], q["pos"][0]
            if b in etat and pos in PROFILS:
                etat[b] = pos
                if pos == "masque":
                    fin_masque[b] = horloge["t"] + 15
                print(f"[sim] {b} -> {pos}")
            self.send_response(303); self.send_header("Location", "/"); self.end_headers(); return
        lignes = "".join(
            f"<tr><td>{b}</td><td>{MAISON[b]}</td><td><b>{etat[b]}</b></td><td>"
            + "".join(f'<a href="/bouge?b={b}&pos={p}">{p}</a>' for p in PROFILS) + "</td></tr>"
            for b in BALISES)
        html = PAGE % (self.server.station, len(clients), horloge["t"], lignes)
        self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers()
        self.wfile.write(html.encode())


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--station", default="A")
    ap.add_argument("--port", type=int, default=8421, help="port du flux radio")
    ap.add_argument("--web", type=int, default=8081, help="port de la page de contrôle")
    ap.add_argument("--vitesse", type=float, default=1.0)
    a = ap.parse_args()
    for b in BALISES:
        etat[b] = "ici" if MAISON[b] == a.station else "loin"
    threading.Thread(target=serveur_flux, args=(a.port,), daemon=True).start()
    threading.Thread(target=boucle_radio, args=(a.station, a.vitesse), daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", a.web), Controle)
    srv.station = a.station
    print(f"[sim] station {a.station} · flux radio sur :{a.port} · contrôle sur http://localhost:{a.web}")
    srv.serve_forever()
