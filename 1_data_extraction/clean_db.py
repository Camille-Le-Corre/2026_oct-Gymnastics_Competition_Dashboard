"""Clean the database built by extract_flipsalto.py and extract_ufolepgym.py, in place.

Removes test competitions, harmonizes phases, seasons, clubs, cities and gymnast names, fixes impossible
birth years when a namesake has a single plausible one, and deletes orphan rows. Rows that become identical
are merged into the smallest id. Safe to run again after each extraction.

    python clean_db.py --dry-run
"""

import argparse
import logging
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

from add_data import normaliser


DB_PATH = Path(__file__).with_name("Competitions_gym.db")

CLUB_INCONNU = "INCONNU"
VILLE_INCONNUE = "INCONNUE"
DEPARTEMENT_INCONNU = "Inconnu"

# normalized key -> kept name, other phases are only capitalized
PHASES = {
    "departementale": "Départementale",
    "regionale": "Régionale",
    "demifinale": "Demi-finale",
    "demifinales": "Demi-finale",
    "finale": "Finale nationale",
    "finalesnationales": "Finale nationale",
    "finalejeune": "Finale jeunes",
    "finalejeunes": "Finale jeunes",
    "finalesjeunes": "Finale jeunes",
    "finalesjeunesg1": "Finale jeunes G1",
    "finalesjeunesg2": "Finale jeunes G2",
    "finalesjeunesg3": "Finale jeunes G3",
    "interdepartementaux": "Interdépartementale",
}
PHASES.update({normaliser(nom): nom for nom in PHASES.values()})  # already cleaned names
PHASES_TEST = {"test"}
RE_VILLE_TEST = re.compile(r"\btest", re.I)

AGE_MIN_GYMNASTE = 3  # minimum age at the first competition
ANNEE_NAISSANCE_MIN = 1940

NB_ANOMALIES_AFFICHEES = 30

RE_PARENTHESES = re.compile(r"\([^)]*\)?")         # "(indiv)", unclosed "( agpv"
RE_PARENTHESES_FERMEES = re.compile(r"\([^)]*\)")  # "(non noté) ELOÏSE"
RE_POINT_SIGLE = re.compile(r"\.(?=[^\W\d_]\.)")   # "A.S.L." -> "ASL."
RE_NON_LETTRE = re.compile(r"[\W\d_]+")            # digits, -, /, *, '...
RE_MOTS_INTERDITS = re.compile(r"\b(?:individuel(?:le)?s?|indiv|ind)\b", re.I)
RE_SAINT = re.compile(r"\b(ST|STE)\b")
RE_MOT_PRENOM = re.compile(r"[^\s-]+")             # words split on spaces or dashes
RE_TIRET = re.compile(r"\s*-+\s*")                 # "LUCAS - TAUPIER", "POUX--GLORY"
RE_RANG_DEBUT = re.compile(r"^[\d\s.\-']+")        # "1 BRUN", "1BRUN"
# prenom "- KELLER Anna": the first word belongs to the nom ("PICARD-KELLER")
RE_NOM_COUPE = re.compile(r"^\s*-\s*([^\s\d]+)\s+(\S.*)$")

# curly apostrophes -> ', compound name separators -> -
TRADUCTION_PERSONNE = str.maketrans({
    "’": "'", "‘": "'", "`": "'", "´": "'",
    "—": "-", "–": "-", "_": "-", "/": "-",
    "#": " ", ".": " ", "(": " ", ")": " ",
})

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("clean_db")


def harmoniser_saison(saison: str) -> str:
    return re.sub(r"\s*[/\-]\s*", "-", saison.strip())


def harmoniser_phase(nom: str) -> str:
    cle = normaliser(nom)
    if cle in PHASES:
        return PHASES[cle]
    nom = " ".join(nom.split())
    return nom[:1].upper() + nom[1:]


def nettoyer_nom_lieu(texte: str, defaut: str) -> str:
    # club or city in uppercase letters only
    # "38- Gym et Rythme" -> "GYM ET RYTHME", "A.L.Gerzat" -> "AL GERZAT", "St Priest" -> "SAINT PRIEST"
    t = RE_PARENTHESES.sub(" ", texte or "")
    t = RE_POINT_SIGLE.sub("", t)
    t = RE_NON_LETTRE.sub(" ", t)
    t = RE_MOTS_INTERDITS.sub(" ", t)
    t = RE_SAINT.sub(lambda m: "SAINT" if m.group(1) == "ST" else "SAINTE", t.upper())
    return " ".join(t.split()) or defaut


