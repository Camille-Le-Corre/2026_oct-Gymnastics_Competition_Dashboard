"""Insert parsed results into the SQLite database.

Text keys are compared after normalization ("Départementale" == " departementale "), so each get_or_create_*
returns the existing row when there is one. The extraction scripts only call the add_* functions.
"""

import re
import sqlite3
import unicodedata
from typing import Any, Iterable, Optional

DB_PATH = "Competitions_gym.db"


def normaliser(texte: Any) -> str:
    # "Demi-Finale" -> "demifinale", "Saint-Étienne " -> "saintetienne"
    t = str(texte).lower().replace("œ", "oe").replace("æ", "ae")
    t = unicodedata.normalize("NFKD", t)
    t = "".join(ch for ch in t if unicodedata.category(ch) != "Mn")
    return re.sub(r"[\W_]+", "", t)


def nettoyer(texte: Any) -> str:
    return " ".join(str(texte).split())


def _cle(valeurs: Iterable) -> tuple:
    return tuple(normaliser(v) if isinstance(v, str) else v for v in valeurs)


def get_connection(db_path: str = DB_PATH) -> sqlite3.Connection:
    # mode=rw fails if the file does not exist instead of creating an empty database
    conn = sqlite3.connect(f"file:{db_path}?mode=rw", uri=True)
    conn.execute("PRAGMA foreign_keys = ON")
    reinitialiser_index()
    return conn


### IN-MEMORY INDEX ###

# SQLite cannot compare ignoring accents and dashes: each table is loaded once into
# {normalized key: id}, then kept up to date on every insert
_INDEX: dict[str, dict[tuple, int]] = {}


def reinitialiser_index() -> None:
    # call after a rollback, so that cancelled ids do not stay in the index
    _INDEX.clear()


def _index(conn, table: str, colonnes_cle: tuple[str, ...]) -> dict[tuple, int]:
    if table not in _INDEX:
        index: dict[tuple, int] = {}
        for row in conn.execute(f"SELECT id, {', '.join(colonnes_cle)} FROM {table}"):
            index.setdefault(_cle(row[1:]), row[0])
        _INDEX[table] = index
    return _INDEX[table]


def _get_or_create(conn, table: str, colonnes_cle: tuple[str, ...], valeurs: dict) -> int:
    valeurs = {k: nettoyer(v) if isinstance(v, str) else v for k, v in valeurs.items()}
    index = _index(conn, table, colonnes_cle)
    cle = _cle(valeurs[c] for c in colonnes_cle)
    if cle not in index:
        cur = conn.execute(f"INSERT INTO {table} ({', '.join(valeurs)}) VALUES ({', '.join('?' * len(valeurs))})",
                           tuple(valeurs.values()))
        index[cle] = cur.lastrowid
    return index[cle]


### REFERENCE TABLES ###

def get_or_create_phase(conn, nom: str) -> int:
    return _get_or_create(conn, "PHASE", ("nom",), {"nom": nom})


def get_or_create_categorie_age(conn, age_min, age_max) -> int:
    return _get_or_create(conn, "CATEGORIE_AGE", ("age_min", "age_max"),
                          {"age_min": str(age_min), "age_max": str(age_max)})


def get_or_create_club(conn, nom: str, ville: str, departement: str) -> int:
    return _get_or_create(conn, "CLUB", ("nom",), {"nom": nom, "ville": ville, "departement": departement})


def get_or_create_gymnaste(conn, nom: str, prenom: str, annee_naissance: int) -> int:
    annee_naissance = int(annee_naissance)
    id_gym = _get_or_create(conn, "GYMNASTE", ("nom", "prenom", "annee_naissance"),
                            {"nom": nom, "prenom": prenom, "annee_naissance": annee_naissance})
    if "GYMNASTE_COMPLET" in _INDEX:
        _INDEX["GYMNASTE_COMPLET"].setdefault((normaliser(nom) + normaliser(prenom), annee_naissance), id_gym)
    return id_gym


# The old ufolepgym.com pages give "NOM PRÉNOM" in a single field. Since normaliser() removes
# spaces, comparing the concatenation nom + prenom makes the split irrelevant.
def _index_nom_complet(conn) -> dict[tuple, int]:
    if "GYMNASTE_COMPLET" not in _INDEX:
        index: dict[tuple, int] = {}
        for id_gym, nom, prenom, annee in conn.execute("SELECT id, nom, prenom, annee_naissance FROM GYMNASTE"):
            index.setdefault((normaliser(nom) + normaliser(prenom), annee), id_gym)
        _INDEX["GYMNASTE_COMPLET"] = index
    return _INDEX["GYMNASTE_COMPLET"]


