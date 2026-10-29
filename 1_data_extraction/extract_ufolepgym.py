"""Load the old ufolepgym.com static result pages (seasons 2015-2016 to 2024-2025) into the Flipsalto database.

Skipped: plan rows linking to Flipsalto (already loaded by extract_flipsalto.py), URLs already in
COMPETITION.url, and competitions published on both systems (same gymnasts, same total, same month ±1).

    python extract_ufolepgym.py --limite 5 --dry-run
    python extract_ufolepgym.py --saisons 2015-2016 2016-2017
"""

import argparse
import hashlib
import logging
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin

import requests
from bs4 import BeautifulSoup

import add_data
import get_all_competitions as plan
from extract_flipsalto import (DB_PATH, DELAI_ENTRE_REQUETES, GENRE_RETENU, TIMEOUT, creer_session,
                               numero_depuis_nom, vers_float)

CACHE_PLAN = Path("data/etape1/plan_archives.html")
DOSSIER_CACHE_PAGES = Path("data/ufolepgym")  # archived pages never change, so they are only downloaded once
SOURCE_RETENUE = "ufolepgym_statique"
AGE_MAX_OUVERT = 99  # "11 +" -> 11-99, as in Flipsalto
NB_RESULTATS_A_VERIFIER = 20  # duplicate if at least half of these are already in another competition

# GAF_NIVEAU8_7-12 ans_SA_DA_A_Equipes_.htm, 37_GAF_..., ..._Equipes_36.htm -> genre, niveau, ages, poule, type
RE_PAGE = re.compile(
    r"^(?:\d+_)?(GA[FM])_([^_]+)_([^_]+)_.*?_([A-Z])_(Equipes|Individuels)(?:_[^.]*)?\.html?$", re.I)
AGRES = {"saut": "note_saut", "barres": "note_barres", "poutre": "note_poutre", "sol": "note_sol"}

logger = logging.getLogger("ufolepgym")
conn = None  # set in main()


### PARSING ###

def telecharger_page(session: requests.Session, url: str) -> str | None:
    cache = DOSSIER_CACHE_PAGES / (hashlib.sha1(url.encode()).hexdigest() + ".htm")
    if cache.exists():
        contenu = cache.read_bytes()
    else:
        try:
            response = session.get(url, timeout=TIMEOUT)
        except requests.RequestException as exc:
            logger.warning("Erreur réseau pour %s : %s", url, exc)
            return None
        finally:
            time.sleep(DELAI_ENTRE_REQUETES)
        if not response.ok:
            logger.warning("HTTP %s pour %s", response.status_code, url)
            return None
        contenu = response.content
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(contenu)
    # pages mix UTF-8 and latin-1
    return plan.decoder(contenu) if contenu.strip() else None


def lister_palmares(html: str, url_index: str) -> dict[tuple, dict]:
    # {(niveau, ages, poule): {"Equipes": url, "Individuels": url}}, GAF only
    soup = BeautifulSoup(html, "html.parser")
    palmares: dict[tuple, dict] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        nom_fichier = unquote(href).rsplit("/", 1)[-1]
        m = RE_PAGE.match(nom_fichier)
        if not m:
            if re.search(r"equipes|individuels", nom_fichier, re.I):
                logger.warning("Lien de résultats non reconnu : %s", nom_fichier)
            continue
        genre, niveau, ages, poule, type_page = m.groups()
        if genre.upper() == GENRE_RETENU:
            cle = (niveau, ages.strip(), poule.upper())
            palmares.setdefault(cle, {})[type_page.capitalize()] = urljoin(url_index, href)
    return palmares


def lire_niveau(brut: str) -> int | None:
    # "NIVEAU8" -> 8, "ENCHAINEMENT" -> None
    m = re.fullmatch(r"niveau\s*(\d+)", brut.strip(), re.I)
    return int(m.group(1)) if m else None