def nettoyer_departement(dep: str) -> str:
    # "1" -> "01", "080" -> "80", "971" and "2A" unchanged
    dep = (dep or "").strip().upper()
    if dep.isdigit():
        return str(int(dep)).zfill(2)
    return dep if re.fullmatch(r"2[AB]", dep) else DEPARTEMENT_INCONNU


def _nettoyer_personne(texte: str) -> str:
    t = RE_PARENTHESES_FERMEES.sub(" ", texte or "").translate(TRADUCTION_PERSONNE)
    t = RE_TIRET.sub("-", " ".join(t.split()))
    return t.strip(" -'")


def nettoyer_nom(nom: str) -> str:
    # "1 BRUN" -> "BRUN", "Poux--Glory" -> "POUX-GLORY"
    t = _nettoyer_personne(nom)
    return RE_RANG_DEBUT.sub("", t).strip(" -'").upper()


def harmoniser_prenom(prenom: str) -> str:
    # "ANNE-MARIE" -> "Anne-Marie", "lou ann" -> "Lou Ann", "Lola ##" -> "Lola"
    p = _nettoyer_personne(re.sub(r"\d+", " ", prenom or ""))
    p = RE_TIRET.sub("-", " ".join(p.split())).strip(" -'")
    return RE_MOT_PRENOM.sub(lambda m: m.group()[0].upper() + m.group()[1:].lower(), p)


def separer_nom_coupe(nom: str, prenom: str) -> tuple[str, str]:
    m = RE_NOM_COUPE.match(prenom or "")
    if m and m.group(1).isupper():
        return f"{nom}-{m.group(1)}", m.group(2)
    return nom, prenom


def score_graphie(texte: str) -> tuple:
    # preferred spelling: most accents, then dashes, then apostrophes
    return (sum(not ch.isascii() for ch in texte), texte.count("-"), texte.count("'"))


def fusionner_resultats_equipe(conn, garde: int, doublon: int) -> None:
    conn.execute("UPDATE OR IGNORE COMPOSITION_EQUIPE SET id_resultat_equipe = ? WHERE id_resultat_equipe = ?",
                 (garde, doublon))
    # what is left is already in the kept composition
    conn.execute("DELETE FROM COMPOSITION_EQUIPE WHERE id_resultat_equipe = ?", (doublon,))
    conn.execute("DELETE FROM RESULTAT_EQUIPE WHERE id = ?", (doublon,))


def fusionner_equipes(conn, garde: int, doublon: int) -> None:
    conn.execute("UPDATE OR IGNORE RESULTAT_EQUIPE SET id_equipe = ? WHERE id_equipe = ?", (garde, doublon))
    # what is left is on a palmares where the kept team already has a result
    for id_res, id_palmares in conn.execute(
        "SELECT id, id_palmares FROM RESULTAT_EQUIPE WHERE id_equipe = ?", (doublon,)
    ).fetchall():
        (id_garde,) = conn.execute("SELECT id FROM RESULTAT_EQUIPE WHERE id_equipe = ? AND id_palmares = ?",
                                   (garde, id_palmares)).fetchone()
        fusionner_resultats_equipe(conn, id_garde, id_res)
    conn.execute("DELETE FROM EQUIPE WHERE id = ?", (doublon,))


def fusionner_clubs(conn, garde: int, doublon: int) -> None:
    conn.execute("UPDATE RESULTAT_INDIVIDUEL SET id_club = ? WHERE id_club = ?", (garde, doublon))
    conn.execute("UPDATE OR IGNORE EQUIPE SET id_club = ? WHERE id_club = ?", (garde, doublon))
    # what is left already exists under the kept club
    for id_eq, numero, niveau, id_cat, saison in conn.execute(
        "SELECT id, numero_equipe, niveau, id_categorie_age, saison "
        "FROM EQUIPE WHERE id_club = ?", (doublon,)
    ).fetchall():
        (id_garde,) = conn.execute(
            "SELECT id FROM EQUIPE WHERE id_club = ? AND numero_equipe = ? AND niveau = ? "
            "AND id_categorie_age = ? AND saison = ?",
            (garde, numero, niveau, id_cat, saison),
        ).fetchone()
        fusionner_equipes(conn, id_garde, id_eq)
    conn.execute("DELETE FROM CLUB WHERE id = ?", (doublon,))


