"""Scrape the public Flipsalto API (competitions > palmares > team and individual rankings) into the SQLite database."""

import logging
import re
import time
import unicodedata
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import add_data

BASE_URL = "https://competition.flipsalto.fr/api/public"
URL_COMPETITIONS = f"{BASE_URL}/competitions"
URL_PAGE_COMPETITION = "https://competition.flipsalto.fr/public/competitions/{id}/palmares"
DB_PATH = "Competitions_gym.db"

TIMEOUT = (10, 60)  # (connect, read) in seconds
DELAI_ENTRE_REQUETES = 0.15
LIMITE_COMPETITIONS = None  # set an int to test on a few competitions only
ACCEPTER_404_CLASSEMENTS_VIDES = True  # a missing ranking (404) counts as an empty one

# The schema only has women's apparatus
GENRE_RETENU = "GAF"
AGRES_GAF = {7: "note_saut", 8: "note_barres", 9: "note_poutre", 10: "note_sol"}  # Flipsalto agres_id -> column

CLES_COLLECTION = ("data", "items", "results", "competitions", "palmares", "equipes", "individuel")
CHAMPS_DATE = ("date_debut", "date_fin", "old_date", "human_date")
MOIS_FR = {
    "janvier": 1, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6,
    "juillet": 7, "aout": 8, "septembre": 9, "octobre": 10, "novembre": 11, "decembre": 12,
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("flipsalto")

conn = None  # set in main()
CACHE_COMPETITIONS: dict[Any, int] = {}  # API competition id -> db id
CACHE_PALMARES: dict[tuple, dict] = {}  # (API competition id, API palmares id) -> db id and context
PALMARES_VUS: dict[int, tuple] = {}  # db palmares id -> API key, to detect collisions
CACHE_EQUIPES: dict[Any, dict] = {}  # API team id -> {"id_res": RESULTAT_EQUIPE id, "club": club}
CACHE_CLUBS: dict[Any, dict] = {}  # API club id -> club (individual results only give a club_id)


### HTTP ###

class ErreurAPI(Exception):
    pass


def creer_session() -> requests.Session:
    retry = Retry(total=5, connect=5, read=3, status=5, backoff_factor=1.0,
                  status_forcelist=(429, 500, 502, 503, 504), allowed_methods=frozenset(["GET"]),
                  respect_retry_after_header=True, raise_on_status=False)
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"Accept": "application/json", "User-Agent": "FlipsaltoDataCollector/1.0"})
    return session


