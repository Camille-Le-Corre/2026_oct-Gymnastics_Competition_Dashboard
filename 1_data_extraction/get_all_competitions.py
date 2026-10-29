"""List the competitions of ufolepgym.com/plan_archives.htm.

Used by extract_ufolepgym.py. Run alone, it writes data/etape1/competitions.csv and .json
to check the list before loading it, without touching the database.
"""

import argparse
import csv
import hashlib
import json
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, Comment, NavigableString, Tag

URL_PLAN = "https://ufolepgym.com/ufolepgym/plan_archives.htm"

MOIS = {
    "janvier": 1, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6,
    "juillet": 7, "aout": 8, "septembre": 9, "octobre": 10, "novembre": 11, "decembre": 12,
}
RE_SAISON = re.compile(r"^(\d{4})\s*[-–]\s*(\d{4})$")
RE_FLIPSALTO = re.compile(r"competition\.flipsalto\.fr/public/competitions/(\d+)/palmares", re.I)
RE_ANNEXE = re.compile(r"agres|photo|masculin")  # on normalized text

COLONNES = [
    "id_evenement", "saison", "phase", "phase_brut", "groupe", "date_brute",
    "date_debut", "date_fin", "mois", "annee", "ville", "departement", "region",
    "libelle", "gam_dans_libelle", "source", "id_flipsalto", "url_principale",
    "autres_liens", "nb_evenements_meme_url", "avertissements",
]


def normaliser(s):
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def nettoyer(s):
    return re.sub(r"\s+", " ", s.replace("\xa0", " ").replace("\u202f", " ")).strip()


def decoder(contenu: bytes) -> str:
    # the page mixes UTF-8 and cp1252 bytes, so it is decoded line by line
    sortie = []
    for ligne in contenu.split(b"\n"):
        try:
            sortie.append(ligne.decode("utf-8"))
        except UnicodeDecodeError:
            sortie.append(ligne.decode("cp1252", errors="replace"))
    return "\n".join(sortie)


def telecharger(url, cache: Path, refresh: bool) -> str:
    if cache.exists() and not refresh:
        print(f"Cache utilisé : {cache}")
        return decoder(cache.read_bytes())
    print(f"Téléchargement : {url}")
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (pipeline-ufolep)"}, timeout=60)
    r.raise_for_status()
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(r.content)
    return decoder(r.content)


def classer_section(titre):
    # "Régionaux G2" -> ("Régionale", "G2"), "Finales jeunes" -> ("Finale jeunes", None)
    t = normaliser(titre)
    m = re.search(r"\bg([1-4])\b", t)
    groupe = f"G{m.group(1)}" if m else None
    if "jeune" in t:
        phase = "Finale jeunes"
    elif "1/2" in t or "demi" in t:
        phase = "Demi-finale"
    elif "final" in t:
        phase = "Finale"
    elif "region" in t:
        phase = "Régionale"
    elif "depart" in t:
        phase = "Départementale"
    else:
        phase = None
    return phase, groupe


def contient_mois(texte):
    return any(tok in MOIS for tok in re.findall(r"[a-z]+", normaliser(texte)))


def parser_dates(texte, saison):
    # "8 et 9 juin 2025", "1er et 2 avril", "31 mai et 1er juin 2025"...
    # The year always comes from the season, which fixes typos like "décembre 2025" in 2024-2025
    debut_saison, fin_saison = map(int, saison.split("-"))
    jours, couples, annee_texte = [], [], None
    for tok in re.findall(r"\d{4}|\d{1,2}|[a-z]+", normaliser(texte)):
        if tok.isdigit():
            if len(tok) == 4:
                annee_texte = int(tok)
            else:
                jours.append(int(tok))
        elif tok in MOIS:
            couples.extend((j, MOIS[tok]) for j in jours)
            jours = []

    avert, dates = [], []
    for j, m in couples:
        try:
            dates.append(date(debut_saison if m >= 9 else fin_saison, m, j))
        except ValueError:
            avert.append(f"date invalide : {j}/{m}")
    if not dates:
        return {"date_debut": "", "date_fin": "", "mois": "", "annee": "",
                "avert": avert + [f"date illisible : '{texte}'"]}

    d0, d1 = min(dates), max(dates)
    if annee_texte is not None and annee_texte != d0.year:
        avert.append(f"année du texte ({annee_texte}) corrigée en {d0.year} d'après la saison")
    return {"date_debut": d0.isoformat(), "date_fin": d1.isoformat(),
            "mois": d0.month, "annee": d0.year, "avert": avert}