def fusionner_gymnastes(conn, garde: int, doublon: int) -> None:
    for table, colonne in (("RESULTAT_INDIVIDUEL", "id_gymnaste"), ("COMPOSITION_EQUIPE", "id_gymnaste")):
        conn.execute(f"UPDATE OR IGNORE {table} SET {colonne} = ? WHERE {colonne} = ?", (garde, doublon))
        # what is left already exists for the kept gymnast
        conn.execute(f"DELETE FROM {table} WHERE {colonne} = ?", (doublon,))
    conn.execute("DELETE FROM GYMNASTE WHERE id = ?", (doublon,))


def supprimer_competition(conn, id_competition: int) -> None:
    palmares = "SELECT id FROM PALMARES WHERE id_competition = ?"
    conn.execute(
        f"DELETE FROM COMPOSITION_EQUIPE WHERE id_resultat_equipe IN "
        f"(SELECT id FROM RESULTAT_EQUIPE WHERE id_palmares IN ({palmares}))",
        (id_competition,),
    )
    conn.execute(f"DELETE FROM RESULTAT_EQUIPE WHERE id_palmares IN ({palmares})", (id_competition,))
    conn.execute(f"DELETE FROM RESULTAT_INDIVIDUEL WHERE id_palmares IN ({palmares})", (id_competition,))
    conn.execute("DELETE FROM PALMARES WHERE id_competition = ?", (id_competition,))
    conn.execute("DELETE FROM COMPETITION WHERE id = ?", (id_competition,))


def supprimer_competitions_test(conn, bilan: Counter) -> None:
    rows = conn.execute("SELECT c.id, c.ville, p.nom FROM COMPETITION c JOIN PHASE p ON p.id = c.id_phase").fetchall()
    for id_comp, ville, phase in rows:
        if normaliser(phase) in PHASES_TEST or RE_VILLE_TEST.search(ville):
            logger.info("Compétition de test supprimée : id=%s, %s, %s", id_comp, ville, phase)
            supprimer_competition(conn, id_comp)
            bilan["compétitions de test supprimées"] += 1


def nettoyer_phases(conn, bilan: Counter) -> None:
    groupes = defaultdict(list)
    for id_phase, nom in conn.execute("SELECT id, nom FROM PHASE ORDER BY id").fetchall():
        groupes[harmoniser_phase(nom)].append((id_phase, nom))

    for nom, phases in groupes.items():
        garde = phases[0][0]
        for doublon, _ in phases[1:]:
            conn.execute("UPDATE COMPETITION SET id_phase = ? WHERE id_phase = ?", (garde, doublon))
            conn.execute("DELETE FROM PHASE WHERE id = ?", (doublon,))
            bilan["phases fusionnées"] += 1
        if phases[0][1] != nom:
            conn.execute("UPDATE PHASE SET nom = ? WHERE id = ?", (nom, garde))
            bilan["phases renommées"] += 1


def nettoyer_clubs(conn, bilan: Counter) -> None:
    groupes = defaultdict(list)
    rows = conn.execute("SELECT id, nom, ville, departement FROM CLUB ORDER BY id").fetchall()
    for id_club, nom, ville, dep in rows:
        nom = nettoyer_nom_lieu(nom, CLUB_INCONNU)
        groupes[normaliser(nom)].append((id_club, nom, nettoyer_nom_lieu(ville, VILLE_INCONNUE),
                                         nettoyer_departement(dep)))

    for clubs in groupes.values():
        garde = clubs[0][0]
        nom = max((c[1] for c in clubs), key=score_graphie)
        # first known city and department of the group
        ville = next((c[2] for c in clubs if c[2] != VILLE_INCONNUE), VILLE_INCONNUE)
        dep = next((c[3] for c in clubs if c[3] != DEPARTEMENT_INCONNU), DEPARTEMENT_INCONNU)
        for doublon, *_ in clubs[1:]:
            fusionner_clubs(conn, garde, doublon)
            bilan["clubs fusionnés"] += 1
        cur = conn.execute(
            "UPDATE CLUB SET nom = ?, ville = ?, departement = ? WHERE id = ? "
            "AND (nom <> ? OR ville <> ? OR departement <> ?)",
            (nom, ville, dep, garde, nom, ville, dep),
        )
        bilan["clubs corrigés"] += cur.rowcount


