#!/usr/bin/env python3
"""KORKO mini — la station : mesures radio -> DEPART / RETOUR / ETRANGERE -> cloud.

    python3 station.py --station A --source localhost:8421 --cloud http://localhost:9001

Règle (explicable en une phrase à un exploitant) :
  « Une planche est PARTIE quand son signal reste plus faible que SEUIL_BAS (ou muet) pendant
    DELAI_DEPART secondes, et REVENUE quand il reste plus fort que SEUIL_HAUT pendant DELAI_RETOUR
    secondes. Entre les deux seuils on ne change rien. Les heures enregistrées sont celles du
    dernier signal proche / du premier signal fort, pas celles de la décision. »

Seuils calibrés sur la maquette : -56 dBm au râtelier, -68 dBm à ~2 m (planche partie).

- Signal lissé (moyenne glissante) + deux seuils avec zone morte entre eux.
- Horloge = champ "t" des messages, jamais time.time() (compatible rejeu accéléré).
- File d'événements locale (fichier) : rien ne se perd si le cloud ou le réseau tombe.
"""
import argparse, json, os, socket, threading, time, urllib.request

MAISON = {"korko-01": "A", "korko-02": "A", "korko-03": "B",
          "korko-04": "B", "korko-05": "C", "korko-06": "C"}

ALPHA = 0.3            # lissage : poids de la nouvelle mesure
SEUIL_HAUT = -60       # plus fort que ça : au râtelier (mesuré -56). Compte pour un RETOUR
SEUIL_BAS = -64        # plus faible que ça : partie (mesuré -68 à ~2 m, 4 dB de marge). Compte pour un DEPART
                       # entre les deux : zone morte, l'état ne change pas
DELAI_DEPART = 25      # s sous SEUIL_BAS (ou muette) avant de déclarer un départ
DELAI_RETOUR = 20      # s au-dessus de SEUIL_HAUT sans interruption avant de déclarer un retour
RESET_LISSAGE = 10     # s sans paquet -> on oublie l'ancienne moyenne
DEMARRAGE = 15         # s d'observation au lancement avant de décider quoi que ce soit
VIE_TOUTES = 5         # s entre deux messages de vie envoyés au cloud


class Balise:
    def __init__(self):
        self.lisse = None
        self.dernier_paquet = None
        self.dernier_proche = None   # dernier instant où le signal lissé était >= SEUIL_BAS
        self.debut_serie = None      # début de la série continue >= SEUIL_HAUT
        self.presente = None         # None = inconnu (démarrage)