def lire_cellule(cell, base_url):
    lignes = [{"texte": "", "liens": []}]
    vus = {}
    for node in cell.descendants:
        if isinstance(node, Tag):
            if node.name == "br":
                lignes.append({"texte": "", "liens": []})
        elif isinstance(node, NavigableString) and not isinstance(node, Comment):
            lignes[-1]["texte"] += str(node)
            for parent in node.parents:
                if parent is cell:
                    break
                if parent.name == "a" and parent.get("href"):
                    cle = id(parent)
                    if cle not in vus:
                        vus[cle] = {"href": urljoin(base_url, parent["href"].strip()), "texte": ""}
                        lignes[-1]["liens"].append(vus[cle])
                    vus[cle]["texte"] += str(node)
                    break
    # merge anchors pointing to the same url ("Château-Renault (" + ")")
    for l in lignes:
        fusion = {}
        for lien in l["liens"]:
            fusion.setdefault(lien["href"], {"href": lien["href"], "texte": ""})["texte"] += lien["texte"]
        l["liens"] = list(fusion.values())
    return lignes


def role_lien(texte):
    t = normaliser(texte)
    if "photo" in t:
        return "photo"
    if "agres" in t or "masculin" in t:
        return "annexe"
    if "samedi" in t:
        return "samedi"
    if "dimanche" in t:
        return "dimanche"
    return "principale"


def canoniser(url):
    m = RE_FLIPSALTO.search(url)
    if m:
        return f"https://competition.flipsalto.fr/public/competitions/{m.group(1)}/palmares"
    return url


def source_de(url):
    if not url:
        return "aucune"
    if RE_FLIPSALTO.search(url):
        return "flipsalto"
    if "ufolepgym.com" in url:
        return "ufolepgym_statique"
    return "autre"


def parser_lieu(texte):
    # "Wavrin (G1)", "Haubourdin (Hauts de France)", "LUC (59)", "78 - Fontenay..."
    principal = texte.split("+")[0].strip()
    ville, precision, dept = principal, "", ""
    m = re.match(r"^(\d{2,3})\s*-\s*(.+)$", principal)
    if m:
        dept, ville = m.group(1), m.group(2).strip()
    else:
        m = re.match(r"^(.*?)\s*\(([^)]*)\)?", principal)
        if m:
            ville, precision = m.group(1).strip(), m.group(2).strip()

    groupe, region = None, ""
    pn = normaliser(precision)
    gs = re.findall(r"g[1-4]", pn)
    if gs:
        groupe = "/".join(g.upper() for g in gs)
    elif precision and re.fullmatch(r"[\d\s\-/]+", pn):
        dept = precision
    elif precision:
        region = precision
    return ville, dept, region, groupe


def id_stable(*parties):
    return hashlib.sha1("|".join(str(p) for p in parties).encode()).hexdigest()[:10]


def analyser(html, base_url):
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    rows = [tr for tr in soup.find_all("tr") if not tr.find("tr")]  # innermost rows only
    evenements = []
    saison = phase_brut = phase = groupe_section = None
    derniere = None
    dates_courantes = None

    def nouvel_evenement(texte, liens, dates, avert_extra=None):
        ville, dept, region, groupe_ligne = parser_lieu(texte)
        avert = list(dates["avert"]) + (avert_extra or [])
        if phase is None:
            avert.append(f"section inconnue : '{phase_brut}'")
        liens_ok = [dict(role=role_lien(l["texte"]), url=canoniser(l["href"]), texte=nettoyer(l["texte"]))
                    for l in liens]
        liens_ok = [l for l in liens_ok if l["role"] != "photo"]
        return {
            "saison": saison, "phase": phase or "", "phase_brut": phase_brut or "",
            "groupe": groupe_ligne or groupe_section or "",
            "date_brute": dates.get("brute", ""), "date_debut": dates["date_debut"],
            "date_fin": dates["date_fin"], "mois": dates["mois"], "annee": dates["annee"],
            "ville": ville, "departement": dept, "region": region, "libelle": texte,
            "gam_dans_libelle": "gam" in normaliser(texte),
            "_liens": liens_ok, "_avert": avert,
        }

    for tr in rows:
        cells = tr.find_all(["td", "th"], recursive=False)
        if not cells:
            continue
        textes = [nettoyer(c.get_text(" ")) for c in cells]
        ensemble = nettoyer(" ".join(textes))

        # season title (the navigation menu is made of "#..." links)
        m = RE_SAISON.match(ensemble)
        if m:
            ancres = tr.find_all("a", href=True)
            if not (ancres and all(a["href"].startswith("#") for a in ancres)):
                saison = f"{m.group(1)}-{m.group(2)}"
                phase_brut = phase = groupe_section = None
                derniere = dates_courantes = None
            continue
        if saison is None or len(cells) < 2:
            continue

        t1 = textes[0]
        col2_vide = not textes[1] and not cells[1].find("a", href=True)

        # section row: "Régionaux G1", "Finales jeunes"...
        if t1 and not contient_mois(t1) and col2_vide:
            phase_brut = t1
            phase, groupe_section = classer_section(t1)
            derniere = dates_courantes = None
            continue

        if col2_vide:
            continue

        # competition row (with a date) or continuation row (same date)
        if t1 and contient_mois(t1):
            dates_courantes = parser_dates(t1, saison)
            dates_courantes["brute"] = t1.rstrip(" -").strip()
            heritage = False
        elif not t1 and dates_courantes is not None:
            heritage = True
        else:
            continue

        for ligne in lire_cellule(cells[1], base_url):
            texte = nettoyer(ligne["texte"])
            if not texte and not ligne["liens"]:
                continue
            annexe_seule = bool(RE_ANNEXE.search(normaliser(texte.split("+")[0])))
            if annexe_seule and derniere is not None:
                for l in ligne["liens"]:
                    role = role_lien(l["texte"])
                    if role == "photo":
                        continue
                    derniere["_liens"].append(dict(role="annexe", url=canoniser(l["href"]), texte=nettoyer(l["texte"])))
                continue
            ev = nouvel_evenement(texte, ligne["liens"], dates_courantes,
                                  ["date héritée de la ligne précédente"] if heritage else None)
            evenements.append(ev)
            derniere = ev

    return evenements