def nettoyer_saisons(conn, bilan: Counter) -> None:
    groupes = defaultdict(list)  # UQ_equipe key with the cleaned season
    for id_eq, id_club, numero, niveau, id_cat, saison in conn.execute(
        "SELECT id, id_club, numero_equipe, niveau, id_categorie_age, saison "
        "FROM EQUIPE ORDER BY id"
    ).fetchall():
        groupes[(id_club, numero, niveau, id_cat, harmoniser_saison(saison))].append((id_eq, saison))

    for cle, equipes in groupes.items():
        garde = equipes[0][0]
        for doublon, _ in equipes[1:]:
            fusionner_equipes(conn, garde, doublon)
            bilan["équipes fusionnées"] += 1
        if equipes[0][1] != cle[-1]:
            conn.execute("UPDATE EQUIPE SET saison = ? WHERE id = ?", (cle[-1], garde))
            bilan["saisons corrigées"] += 1


def nettoyer_villes(conn, bilan: Counter) -> None:
    # one spelling per city across CLUB and COMPETITION ("CHATEAUROUX" -> "CHÂTEAUROUX")
    lignes = [
        (table, id_, ville, nettoyer_nom_lieu(ville, VILLE_INCONNUE))
        for table in ("CLUB", "COMPETITION")
        for id_, ville in conn.execute(f"SELECT id, ville FROM {table}").fetchall()
    ]
    variantes = defaultdict(Counter)
    for *_, propre in lignes:
        variantes[normaliser(propre)][propre] += 1
    graphie = {
        cle: max(compte, key=lambda v: (score_graphie(v), compte[v]))
        for cle, compte in variantes.items()
    }
    for table, id_, ville, propre in lignes:
        retenue = graphie[normaliser(propre)]
        if retenue != ville:
            conn.execute(f"UPDATE {table} SET ville = ? WHERE id = ?", (retenue, id_))
            bilan[f"villes corrigées ({table})"] += 1


def nettoyer_gymnastes(conn, bilan: Counter, anomalies: dict) -> None:
    premiere_annee = dict(conn.execute(
        """SELECT ri.id_gymnaste, MIN(c.annee) FROM RESULTAT_INDIVIDUEL ri
           JOIN PALMARES p ON p.id = ri.id_palmares
           JOIN COMPETITION c ON c.id = p.id_competition
           GROUP BY ri.id_gymnaste"""
    ).fetchall())

    def annee_plausible(id_gym: int, annee: int) -> bool:
        limite = premiere_annee.get(id_gym, 9999) - AGE_MIN_GYMNASTE
        return ANNEE_NAISSANCE_MIN <= annee <= limite

    gymnastes = []  # (id, nom, prenom, annee, clean nom, clean prenom)
    rows = conn.execute("SELECT id, nom, prenom, annee_naissance FROM GYMNASTE ORDER BY id").fetchall()
    for id_gym, nom, prenom, annee in rows:
        nom_coupe, prenom_coupe = separer_nom_coupe(nom, prenom)
        if nom_coupe != nom:
            bilan["noms composés recollés"] += 1
        gymnastes.append((id_gym, nom, prenom, annee, nettoyer_nom(nom_coupe), harmoniser_prenom(prenom_coupe)))

    # impossible birth year: use the namesake's one if there is only one
    annees_homonymes = defaultdict(set)
    for id_gym, _, _, annee, nom, prenom in gymnastes:
        if annee_plausible(id_gym, annee):
            annees_homonymes[(normaliser(nom), normaliser(prenom))].add(annee)

    groupes = defaultdict(list)
    for g in gymnastes:
        id_gym, _, _, annee, nom, prenom = g
        cle_nom = (normaliser(nom), normaliser(prenom))
        if not annee_plausible(id_gym, annee):
            candidates = annees_homonymes[cle_nom]
            if len(candidates) == 1:
                annee = next(iter(candidates))
                bilan["années de naissance corrigées"] += 1
            else:
                anomalies["année de naissance impossible"].append(g[:4])
        groupes[(*cle_nom, annee)].append((g, annee))

    for (_, _, annee), membres in groupes.items():
        garde, nom_av, prenom_av, annee_av, _, _ = membres[0][0]
        nom, prenom = max(((g[4], g[5]) for g, _ in membres), key=lambda np: score_graphie(np[0] + np[1]))
        for g, _ in membres[1:]:
            fusionner_gymnastes(conn, garde, g[0])
            bilan["gymnastes fusionnées"] += 1
        if (nom_av, prenom_av, annee_av) != (nom, prenom, annee):
            conn.execute("UPDATE GYMNASTE SET nom = ?, prenom = ?, annee_naissance = ? WHERE id = ?",
                         (nom, prenom, annee, garde))
            bilan["gymnastes corrigées"] += 1

        if not prenom or not nom:
            anomalies["nom ou prénom vide"].append((garde, nom, prenom, annee))
        elif re.search(r"[^\w '\-]|[\d_]", nom + prenom):
            anomalies["caractère suspect dans le nom"].append((garde, nom, prenom, annee))


