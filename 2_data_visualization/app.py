import pandas as pd
import streamlit as st
from streamlit_searchbox import st_searchbox

from data import FILTERS, load_data, normalize, normalized_labels
from views import club_view, gymnast_view, team_view

MAX_SUGGESTIONS = 1000


### FILTERS ###

def init_state():
    if "filters" not in st.session_state:
        st.session_state.filters = {col: [] for col in FILTERS}


def mask_for(df, exclude=None):
    mask = pd.Series(True, index=df.index)
    for col, values in st.session_state.filters.items():
        if col != exclude and values and col in df:
            mask &= df[col].isin(values)
    return mask


def available_options(df, col):
    present = df.loc[mask_for(df, exclude=col), col].cat.remove_unused_categories()
    selected = set(st.session_state.filters[col])
    return [v for v in present.cat.categories if v not in selected]


def make_search_function(df, col, labels):
    def search(term):
        options = available_options(df, col)
        tokens = normalize(term or "").split()
        if tokens:
            options = [o for o in options if all(t in labels[o] for t in tokens)]
            options.sort(key=lambda o: not labels[o].startswith(tokens[0]))
        return options[:MAX_SUGGESTIONS]

    return search


def reset_searchboxes():
    for col in FILTERS:
        st.session_state.pop(f"searchbox_{col}", None)


def remove_filter(col, value):
    st.session_state.filters[col].remove(value)
    reset_searchboxes()


def clear_filters():
    st.session_state.filters = {col: [] for col in FILTERS}
    reset_searchboxes()


def filter_searchbox(df, col, labels):
    search = make_search_function(df, col, labels[col])
    selected = st_searchbox(search, label=FILTERS[col], placeholder="Rechercher...", default_options=search(""),
                            key=f"searchbox_{col}")
    if selected is not None:
        st.session_state.filters[col].append(selected)
        reset_searchboxes()
        st.rerun()

    for value in st.session_state.filters[col]:
        st.button(f"✕ {value}", key=f"remove_{col}_{value}", on_click=remove_filter, args=(col, value),
                  help="Supprimer ce filtre")


### APP ###

st.set_page_config(page_title="Tableau de bord UFOLEP GYM", layout="wide")
init_state()

ind, team = load_data()
labels = normalized_labels(ind)

with st.sidebar:
    st.header("Filtres")
    if any(st.session_state.filters.values()):
        st.button("Tout effacer", on_click=clear_filters, type="tertiary")
    for col in FILTERS:
        filter_searchbox(ind, col, labels)

st.header("Bienvenue sur le tableau de bord des résultats UFOLEP GYM !")

filters = st.session_state.filters
ind_f = ind[mask_for(ind)]
team_f = team[mask_for(team)]
if filters["gymnaste"]:
    team_f = team_f[team_f["id_equipe"].isin(ind_f["id_equipe"].dropna())]
teams = team_f["id_equipe"].unique()

views = {}
if len(filters["gymnaste"]) == 1:
    views["Gymnaste"] = lambda: gymnast_view(ind_f, ind, team)
if len(teams) == 1:
    views["Équipe"] = lambda: team_view(team_f, ind, team)
if len(filters["club"]) == 1:
    views["Club"] = lambda: club_view(ind_f, team_f)

if 1 < len(teams) <= 30 and any(filters.values()):
    with st.expander(f"{len(teams)} équipes correspondent aux filtres : précisez la saison, le niveau, "
                     "la catégorie ou le numéro pour en afficher une"):
        st.write(sorted(team_f["equipe"].unique()))

if not views:
    st.info(
        "Sélectionnez dans les filtres à gauche :\n"
        "- **une gymnaste** pour voir ses notes et classements ;\n"
        "- **un club** pour comparer ses gymnastes, équipes, niveaux, catégories et agrès ;\n"
        "- **une équipe**, en combinant club, saison, niveau, catégorie d'âge et numéro d'équipe."
    )
else:
    for tab, view in zip(st.tabs(list(views)), views.values()):
        with tab:
            view()