def lire_ages(brut: str) -> tuple[int, int]:
    nombres = [int(n) for n in re.findall(r"\d+", brut)]
    if len(nombres) == 1 and "+" in brut:
        return nombres[0], AGE_MAX_OUVERT
    if len(nombres) == 2:
        return nombres[0], nombres[1]
    raise ValueError(f"Catégorie d'âge illisible : {brut!r}")


def _titre_colonne(cellule) -> str:
    # apparatus columns only contain an image
    img = cellule.find("img", alt=True)
    titre = plan.normaliser(img["alt"] if img else cellule.get_text(" ")).strip()
    return AGRES.get(titre, titre)


def lire_tableau(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    for table in soup.find_all("table"):
        lignes = table.find_all("tr")
        if not lignes:
            continue
        entete = [_titre_colonne(c) for c in lignes[0].find_all(["td", "th"])]
        if "total" not in entete:
            continue
        resultats, rang = [], None
        for tr in lignes[1:]:
            cellules = [plan.nettoyer(c.get_text(" ")) for c in tr.find_all(["td", "th"])]
            if len(cellules) != len(entete):
                continue  # "Equipes qualifiées", trailing empty rows
            ligne = dict(zip(entete, cellules))
            if not ligne["total"]:
                continue
            if ligne[entete[0]].isdigit():
                rang = int(ligne[entete[0]])
            elif ligne[entete[0]] or rang is None:
                continue
            # empty rank = tied with the previous row
            resultats.append({**ligne, "classement": rang})
        return resultats
    return []


def note(valeur: Any) -> float:
    try:
        return vers_float(valeur)
    except ValueError:  # "-", "abs"...
        return 0.0


def notes_agres(ligne: dict) -> dict:
    return {col: note(ligne.get(col)) for col in AGRES.values()}


def lire_club(texte: str, equipe: str = "") -> dict:
    # "95--Espoirs gymniques d'osny" -> name + department; empty cell -> team name without number
    m = re.match(r"^\s*(\d{1,3}|2[AB])\s*--\s*(.+)$", texte or "")
    departement, nom = (m.group(1), m.group(2)) if m else ("Inconnu", texte)
    nom = re.sub(r"\s+\d$", "", plan.nettoyer(nom or ""))
    nom = nom or base_equipe(equipe) or "Inconnu"
    return {"nom": nom, "ville": "Inconnue", "departement": departement}


def base_equipe(equipe: str) -> str:
    # "Wavrin 1 (indiv) (indiv)" -> "Wavrin"
    nom = re.sub(r"\(\s*indiv\s*\)", "", equipe or "", flags=re.I)
    return plan.nettoyer(re.sub(r"\s*\d+\s*$", "", plan.nettoyer(nom)))


def lire_gymnaste(r: dict) -> tuple[str, int] | None:
    # (full name, birth year), None for filler rows ("2", "", "0"...)
    nom = plan.nettoyer(re.sub(r"\*", " ", r.get("gymnaste", "")))
    annee = r.get("annee", "")
    if not re.search(r"[^\W\d_]", nom) or not annee.isdigit() or int(annee) < 1900:
        return None
    return nom, int(annee)


def lire_competition(session: requests.Session, ev: dict) -> list[dict]:
    url = ev["url_principale"]
    html = telecharger_page(session, url)
    if not html:
        return []

    palmares = []
    for (niveau_brut, ages_brut, poule), pages in lister_palmares(html, url).items():
        niveau = lire_niveau(niveau_brut)
        if niveau is None:
            logger.info("Palmarès %s %s ignoré (pas de niveau numéroté)", niveau_brut, ages_brut)
            continue
        try:
            age_min, age_max = lire_ages(ages_brut)
        except ValueError:
            logger.warning("Palmarès ignoré (%s) : %s", url, ages_brut)
            continue

        tableaux = {}
        for type_page in ("Equipes", "Individuels"):
            page = telecharger_page(session, pages[type_page]) if type_page in pages else None
            tableaux[type_page] = lire_tableau(page) if page else []
        if not tableaux["Equipes"] and not tableaux["Individuels"]:
            logger.warning("Palmarès N%s %s %s sans résultats : %s", niveau, ages_brut, poule, url)
            continue

        palmares.append({"niveau": niveau, "age_min": age_min, "age_max": age_max, "poule": poule,
                         "equipes": tableaux["Equipes"], "individuels": tableaux["Individuels"]})
    return palmares


### DUPLICATES ###

def chercher_doublon(palmares: list[dict], mois: int, annee: int) -> tuple | None:
    # (id, url) of a competition already in the db with the same results. Read only.
    verifies = 0
    trouvees: Counter = Counter()
    for p in palmares:
        for r in p["individuels"]:
            if verifies >= NB_RESULTATS_A_VERIFIER:
                break
            gymnaste = lire_gymnaste(r)
            if gymnaste is None:
                continue
            verifies += 1
            id_gym = add_data.trouver_gymnaste_nom_complet(conn, *gymnaste)
            if id_gym is None:
                continue
            row = conn.execute(
                """SELECT c.id, c.url FROM RESULTAT_INDIVIDUEL ri
                   JOIN PALMARES p ON p.id = ri.id_palmares
                   JOIN COMPETITION c ON c.id = p.id_competition
                   WHERE ri.id_gymnaste = ? AND ABS(ri.total - ?) < 0.001
                     AND ABS((c.annee * 12 + c.mois) - ?) <= 1
                   LIMIT 1""",
                (id_gym, note(r["total"]), annee * 12 + mois),
            ).fetchone()
            if row:
                trouvees[row] += 1
    if verifies and trouvees:
        competition, nb = trouvees.most_common(1)[0]
        if nb * 2 >= verifies:
            return competition
    return None


### INSERTS ###

def charger_competition(ev: dict, palmares: list[dict]) -> None:
    id_competition = add_data.add_competition(conn, ev["mois"], ev["annee"], ev["ville"] or "Inconnue",
                                              ev["phase"] or "Inconnue", ev["url_principale"])

    # Individual pages only give the team name: the club is found on the team pages of the competition,
    # by exact team name first, then by name without number ("Wavrin 2" -> "Wavrin")
    clubs_par_equipe, clubs_par_base = {}, {}
    for p in palmares:
        for e in p["equipes"]:
            club = lire_club(e.get("club", ""), e["equipe"])
            clubs_par_equipe.setdefault(add_data.normaliser(e["equipe"]), club)
            clubs_par_base.setdefault(add_data.normaliser(base_equipe(e["equipe"])), club)

    for p in palmares:
        id_palmares = add_data.add_palmares(conn, id_competition, p["niveau"], p["age_min"], p["age_max"], p["poule"])

        resultats_equipe = {}  # normalized team name -> RESULTAT_EQUIPE id
        for e in p["equipes"]:
            if not e["equipe"]:
                continue
            resultat = {
                "club": lire_club(e.get("club", ""), e["equipe"]),
                "numero_equipe": numero_depuis_nom(e["equipe"]),
                "classement": e["classement"],
                "quota": None,
                "total": note(e["total"]),
                **notes_agres(e),
            }
            id_res = add_data.add_team_result(conn, id_palmares, ev["saison"], p["niveau"], p["age_min"],
                                              p["age_max"], resultat)
            resultats_equipe[add_data.normaliser(e["equipe"])] = id_res

        for r in p["individuels"]:
            lu = lire_gymnaste(r)
            if lu is None:
                if r.get("gymnaste", "").strip("* ") and not r["gymnaste"].strip("* ").isdigit():
                    logger.warning("Gymnaste %r sans année de naissance : ignorée (%s)",
                                   r.get("gymnaste"), ev["url_principale"])
                continue
            equipe = add_data.normaliser(r.get("equipe", ""))
            club = (clubs_par_equipe.get(equipe)
                    or clubs_par_base.get(add_data.normaliser(base_equipe(r.get("equipe", ""))))
                    or lire_club("", r.get("equipe", "")))
            gymnaste = {"nom_complet": lu[0], "annee_naissance": lu[1]}
            add_data.add_indiv_result(conn, id_palmares, {
                "gymnaste": gymnaste,
                "club": club,
                "classement": r["classement"],
                "total": note(r["total"]),
                **notes_agres(r),
            })
            if equipe in resultats_equipe:
                add_data.add_composition_equipe(conn, resultats_equipe[equipe], add_data.id_gymnaste(conn, gymnaste))


def main() -> None:
    global conn

    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--saisons", nargs="*", help="ex. 2015-2016 2016-2017")
    ap.add_argument("--limite", type=int, help="nombre max de compétitions à traiter")
    ap.add_argument("--dry-run", action="store_true", help="lit et contrôle tout, mais n'écrit rien en base")
    ap.add_argument("--refresh-plan", action="store_true", help="re-télécharge plan_archives.htm")
    ap.add_argument("--db", default=DB_PATH, help=f"base SQLite (défaut : {DB_PATH})")
    args = ap.parse_args()

    logger.info("Début de l'extraction ufolepgym.com%s", " (dry-run)" if args.dry_run else "")
    html = plan.telecharger(plan.URL_PLAN, CACHE_PLAN, args.refresh_plan)
    evenements, _ = plan.finaliser(plan.analyser(html, plan.URL_PLAN))
    if args.saisons:
        evenements = [e for e in evenements if e["saison"] in args.saisons]

    session = creer_session()
    session.headers.update({"Accept": "text/html", "User-Agent": "Mozilla/5.0 (pipeline-ufolep)"})
    conn = add_data.get_connection(args.db)

    bilan: Counter = Counter()
    urls_vues: set[str] = set()
    traitees = 0
    try:
        for ev in evenements:
            url = ev["url_principale"]
            if ev["source"] != SOURCE_RETENUE:
                bilan[f"ignorée (lien {ev['source']})"] += 1
                continue
            if not re.search(r"\.html?$", url, re.I):
                bilan["ignorée (pas une page HTML)"] += 1
                continue
            if url in urls_vues:
                bilan["ignorée (même URL qu'une ligne précédente)"] += 1
                continue
            urls_vues.add(url)
            if not ev["mois"]:
                logger.warning("Date illisible, compétition ignorée : %s", ev["libelle"])
                bilan["ignorée (date illisible)"] += 1
                continue
            if conn.execute("SELECT 1 FROM COMPETITION WHERE url = ?", (url,)).fetchone():
                bilan["déjà en base"] += 1
                continue
            if args.limite is not None and traitees >= args.limite:
                break
            traitees += 1

            logger.info("[%s] %s %s - %s (%s)", ev["saison"], ev["phase"], ev["groupe"], ev["ville"], ev["date_debut"])
            try:
                palmares = lire_competition(session, ev)
                if not palmares:
                    logger.warning("Aucun résultat GAF lisible : %s", url)
                    bilan["sans résultats GAF"] += 1
                    continue

                doublon = chercher_doublon(palmares, ev["mois"], ev["annee"])
                if doublon:
                    logger.info("  -> doublon de la compétition %s en base (%s)", *doublon)
                    bilan["doublon (déjà chargée depuis un autre lien)"] += 1
                    continue

                charger_competition(ev, palmares)
                logger.info("  -> %s palmarès, %s équipes, %s individuels", len(palmares),
                            sum(len(p["equipes"]) for p in palmares), sum(len(p["individuels"]) for p in palmares))
                if args.dry_run:
                    conn.rollback()
                    add_data.reinitialiser_index()
                else:
                    conn.commit()
                bilan["chargée" + (" (dry-run, annulée)" if args.dry_run else "")] += 1

            except Exception:
                conn.rollback()
                add_data.reinitialiser_index()  # forget the ids that were rolled back
                bilan["erreur"] += 1
                logger.exception("Erreur sur la compétition %s", url)
    finally:
        if not args.dry_run:
            conn.commit()
        conn.close()
        session.close()

    logger.info("Bilan :")
    for etat, nb in sorted(bilan.items()):
        logger.info("  %-45s %s", etat, nb)


if __name__ == "__main__":
    main()