def supprimer_orphelins(conn, bilan: Counter) -> None:
    # in dependency order, since a deletion can create new orphans
    requetes = {
        "palmarès sans résultat": """DELETE FROM PALMARES WHERE
            id NOT IN (SELECT id_palmares FROM RESULTAT_INDIVIDUEL)
            AND id NOT IN (SELECT id_palmares FROM RESULTAT_EQUIPE)""",
        "compétitions sans palmarès": """DELETE FROM COMPETITION WHERE
            id NOT IN (SELECT id_competition FROM PALMARES)""",
        "équipes sans résultat": """DELETE FROM EQUIPE WHERE
            id NOT IN (SELECT id_equipe FROM RESULTAT_EQUIPE)""",
        "clubs inutilisés": """DELETE FROM CLUB WHERE
            id NOT IN (SELECT id_club FROM RESULTAT_INDIVIDUEL)
            AND id NOT IN (SELECT id_club FROM EQUIPE)""",
        "gymnastes sans résultat": """DELETE FROM GYMNASTE WHERE
            id NOT IN (SELECT id_gymnaste FROM RESULTAT_INDIVIDUEL)
            AND id NOT IN (SELECT id_gymnaste FROM COMPOSITION_EQUIPE)""",
        "catégories d'âge inutilisées": """DELETE FROM CATEGORIE_AGE WHERE
            id NOT IN (SELECT id_categorie_age FROM PALMARES)
            AND id NOT IN (SELECT id_categorie_age FROM EQUIPE)""",
        "phases inutilisées": """DELETE FROM PHASE WHERE
            id NOT IN (SELECT id_phase FROM COMPETITION)""",
    }
    for libelle, requete in requetes.items():
        bilan[f"{libelle} supprimé(e)s"] += conn.execute(requete).rowcount


def signaler_notes(conn, anomalies: dict) -> None:
    for table in ("RESULTAT_INDIVIDUEL", "RESULTAT_EQUIPE"):
        anomalies[f"note négative ({table})"] = conn.execute(
            f"""SELECT id, total, note_saut, note_barres, note_poutre, note_sol FROM {table}
                WHERE MIN(total, note_saut, note_barres, note_poutre, note_sol) < 0"""
        ).fetchall()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", default=str(DB_PATH), help=f"base SQLite (défaut : {DB_PATH.name})")
    ap.add_argument("--dry-run", action="store_true", help="affiche le bilan sans rien écrire")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    bilan: Counter = Counter()
    anomalies: dict[str, list] = defaultdict(list)
    try:
        supprimer_competitions_test(conn, bilan)
        nettoyer_phases(conn, bilan)
        # clubs before seasons: merging clubs can create duplicate teams
        nettoyer_clubs(conn, bilan)
        nettoyer_saisons(conn, bilan)
        nettoyer_villes(conn, bilan)
        nettoyer_gymnastes(conn, bilan, anomalies)
        supprimer_orphelins(conn, bilan)
        signaler_notes(conn, anomalies)

        orphelins = conn.execute("PRAGMA foreign_key_check").fetchall()
        if orphelins:
            raise RuntimeError(f"Références cassées après nettoyage : {orphelins[:10]}")

        if args.dry_run:
            conn.rollback()
        else:
            conn.commit()
    except Exception:
        conn.rollback()
        logger.exception("Nettoyage annulé, la base n'a pas été modifiée")
        raise
    finally:
        conn.close()

    logger.info("Bilan%s :", " (dry-run, rien n'a été écrit)" if args.dry_run else "")
    for etape, nb in bilan.items():
        if nb:
            logger.info("  %-45s %s", etape, nb)

    for type_anomalie, lignes in anomalies.items():
        if not lignes:
            continue
        logger.warning("À vérifier à la main - %s : %s ligne(s)", type_anomalie, len(lignes))
        for ligne in lignes[:NB_ANOMALIES_AFFICHEES]:
            logger.warning("    %s", ligne)


if __name__ == "__main__":
    main()