def get_json(session: requests.Session, url: str, *, accepter_404: bool = False) -> Any:
    try:
        response = session.get(url, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise ErreurAPI(f"Erreur réseau pour {url}: {exc}") from exc
    finally:
        time.sleep(DELAI_ENTRE_REQUETES)

    if response.status_code == 404 and accepter_404:
        logger.warning("Ressource absente (404) : %s", url)
        return None
    if not response.ok:
        extrait = response.text[:300].replace("\n", " ")
        raise ErreurAPI(f"HTTP {response.status_code} pour {url}. Réponse : {extrait}")
    try:
        return response.json()
    except requests.exceptions.JSONDecodeError as exc:
        raise ErreurAPI(f"La réponse n'est pas un JSON valide : {url}") from exc


def extraire_enregistrements(payload: Any) -> list[dict]:
    """Accepts a list, a wrapped list ({"data": [...]}), a single record or a dict indexed by id."""
    if payload is None:
        return []
    if isinstance(payload, list):
        if not all(isinstance(item, dict) for item in payload):
            raise ErreurAPI("La liste JSON contient des éléments non objets.")
        return payload
    if not isinstance(payload, dict):
        raise ErreurAPI(f"Format JSON inattendu : {type(payload).__name__}")

    for cle in CLES_COLLECTION:
        if cle in payload:
            return extraire_enregistrements(payload[cle])
    if "id" in payload:
        return [payload]
    valeurs = list(payload.values())
    if not valeurs:
        return []
    if all(isinstance(v, dict) and "id" in v for v in valeurs):
        return valeurs
    raise ErreurAPI(f"Impossible d'identifier les enregistrements. Clés reçues : {list(payload.keys())[:20]}")


### JSON -> DB FORMAT ###

def vers_float(valeur: Any) -> float:
    if valeur in (None, ""):
        return 0.0
    return float(str(valeur).replace(",", ".").strip())


def vers_int(valeur: Any) -> int:
    # 7, "7", "N7", "Niveau 7" -> 7
    if isinstance(valeur, int):
        return valeur
    m = re.search(r"\d+", str(valeur))
    if not m:
        raise ValueError(f"Impossible d'extraire un entier de {valeur!r}")
    return int(m.group())


def parser_date(texte: Any) -> tuple[int, int] | None:
    # (month, year) from "2025-12-13", "13/12/2025" or "13-14 décembre 2025"
    if not texte:
        return None
    t = unicodedata.normalize("NFD", str(texte).lower())
    t = "".join(ch for ch in t if unicodedata.category(ch) != "Mn")

    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", t)
    if m:
        return int(m.group(2)), int(m.group(1))
    m = re.search(r"(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})", t)
    if m and 1 <= int(m.group(2)) <= 12:
        return int(m.group(2)), int(m.group(3))
    m = re.search(r"([a-z]+)\s+(\d{4})", t)
    if m and m.group(1) in MOIS_FR:
        return MOIS_FR[m.group(1)], int(m.group(2))
    return None


def lire_competition(c: dict) -> dict:
    mois_annee = next((d for d in map(parser_date, (c.get(k) for k in CHAMPS_DATE)) if d), None)
    if mois_annee is None:
        raise ValueError("Date introuvable pour la compétition %s : %s ; saison=%s"
                         % (c.get("id"), {k: c.get(k) for k in CHAMPS_DATE}, c.get("saison")))
    mois, annee = mois_annee
    # EQUIPE.saison is NOT NULL: fall back to the season deduced from the date (September to August)
    debut = annee if mois >= 9 else annee - 1
    saison = (c.get("saison") or {}).get("nom") or f"{debut}-{debut + 1}"
    type_ = c.get("type") or {}
    return {
        "mois": mois,
        "annee": annee,
        "ville": (c.get("ville") or "Inconnue").strip(),
        "phase": type_.get("name") or c.get("type_old") or "Inconnue",
        "saison": saison,
        "url": URL_PAGE_COMPETITION.format(id=c["id"]),
    }


def lire_palmares(p: dict) -> dict:
    cat = p["categorie"]
    return {
        "niveau": vers_int(p["niveau"]["name"]),
        "age_min": cat["age_min"],
        "age_max": cat["age_max"],
        "poule": str(p.get("finalite") or ""),
    }


def lire_club(club: dict) -> dict:
    return {
        "nom": club["nom"].strip(),
        "ville": (club.get("ville") or "Inconnue").strip(),
        "departement": str(club.get("departement") or "Inconnu"),
    }


def numero_depuis_nom(nom: str) -> int:
    # "Arpajon 2" -> 2, no number -> 1
    m = re.search(r"(\d+)\s*$", nom or "")
    return int(m.group(1)) if m else 1


def lire_equipe(e: dict) -> dict:
    notes = {col: 0.0 for col in AGRES_GAF.values()}
    for a in e.get("agres", []):
        col = AGRES_GAF.get(a.get("agres_id"))
        if col:
            notes[col] = vers_float(a.get("note_agres"))
    return {
        "club": lire_club(e["club"]),
        "numero_equipe": numero_depuis_nom(e.get("name")),
        "classement": vers_int(e["classement"]),
        "quota": None,
        "total": vers_float(e.get("note_total")),
        **notes,
        "gymnastes": [],  # filled from the individual results
    }


def lire_individuel(r: dict) -> dict:
    g = r["gymnaste"]
    notes = {col: 0.0 for col in AGRES_GAF.values()}
    for a in g.get("agrescompetition", []):
        col = AGRES_GAF.get(a.get("agres_id"))
        if col and a.get("competition_id") == r.get("competition_id"):
            notes[col] = vers_float(a.get("note"))
    return {
        "gymnaste": {
            "nom": str(g["nom"]).strip(),
            "prenom": str(g["prenom"]).strip(),
            "annee_naissance": vers_int(g["annee"]),
        },
        "classement": vers_int(r["classement"]),
        "total": vers_float(r.get("note_total")),
        **notes,
    }


def club_du_resultat(r: dict, palmares_id: Any) -> dict:
    eq = CACHE_EQUIPES.get(r.get("equipe_id"))
    if eq:
        return eq["club"]
    for e in r["gymnaste"].get("equipes", []):
        if e.get("id") == r.get("equipe_id") or e.get("palmares_id") == palmares_id:
            club = CACHE_CLUBS.get(e.get("club_id"))
            if club:
                return club
    raise KeyError(f"Club introuvable pour le gymnaste {r['gymnaste'].get('id')} (equipe_id={r.get('equipe_id')})")


### INSERTS ###

def _assurer_competition(competition: dict) -> int:
    # Created on first palmares only, so that competitions without GAF results are skipped
    cid = competition["id"]
    if cid not in CACHE_COMPETITIONS:
        info = competition["_info"]
        CACHE_COMPETITIONS[cid] = add_data.add_competition(
            conn, info["mois"], info["annee"], info["ville"], info["phase"], info["url"])
    return CACHE_COMPETITIONS[cid]


def traiter_palmares(competition: dict, palmares: dict) -> None:
    info = lire_palmares(palmares)
    id_competition = _assurer_competition(competition)
    id_base = add_data.add_palmares(conn, id_competition, info["niveau"], info["age_min"], info["age_max"],
                                    info["poule"])

    cle = (competition["id"], palmares["id"])
    deja = PALMARES_VUS.setdefault(id_base, cle)
    if deja != cle:
        logger.warning("Collision UQ_palmares : les palmarès API %s et %s de la compétition %s "
                       "partagent la même ligne en base (id=%s)", deja[1], cle[1], cle[0], id_base)

    CACHE_PALMARES[cle] = {
        "id": id_base,
        "niveau": info["niveau"],
        "age_min": info["age_min"],
        "age_max": info["age_max"],
        "saison": competition["_info"]["saison"],
    }


def traiter_classement_equipe(competition: dict, palmares: dict, equipe: dict) -> None:
    # Clubs are cached even for pseudo-teams and disqualified teams:
    # individual results only give a club_id, resolved through these objects
    club = lire_club(equipe["club"])
    add_data.get_or_create_club(conn, club["nom"], club["ville"], club["departement"])
    CACHE_CLUBS[equipe["club_id"]] = club
    CACHE_EQUIPES[equipe["id"]] = {"id_res": None, "club": club}

    if equipe.get("individuel") == 1:
        return
    if equipe.get("classement") is None:
        logger.warning("Équipe %s sans classement (déclassée ?) : ignorée", equipe.get("id"))
        return

    pal = CACHE_PALMARES[(competition["id"], palmares["id"])]
    id_res = add_data.add_team_result(conn, pal["id"], pal["saison"], pal["niveau"], pal["age_min"],
                                      pal["age_max"], lire_equipe(equipe))
    CACHE_EQUIPES[equipe["id"]]["id_res"] = id_res


def traiter_classement_individuel(competition: dict, palmares: dict, resultat: dict) -> None:
    if resultat.get("classement") is None:
        logger.warning("Résultat individuel %s sans classement : ignoré", resultat.get("id"))
        return

    pal = CACHE_PALMARES[(competition["id"], palmares["id"])]
    data = lire_individuel(resultat)
    data["club"] = club_du_resultat(resultat, palmares["id"])
    add_data.add_indiv_result(conn, pal["id"], data)

    # Team composition comes from the teams the gymnast is linked to in this palmares
    id_gym = add_data.get_or_create_gymnaste(conn, **data["gymnaste"])
    for e in resultat["gymnaste"].get("equipes", []):
        if e.get("palmares_id") == palmares["id"] or e.get("id") == resultat.get("equipe_id"):
            eq = CACHE_EQUIPES.get(e["id"])
            if eq and eq["id_res"] is not None:
                add_data.add_composition_equipe(conn, eq["id_res"], id_gym)


### SCRAPING ###

def recuperer_classements(session: requests.Session, competition: dict, palmares: dict) -> None:
    competition_id, palmares_id = competition.get("id"), palmares.get("id")
    if competition_id is None or palmares_id is None:
        logger.error("Identifiant manquant : compétition=%s, palmarès=%s", competition_id, palmares_id)
        return

    url_base = f"{BASE_URL}/competitions/{competition_id}/palmares/{palmares_id}"
    traitements = {"equipes": traiter_classement_equipe, "individuel": traiter_classement_individuel}
    # Teams first: individual results need the clubs and team ids cached above
    for type_classement, traiter in traitements.items():
        try:
            payload = get_json(session, f"{url_base}/{type_classement}", accepter_404=ACCEPTER_404_CLASSEMENTS_VIDES)
            enregistrements = extraire_enregistrements(payload)
        except ErreurAPI:
            logger.exception("Impossible de récupérer le classement %s (compétition=%s, palmarès=%s)",
                             type_classement, competition_id, palmares_id)
            continue

        logger.info("Compétition %s | Palmarès %s | %s : %s résultats",
                    competition_id, palmares_id, type_classement, len(enregistrements))
        for resultat in enregistrements:
            try:
                traiter(competition, palmares, resultat)
            except Exception:
                logger.exception("Erreur de traitement du classement %s (compétition=%s, palmarès=%s, "
                                 "résultat=%s)", type_classement, competition_id, palmares_id, resultat.get("id"))


def recuperer_palmares(session: requests.Session, competition: dict) -> None:
    competition_id = competition.get("id")
    if competition_id is None:
        logger.error("Compétition sans identifiant : %s", competition)
        return

    try:
        palmares_liste = extraire_enregistrements(get_json(session, f"{BASE_URL}/competitions/{competition_id}/palmares/"))
    except ErreurAPI:
        logger.exception("Impossible de récupérer les palmarès de la compétition %s", competition_id)
        return
    logger.info("Compétition %s : %s palmarès", competition_id, len(palmares_liste))

    for palmares in palmares_liste:
        genre = (palmares.get("genre") or {}).get("name")
        if genre != GENRE_RETENU:
            logger.info("Palmarès %s ignoré (genre %s)", palmares.get("id"), genre)
            continue
        try:
            traiter_palmares(competition, palmares)
        except Exception:
            logger.exception("Erreur de traitement du palmarès %s", palmares.get("id"))
        recuperer_classements(session, competition, palmares)
        conn.commit()


def main() -> None:
    global conn
    logger.info("Début de l'extraction Flipsalto")
    session = creer_session()
    conn = add_data.get_connection(DB_PATH)

    try:
        competitions = extraire_enregistrements(get_json(session, URL_COMPETITIONS))
        if LIMITE_COMPETITIONS is not None:
            competitions = competitions[:LIMITE_COMPETITIONS]
        logger.info("%s compétitions à parcourir", len(competitions))

        erreurs = 0
        for index, competition in enumerate(competitions, start=1):
            competition_id = competition.get("id")
            logger.info("[%s/%s] Compétition %s : %s", index, len(competitions), competition_id,
                        competition.get("type_old", "Type inconnu"))
            try:
                competition["_info"] = lire_competition(competition)
                recuperer_palmares(session, competition)
                conn.commit()
            except Exception:
                conn.rollback()
                add_data.reinitialiser_index()  # forget the ids that were rolled back
                erreurs += 1
                logger.exception("Erreur inattendue sur la compétition %s", competition_id)

        logger.info("Extraction terminée : %s compétitions parcourues, %s erreurs inattendues.",
                    len(competitions), erreurs)
    except ErreurAPI:
        logger.exception("Impossible de récupérer la liste des compétitions.")
    finally:
        conn.commit()
        conn.close()
        session.close()


if __name__ == "__main__":
    main()