class Station:
    def __init__(self, nom, cloud):
        self.nom, self.cloud = nom, cloud
        self.balises = {}
        self.t0 = None
        self.t = None
        self.derniere_vie = None
        self.demarree = False
        self.seq = 0
        self.fichier_file = f"file_station_{nom}.ndjson"
        self.file = self._charger_file()
        self.verrou = threading.Lock()
        self.verrou_decision = threading.RLock()
        self.horloge_murale = None
        self.ignores = 0
        self.inconnues = set()

    # ---------- file d'événements persistante ----------
    def _charger_file(self):
        if not os.path.exists(self.fichier_file):
            return []
        with open(self.fichier_file) as f:
            return [json.loads(l) for l in f if l.strip()]

    def _sauver_file(self):
        with open(self.fichier_file, "w") as f:
            for e in self.file:
                f.write(json.dumps(e) + "\n")

    def emettre(self, type_, balise=None, t=None, **extra):
        self.seq += 1
        evt = {"id": f"{self.nom}-{int(self.t0)}-{self.seq}", "station": self.nom,
               "type": type_, "t": round(t if t is not None else self.t, 1)}
        if balise:
            evt["balise"] = balise
        evt.update(extra)
        if type_ != "VIE":
            detail = extra.get("raison", "") or ", ".join(extra.get("presentes", [])) or "aucune planche entendue"
            print(f"[station {self.nom}] {type_:10} {balise or ''} {detail}")
        with self.verrou:
            self.file.append(evt)
            if type_ != "VIE":
                self._sauver_file()

    def envoyer(self):
        """Thread : pousse la file au cloud, garde tout en cas d'échec."""
        while True:
            time.sleep(1)
            with self.verrou:
                lot = self.file[:50]
            if not lot:
                continue
            try:
                req = urllib.request.Request(self.cloud + "/evenements", data=json.dumps(lot).encode(),
                                             headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=3).read()
            except OSError:
                continue  # cloud injoignable : on réessaie, la file est sur disque
            with self.verrou:
                self.file = self.file[len(lot):]
                self._sauver_file()

    # ---------- décision ----------
    def mesure(self, msg):
        with self.verrou_decision:
            champs = extraire(msg)
            if champs is None:
                self.ignores += 1
                if self.ignores in (1, 10, 100) or self.ignores % 1000 == 0:
                    print(f"[station {self.nom}] message ignoré (n°{self.ignores}) : {str(msg)[:160]}")
                return
            t, b, rssi = champs
            if self.t0 is None:
                self.t0 = t
            if self.t is not None and t < self.t - 5:
                return                            # message en retard : on l'ignore
            self.t = max(self.t or t, t)
            self.horloge_murale = time.monotonic()
            if b and rssi is not None:
                if b in MAISON:
                    self._mesure_balise(t, b, rssi)
                elif b not in self.inconnues:
                    self.inconnues.add(b)
                    print(f"[station {self.nom}] appareil inconnu ignoré : {b} ({rssi:.0f} dBm)"
                          f" — si c'est une planche, ajoute --alias {b}=korko-0X")
            self.verifier()

    def avancer_horloge(self):
        """Mode direct seulement : si le Pi se tait (plus aucune balise entendue),
        on fait avancer l'horloge du flux du temps réellement écoulé, sinon aucun
        départ ne serait jamais déclaré quand toutes les planches sont parties."""
        while True:
            time.sleep(1)
            with self.verrou_decision:
                if self.t is None or self.horloge_murale is None:
                    continue
                ecoule = time.monotonic() - self.horloge_murale
                if ecoule >= 2:
                    self.t += ecoule
                    self.horloge_murale = time.monotonic()
                    self.verifier()

    def afficher(self):
        while True:
            time.sleep(5)
            with self.verrou_decision:
                if self.t is None:
                    print(f"[station {self.nom}] rien reçu pour l'instant")
                    continue
                for b in sorted(self.balises):
                    s = self.balises[b]
                    etat = {True: "présente", False: "partie", None: "?"}[s.presente]
                    lisse = f"{s.lisse:6.1f}" if s.lisse is not None else "   —  "
                    muet = self.t - s.dernier_paquet if s.dernier_paquet else None
                    print(f"   {b:12} lissé {lisse} dBm · {etat:8} · "
                          + (f"dernier paquet il y a {muet:.0f} s" if muet is not None else "jamais entendue"))

    def _mesure_balise(self, t, b, rssi):
        s = self.balises.setdefault(b, Balise())
        if s.dernier_paquet is None or t - s.dernier_paquet > RESET_LISSAGE:
            s.lisse = rssi
        else:
            s.lisse = ALPHA * rssi + (1 - ALPHA) * s.lisse
        s.dernier_paquet = t
        if s.lisse >= SEUIL_BAS:
            s.dernier_proche = t          # pas (encore) partie
        if s.lisse >= SEUIL_HAUT:
            if s.debut_serie is None:
                s.debut_serie = t
        else:
            s.debut_serie = None          # la série de retour doit être continue

    def verifier(self):
        t = self.t
        if not self.demarree:
            if t - self.t0 < DEMARRAGE:
                return
            self.demarree = True
            for b in MAISON:
                s = self.balises.setdefault(b, Balise())
                s.presente = s.dernier_proche is not None and t - s.dernier_proche < DELAI_DEPART
            presentes = sorted(b for b, s in self.balises.items() if s.presente)
            self.emettre("INVENTAIRE", presentes=presentes)
        for b, s in self.balises.items():
            if s.presente:
                loin_depuis = t - (s.dernier_proche or self.t0)
                if loin_depuis >= DELAI_DEPART:
                    s.presente, s.debut_serie = False, None
                    self.emettre("DEPART", b, t=s.dernier_proche or t,
                                 raison=f"sous {SEUIL_BAS} dBm depuis {loin_depuis:.0f} s")
            else:
                if s.debut_serie is not None and t - s.debut_serie >= DELAI_RETOUR:
                    s.presente = True
                    type_ = "RETOUR" if MAISON.get(b) == self.nom else "ETRANGERE"
                    self.emettre(type_, b, t=s.debut_serie,
                                 raison=f"au-dessus de {SEUIL_HAUT} dBm depuis {t - s.debut_serie:.0f} s")
        if self.derniere_vie is None or t - self.derniere_vie >= VIE_TOUTES:
            self.derniere_vie = t
            self.emettre("VIE", presentes=sorted(b for b, s in self.balises.items() if s.presente))