def separer_nom_complet(nom_complet: str) -> tuple[str, str]:
    # "LE DAULT ROSE" -> ("LE DAULT", "ROSE"), "TROUILLET Alya" -> ("TROUILLET", "Alya")
    mots = nettoyer(nom_complet).split()
    if len(mots) < 2:
        return (mots[0] if mots else ""), ""
    en_majuscules = [m.isupper() for m in mots]
    if en_majuscules[0] and not all(en_majuscules):
        n = en_majuscules.index(False)
        return " ".join(mots[:n]), " ".join(mots[n:])
    return " ".join(mots[:-1]), mots[-1]


def trouver_gymnaste_nom_complet(conn, nom_complet: str, annee_naissance: int) -> Optional[int]:
    # tries "NOM PRÉNOM", then "PRÉNOM NOM" with every possible split
    index = _index_nom_complet(conn)
    annee_naissance = int(annee_naissance)
    mots = nettoyer(nom_complet).split()
    for i in range(len(mots)):
        cle = (normaliser(" ".join(mots[i:])) + normaliser(" ".join(mots[:i])), annee_naissance)
        if cle in index:
            return index[cle]
    return None


def id_gymnaste(conn, g: dict) -> int:
    # g = {"nom", "prenom", "annee_naissance"} or {"nom_complet", "annee_naissance"}
    if "nom_complet" not in g:
        return get_or_create_gymnaste(conn, g["nom"], g["prenom"], g["annee_naissance"])
    id_gym = trouver_gymnaste_nom_complet(conn, g["nom_complet"], g["annee_naissance"])
    if id_gym is None:
        nom, prenom = separer_nom_complet(g["nom_complet"])
        id_gym = get_or_create_gymnaste(conn, nom, prenom, g["annee_naissance"])
    return id_gym


### COMPETITIONS AND TEAMS ###

def get_or_create_competition(conn, mois: int, annee: int, ville: str, id_phase: int,
                              url: Optional[str] = None) -> int:
    # No UNIQUE constraint: the source url identifies the competition when there is one,
    # since two competitions can take place in the same city the same month
    valeurs = {"mois": int(mois), "annee": int(annee), "ville": nettoyer(ville), "id_phase": id_phase, "url": url}
    if not url:
        return _get_or_create(conn, "COMPETITION", ("mois", "annee", "ville", "id_phase"), valeurs)
    row = conn.execute("SELECT id FROM COMPETITION WHERE url = ?", (url,)).fetchone()
    if row:
        return row[0]
    cur = conn.execute("""INSERT INTO COMPETITION (mois, annee, ville, id_phase, url)
                          VALUES (:mois, :annee, :ville, :id_phase, :url)""", valeurs)
    _INDEX.pop("COMPETITION", None)
    return cur.lastrowid


def get_or_create_palmares(conn, id_competition: int, niveau: int, id_categorie_age: int, poule: str) -> int:
    return _get_or_create(conn, "PALMARES", ("id_competition", "niveau", "id_categorie_age", "poule"),
                          {"id_competition": id_competition, "niveau": int(niveau),
                           "id_categorie_age": id_categorie_age, "poule": poule})


def get_or_create_equipe(conn, id_club: int, numero_equipe: int, niveau: int, id_categorie_age: int,
                         saison: str) -> int:
    return _get_or_create(conn, "EQUIPE", ("id_club", "numero_equipe", "niveau", "id_categorie_age", "saison"),
                          {"id_club": id_club, "numero_equipe": int(numero_equipe), "niveau": int(niveau),
                           "id_categorie_age": id_categorie_age, "saison": saison})


### RESULTS (insert or update) ###

