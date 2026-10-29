import sqlite3
import unicodedata
from pathlib import Path

import numpy as np
import pandas as pd
import streamlit as st

db_path = Path(__file__).resolve().parent.parent / "1_data_extraction" / "Competitions_gym.db"

FILTERS = {
    "club": "Club",
    "saison": "Saison",
    "gymnaste": "Gymnaste",
    "phase": "Phase de compétition",
    "niveau": "Niveau",
    "categorie": "Catégorie d'âge",
    "numero": "Numéro d'équipe",
}

APPARATUS = {"saut": "Saut", "barres": "Barres", "poutre": "Poutre", "sol": "Sol"}
METRICS = {"total": "Total", **APPARATUS}
NOTES = [f"note_{m}" for m in METRICS]

SEASON = """CASE WHEN CO.mois >= 9
                 THEN CO.annee || '-' || (CO.annee + 1)
                 ELSE (CO.annee - 1) || '-' || CO.annee
            END"""

CONTEXT = f"""
        CL.nom AS club,
        {SEASON} AS saison,
        PH.nom AS phase,
        PA.id AS id_palmares,
        PA.niveau,
        CAST(CATE.age_min AS INTEGER) AS age_min,
        CAST(CATE.age_max AS INTEGER) AS age_max,
        CO.id AS id_competition,
        CO.annee,
        CO.mois,
        CO.ville"""

CONTEXT_JOINS = """
    JOIN CATEGORIE_AGE CATE ON CATE.id = PA.id_categorie_age
    JOIN COMPETITION CO ON CO.id = PA.id_competition
    JOIN PHASE PH ON PH.id = CO.id_phase"""

INDIVIDUAL_QUERY = f"""
    SELECT
        RESI.id AS id_resultat,
        GY.id AS id_gymnaste,
        GY.nom || ' ' || GY.prenom || ' (' || GY.annee_naissance || ')' AS gymnaste,
        {CONTEXT},
        RESI.total AS note_total,
        RESI.note_saut,
        RESI.note_barres,
        RESI.note_poutre,
        RESI.note_sol
    FROM RESULTAT_INDIVIDUEL RESI
    JOIN GYMNASTE GY ON GY.id = RESI.id_gymnaste
    JOIN CLUB CL ON CL.id = RESI.id_club
    JOIN PALMARES PA ON PA.id = RESI.id_palmares
    {CONTEXT_JOINS}
"""

TEAM_QUERY = f"""
    SELECT
        RESE.id AS id_resultat_equipe,
        E.id AS id_equipe,
        E.numero_equipe,
        E.saison AS saison_equipe,
        {CONTEXT},
        RESE.total AS note_total,
        RESE.note_saut,
        RESE.note_barres,
        RESE.note_poutre,
        RESE.note_sol
    FROM RESULTAT_EQUIPE RESE
    JOIN EQUIPE E ON E.id = RESE.id_equipe
    JOIN CLUB CL ON CL.id = E.id_club
    JOIN PALMARES PA ON PA.id = RESE.id_palmares
    {CONTEXT_JOINS}
"""

COMPOSITION_QUERY = """
    SELECT CE.id_gymnaste, RESE.id_palmares, RESE.id AS id_resultat_equipe
    FROM COMPOSITION_EQUIPE CE
    JOIN RESULTAT_EQUIPE RESE ON RESE.id = CE.id_resultat_equipe
"""


def add_context(df):
    age_min, age_max = df["age_min"].astype(str), df["age_max"].astype(str)
    df["categorie"] = np.where(df["age_max"] >= 99, age_min + " ans et +", age_min + "-" + age_max + " ans")
    df["niveau"] = "Niveau " + df["niveau"].astype(str)
    df["date"] = pd.to_datetime(pd.DataFrame({"year": df["annee"], "month": df["mois"], "day": 1}))
    df["competition"] = df["date"].dt.strftime("%m/%Y") + " · " + df["ville"].str.title() + " · " + df["phase"]
    df[NOTES] = df[NOTES].replace(0, np.nan)


def add_ranks(df, group, suffix=""):
    grouped = df.groupby(group)
    for m in METRICS:
        note = f"note_{m}"
        df[f"rang{suffix}_{m}"] = grouped[note].rank(method="min", ascending=False)
        df[f"nb{suffix}_{m}"] = grouped[note].transform("count")
        df[f"prop{suffix}_{m}"] = 100 * df[f"rang{suffix}_{m}"] / df[f"nb{suffix}_{m}"]


def set_categories(ind, team):
    both = pd.concat([ind, team])
    ages = both[["categorie", "age_min", "age_max"]].drop_duplicates().sort_values(["age_min", "age_max"])
    orders = {
        "club": sorted(both["club"].unique()),
        "saison": sorted(both["saison"].unique(), reverse=True),
        "gymnaste": sorted(ind["gymnaste"].unique()),
        "phase": sorted(both["phase"].unique()),
        "niveau": sorted(both["niveau"].unique(), key=lambda n: int(n.split()[-1])),
        "categorie": list(ages["categorie"]),
        "numero": sorted(both["numero"].dropna().unique(), key=lambda n: int(n.split()[-1])),
    }
    for df in (ind, team):
        for col, order in orders.items():
            if col in df:
                df[col] = pd.Categorical(df[col], categories=order)


@st.cache_resource(show_spinner="Chargement de la base de données...")
def load_data():
    with sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True) as connection:
        ind = pd.read_sql_query(INDIVIDUAL_QUERY, connection)
        team = pd.read_sql_query(TEAM_QUERY, connection)
        composition = pd.read_sql_query(COMPOSITION_QUERY, connection)

    add_context(ind)
    add_context(team)

    team["numero"] = "Équipe " + team["numero_equipe"].astype(str)
    team["equipe"] = (
        team["club"] + " · Équipe " + team["numero_equipe"].astype(str) + " · "
        + team["niveau"] + " · " + team["categorie"] + " · " + team["saison_equipe"]
    )

    composition = composition.drop_duplicates(["id_gymnaste", "id_palmares"])
    ind = ind.merge(composition, on=["id_gymnaste", "id_palmares"], how="left")
    ind = ind.merge(team[["id_resultat_equipe", "id_equipe", "numero", "equipe"]], on="id_resultat_equipe", how="left")

    add_ranks(ind, "id_palmares")
    add_ranks(ind, "id_resultat_equipe", "_eq")
    add_ranks(team, "id_palmares")

    set_categories(ind, team)
    return ind, team


def normalize(text):
    text = unicodedata.normalize("NFKD", str(text))
    return "".join(c for c in text if not unicodedata.combining(c)).lower()


@st.cache_resource
def normalized_labels(_ind):
    return {col: {v: normalize(v) for v in _ind[col].cat.categories} for col in FILTERS}