def extraire(msg):
    """Tolère plusieurs formats de message. Renvoie (t, balise, rssi) ou None si inutilisable."""
    if not isinstance(msg, dict):
        return None
    for cle in ("data", "mesure", "payload"):           # message imbriqué
        if isinstance(msg.get(cle), dict):
            msg = {**msg, **msg[cle]}
    t = next((msg[k] for k in ("t", "ts", "time", "timestamp") if msg.get(k) is not None), None)
    try:
        t = float(t)
    except (TypeError, ValueError):
        return None
    if t > 1e11:                                        # millisecondes -> secondes
        t /= 1000.0
    b = next((msg[k] for k in ("balise", "id", "beacon", "nom", "name", "mac", "addr", "address")
              if msg.get(k)), None)
    if b is not None:
        b = ALIAS.get(str(b).lower(), str(b))
    rssi = next((msg[k] for k in ("rssi", "RSSI", "signal") if msg.get(k) is not None), None)
    try:
        rssi = float(rssi) if rssi is not None else None
    except (TypeError, ValueError):
        rssi = None
    return t, b, rssi


ALIAS = {}   # adresse MAC (minuscules) -> nom korko-0X, rempli par --alias


def lire_source(station, source):
    hote, port = source.rsplit(":", 1)
    while True:
        try:
            with socket.create_connection((hote, int(port)), timeout=30) as c:
                print(f"[station {station.nom}] branchée sur {source}")
                tampon = b""
                while True:
                    bloc = c.recv(4096)
                    if not bloc:
                        break
                    tampon += bloc
                    while b"\n" in tampon:
                        ligne, tampon = tampon.split(b"\n", 1)
                        if ligne.strip():
                            try:
                                msg = json.loads(ligne)
                            except ValueError:
                                msg = ligne.decode(errors="replace")
                            station.mesure(msg)
        except OSError as e:
            print(f"[station {station.nom}] source injoignable ({e}), nouvel essai dans 2 s")
            time.sleep(2)


def rejouer(station, fichier):
    with open(fichier) as f:
        for ligne in f:
            if ligne.strip():
                try:
                    station.mesure(json.loads(ligne))
                except ValueError:
                    pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--station", default="A")
    ap.add_argument("--source", default="localhost:8421", help="hote:port du flux radio")
    ap.add_argument("--cloud", default="http://localhost:9001")
    ap.add_argument("--rejeu", help="fichier NDJSON à rejouer au lieu du flux")
    ap.add_argument("--verbose", action="store_true", help="affiche le signal de chaque balise toutes les 5 s")
    ap.add_argument("--alias", default="", help="ex. aa:bb:cc:dd:ee:01=korko-01,aa:bb:...=korko-02")
    a = ap.parse_args()
    for paire in filter(None, a.alias.split(",")):
        mac, nom = paire.split("=")
        ALIAS[mac.strip().lower()] = nom.strip()
    st = Station(a.station, a.cloud)
    threading.Thread(target=st.envoyer, daemon=True).start()
    if a.verbose:
        threading.Thread(target=st.afficher, daemon=True).start()
    if a.rejeu:
        rejouer(st, a.rejeu)
        time.sleep(3)
    else:
        threading.Thread(target=st.avancer_horloge, daemon=True).start()
        lire_source(st, a.source)