def add_resultat_equipe(conn, id_equipe: int, id_palmares: int, classement: int, total: float, note_saut: float,
                        note_barres: float, note_poutre: float, note_sol: float, quota: Optional[int] = None) -> int:
    row = conn.execute("SELECT id FROM RESULTAT_EQUIPE WHERE id_equipe = ? AND id_palmares = ?",
                       (id_equipe, id_palmares)).fetchone()
    values = (classement, quota, total, note_saut, note_barres, note_poutre, note_sol)
    if row:
        conn.execute("""UPDATE RESULTAT_EQUIPE
                        SET classement = ?, quota = ?, total = ?, note_saut = ?, note_barres = ?, note_poutre = ?,
                            note_sol = ?
                        WHERE id = ?""", values + (row[0],))
        return row[0]
    cur = conn.execute("""INSERT INTO RESULTAT_EQUIPE (classement, quota, total, note_saut, note_barres, note_poutre,
                                                       note_sol, id_equipe, id_palmares)
                          VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", values + (id_equipe, id_palmares))
    return cur.lastrowid


def add_composition_equipe(conn, id_resultat_equipe: int, id_gymnaste: int) -> int:
    row = conn.execute("SELECT id FROM COMPOSITION_EQUIPE WHERE id_resultat_equipe = ? AND id_gymnaste = ?",
                       (id_resultat_equipe, id_gymnaste)).fetchone()
    if row:
        return row[0]
    cur = conn.execute("INSERT INTO COMPOSITION_EQUIPE (id_resultat_equipe, id_gymnaste) VALUES (?, ?)",
                       (id_resultat_equipe, id_gymnaste))
    return cur.lastrowid


def add_resultat_individuel(conn, id_gymnaste: int, id_palmares: int, id_club: int, classement: int, total: float,
                            note_saut: float, note_barres: float, note_poutre: float, note_sol: float) -> int:
    row = conn.execute("SELECT id FROM RESULTAT_INDIVIDUEL WHERE id_gymnaste = ? AND id_palmares = ?",
                       (id_gymnaste, id_palmares)).fetchone()
    values = (total, note_saut, note_barres, note_poutre, note_sol, classement, id_club)
    if row:
        conn.execute("""UPDATE RESULTAT_INDIVIDUEL
                        SET total = ?, note_saut = ?, note_barres = ?, note_poutre = ?, note_sol = ?, classement = ?,
                            id_club = ?
                        WHERE id = ?""", values + (row[0],))
        return row[0]
    cur = conn.execute("""INSERT INTO RESULTAT_INDIVIDUEL (total, note_saut, note_barres, note_poutre, note_sol,
                                                           classement, id_club, id_gymnaste, id_palmares)
                          VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", values + (id_gymnaste, id_palmares))
    return cur.lastrowid


### ENTRY POINTS FOR THE EXTRACTION SCRIPTS ###

def add_competition(conn, mois: int, annee: int, ville: str, phase: str, url: Optional[str] = None) -> int:
    return get_or_create_competition(conn, mois, annee, ville, get_or_create_phase(conn, phase), url)


def add_palmares(conn, id_competition: int, niveau: int, age_min, age_max, poule: str) -> int:
    id_cat = get_or_create_categorie_age(conn, age_min, age_max)
    return get_or_create_palmares(conn, id_competition, niveau, id_cat, poule)


def add_team_result(conn, id_palmares: int, saison: str, niveau: int, age_min, age_max, e: dict) -> int:
    # e: club {nom, ville, departement}, numero_equipe, classement, quota, total, note_*, optional gymnastes list
    id_cat = get_or_create_categorie_age(conn, age_min, age_max)
    c = e["club"]
    id_club = get_or_create_club(conn, c["nom"], c["ville"], c["departement"])
    id_equipe = get_or_create_equipe(conn, id_club, e["numero_equipe"], niveau, id_cat, saison)
    id_res = add_resultat_equipe(conn, id_equipe, id_palmares, e["classement"], e["total"], e["note_saut"],
                                 e["note_barres"], e["note_poutre"], e["note_sol"], quota=e.get("quota"))
    for g in e.get("gymnastes", []):
        add_composition_equipe(conn, id_res, id_gymnaste(conn, g))
    return id_res


def add_indiv_result(conn, id_palmares: int, r: dict) -> int:
    # r: gymnaste (see id_gymnaste), club {nom, ville, departement}, classement, total, note_*
    c = r["club"]
    id_gym = id_gymnaste(conn, r["gymnaste"])
    id_club = get_or_create_club(conn, c["nom"], c["ville"], c["departement"])
    return add_resultat_individuel(conn, id_gym, id_palmares, id_club, r["classement"], r["total"],
                                   r["note_saut"], r["note_barres"], r["note_poutre"], r["note_sol"])
