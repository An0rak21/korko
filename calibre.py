#!/usr/bin/env python3
"""KORKO mini — test de distance : mesure le RSSI d'une balise à plusieurs distances
et propose SEUIL_HAUT / SEUIL_BAS pour station.py.

    # sur la maquette (Wi-Fi de la maquette), station A :
    python3 calibre.py --source 192.168.8.100:8420 --balise korko-01

    # avec le simulateur :
    python3 calibre.py --source localhost:8421 --balise korko-01

Déroulé : pour chaque distance (par défaut 0.5, 1, 2, 3, 5 m), tu places la balise,
tu appuies sur Entrée, le script écoute DUREE secondes puis affiche les statistiques.
À la fin : tableau récapitulatif, seuils conseillés, et un fichier CSV avec toutes les mesures.
"""
import argparse, csv, json, math, queue, socket, statistics, threading, time

ALPHA = 0.3   # même lissage que station.py


def lire(source, file_msgs):
    hote, port = source.rsplit(":", 1)
    while True:
        try:
            with socket.create_connection((hote, int(port)), timeout=30) as c:
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
                                file_msgs.put(json.loads(ligne))
                            except ValueError:
                                pass
        except OSError as e:
            print(f"  (source injoignable : {e}, nouvel essai dans 2 s)")
            time.sleep(2)


def id_balise(m):
    return m.get("balise") or m.get("id") or m.get("beacon") or m.get("nom")


def mesurer(file_msgs, balise, duree):
    """Écoute `duree` secondes (horloge = champ t des messages) et renvoie les mesures."""
    while not file_msgs.empty():          # on jette ce qui est arrivé pendant qu'on déplaçait la balise
        file_msgs.get_nowait()
    brut, lisse, t0, t, ema, dernier_affichage = [], [], None, None, None, None
    while t is None or t - t0 < duree:
        try:
            m = file_msgs.get(timeout=5)
        except queue.Empty:
            print("  aucun message reçu depuis 5 s : vérifie --source")
            continue
        if "t" not in m:
            continue
        t = float(m["t"])
        if t0 is None:
            t0 = t
        if id_balise(m) == balise and m.get("rssi") is not None:
            r = float(m["rssi"])
            ema = r if ema is None else ALPHA * r + (1 - ALPHA) * ema
            brut.append((t - t0, r))
            lisse.append(ema)
        if dernier_affichage is None or t - dernier_affichage >= 2:
            dernier_affichage = t
            etat = f"lissé {ema:6.1f} dBm" if ema is not None else "pas encore entendue"
            print(f"  {t - t0:4.0f}/{duree} s · {len(brut):3d} paquets · {etat}")
    return brut, lisse, duree


def stats(brut, lisse, duree):
    if not brut:
        return None
    valeurs = [r for _, r in brut]
    return {
        "paquets": len(valeurs),
        "reception": len(valeurs) / duree,                # paquets par seconde
        "mediane": statistics.median(valeurs),
        "moyenne": statistics.mean(valeurs),
        "ecart": statistics.pstdev(valeurs),
        "min": min(valeurs), "max": max(valeurs),
        "lisse_min": min(lisse), "lisse_max": max(lisse),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="192.168.8.100:8420", help="hote:port du flux radio")
    ap.add_argument("--balise", default="korko-01")
    ap.add_argument("--distances", default="0.5,1,2,3,5", help="distances en mètres, séparées par des virgules")
    ap.add_argument("--duree", type=int, default=30, help="secondes d'écoute par distance")
    ap.add_argument("--cible", type=float, default=2.0, help="distance de départ voulue (m)")
    ap.add_argument("--csv", default="calibration.csv")
    a = ap.parse_args()

    distances = [float(x) for x in a.distances.split(",")]
    file_msgs = queue.Queue()
    threading.Thread(target=lire, args=(a.source, file_msgs), daemon=True).start()
    print(f"Test de distance · balise {a.balise} · source {a.source} · {a.duree} s par distance")
    print("Conseil : balise à hauteur du râtelier, orientée comme sur la planche, personne entre elle et le Pi.\n")

    resultats, lignes_csv = {}, []
    for d in distances:
        input(f"Place {a.balise} à {d:g} m de la station puis appuie sur Entrée… ")
        brut, lisse, duree = mesurer(file_msgs, a.balise, a.duree)
        s = stats(brut, lisse, duree)
        resultats[d] = s
        lignes_csv += [(d, round(t, 1), r) for t, r in brut]
        if s:
            print(f"  -> médiane {s['mediane']:.0f} dBm, écart {s['ecart']:.1f}, "
                  f"min {s['min']:.0f} / max {s['max']:.0f}, {s['reception']:.1f} paquet/s\n")
        else:
            print("  -> balise jamais entendue à cette distance\n")

    with open(a.csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["distance_m", "t_s", "rssi"])
        w.writerows(lignes_csv)

    print("\n Distance | Médiane | Écart | Lissé min..max | Paquets/s")
    print("----------+---------+-------+----------------+----------")
    for d, s in resultats.items():
        if s:
            print(f" {d:6.1f} m | {s['mediane']:5.0f}   | {s['ecart']:5.1f} | "
                  f"{s['lisse_min']:5.0f} .. {s['lisse_max']:4.0f}  | {s['reception']:5.1f}")
        else:
            print(f" {d:6.1f} m |   —     |   —   |       —        |   0.0")

    # Seuils conseillés, à partir de la distance cible
    mesurees = {d: s for d, s in resultats.items() if s}
    # Dans station.py : sous SEUIL_BAS (ou muette) pendant DELAI_DEPART -> DEPART ;
    # au-dessus de SEUIL_HAUT sans interruption pendant DELAI_RETOUR -> RETOUR ; entre les deux, rien.
    proches = [d for d in mesurees if d < a.cible]
    if a.cible in mesurees and proches:
        s = mesurees[a.cible]
        rack = mesurees[min(proches)]
        ecart_total = rack["mediane"] - s["mediane"]      # ex. -56 - (-68) = 12 dB
        haut = rack["mediane"] - ecart_total / 3          # ex. -60 : marge sous le signal du râtelier
        bas = s["mediane"] + ecart_total / 3              # ex. -64 : marge au-dessus du signal à la cible
        print(f"\nSeuils conseillés pour un départ vers {a.cible:g} m (à copier en tête de station.py) :")
        print(f"  SEUIL_HAUT = {round(haut)}   # plus fort que ça, en continu : RETOUR")
        print(f"  SEUIL_BAS  = {round(bas)}   # plus faible que ça pendant DELAI_DEPART : DEPART")
        if rack["mediane"] - s["mediane"] < 10:
            print(f"  Attention : seulement {rack['mediane'] - s['mediane']:.0f} dB entre {min(proches):g} m et {a.cible:g} m. "
                  "La marge est faible, un corps devant la balise peut suffire à la franchir.")
        if rack["lisse_min"] < bas:
            print(f"  Attention : au râtelier le signal lissé est descendu à {rack['lisse_min']:.0f} dBm, sous SEUIL_BAS. "
                  "Risque de faux départ : allonge DELAI_DEPART.")
        if s["ecart"] > 5:
            print(f"  Attention : écart de {s['ecart']:.1f} dB à {a.cible:g} m, frontière floue (de l'ordre du mètre).")
    else:
        print(f"\nIl faut une mesure à {a.cible:g} m et au moins une plus proche : ajuste --distances.")
    print(f"\nMesures brutes enregistrées dans {a.csv}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrompu.")