def finaliser(evenements):
    sortie, vus, doublons = [], set(), 0
    for ev in evenements:
        liens = ev.pop("_liens")
        avert = ev.pop("_avert")

        # main link: first "principale" link, else saturday/sunday
        candidats = [l for l in liens if l["role"] in ("principale", "samedi", "dimanche")]
        principal = candidats[0] if candidats else None
        autres = [l for l in liens if l is not principal]
        if len({l["url"] for l in candidats}) > 1 and all(l["role"] == "principale" for l in candidats):
            avert.append("plusieurs liens 'principaux' différents : à vérifier")

        ev["url_principale"] = principal["url"] if principal else ""
        ev["source"] = source_de(ev["url_principale"])
        m = RE_FLIPSALTO.search(ev["url_principale"])
        ev["id_flipsalto"] = int(m.group(1)) if m else ""
        ev["autres_liens"] = " | ".join(f"{l['role']}:{l['url']}" for l in autres)
        ev["_liens_detail"] = liens
        if not ev["url_principale"]:
            avert.append("aucun lien de résultats")

        cle = (ev["saison"], ev["phase"], ev["groupe"], ev["date_debut"],
               normaliser(ev["ville"]), ev["url_principale"])
        if cle in vus:
            doublons += 1
            continue
        vus.add(cle)
        ev["id_evenement"] = id_stable(*cle)
        ev["avertissements"] = " ; ".join(avert)
        sortie.append(ev)

    partage = Counter(e["url_principale"] for e in sortie if e["url_principale"])
    for e in sortie:
        e["nb_evenements_meme_url"] = partage.get(e["url_principale"], 0) if e["url_principale"] else 0
    return sortie, doublons


def ecrire(evenements, dossier: Path):
    dossier.mkdir(parents=True, exist_ok=True)
    with open(dossier / "competitions.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=COLONNES, extrasaction="ignore", delimiter=";")
        w.writeheader()
        w.writerows(evenements)
    with open(dossier / "competitions.json", "w", encoding="utf-8") as f:
        json.dump(evenements, f, ensure_ascii=False, indent=1, default=str)
    print(f"\nFichiers écrits dans {dossier}/ (competitions.csv, competitions.json)")


def rapport(evenements, doublons):
    print(f"\n===== RAPPORT : {len(evenements)} compétitions ({doublons} doublon(s) exact(s) ignoré(s)) =====")
    par_saison = defaultdict(Counter)
    for e in evenements:
        par_saison[e["saison"]][e["phase"] or "?"] += 1
    for s in sorted(par_saison, reverse=True):
        detail = ", ".join(f"{p}: {n}" for p, n in sorted(par_saison[s].items()))
        print(f"  {s}  total {sum(par_saison[s].values()):3d}  | {detail}")

    src = Counter(e["source"] for e in evenements)
    print("\nSource des liens :", dict(src))
    partages = {e["url_principale"] for e in evenements if e["nb_evenements_meme_url"] > 1}
    print(f"URL partagées par plusieurs lignes de la page : {len(partages)}")

    alertes = [e for e in evenements if e["avertissements"]]
    print(f"\n{len(alertes)} compétition(s) avec avertissement(s) (30 premières) :")
    for e in alertes[:30]:
        print(f"  [{e['saison']}] {e['phase']} {e['groupe']} - {e['date_brute']} - "
              f"{e['libelle']}  ->  {e['avertissements']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=URL_PLAN)
    ap.add_argument("--html", help="parser un fichier HTML local au lieu de télécharger")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--saisons", nargs="*", help="ex. 2024-2025 2023-2024")
    ap.add_argument("--out", default="data/etape1")
    args = ap.parse_args()

    if args.html:
        html = decoder(Path(args.html).read_bytes())
    else:
        html = telecharger(args.url, Path(args.out) / "plan_archives.html", args.refresh)

    evenements = analyser(html, args.url)
    if args.saisons:
        evenements = [e for e in evenements if e["saison"] in args.saisons]
    evenements, doublons = finaliser(evenements)
    if not evenements:
        sys.exit("Aucune compétition trouvée : la structure HTML a peut-être changé.")
    ecrire(evenements, Path(args.out))
    rapport(evenements, doublons)


if __name__ == "__main__":
    main()