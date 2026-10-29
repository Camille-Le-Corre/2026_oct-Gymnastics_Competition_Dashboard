import math
import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from data import APPARATUS, METRICS, NOTES

PROP_HELP = "Proportion = 100 × rang / nombre de classées. Plus c'est bas, mieux c'est (1re sur 10 → 10 %)."
INDEX_HELP = "Indice = 100 − proportion moyenne : plus la surface est grande, meilleurs sont les classements."
STAT_NAMES = {
    "count": "N", "mean": "Moyenne", "std": "Écart-type", "min": "Min",
    "25%": "Q1", "50%": "Médiane", "75%": "Q3", "max": "Max",
}
PROP_COLUMN = st.column_config.ProgressColumn(format="%.1f %%", min_value=0, max_value=100)
PHASE_HELP = (
    "Au fil d'une saison, le niveau du plateau monte avec les qualifications : les proportions ne sont "
    "donc comparées que d'une saison à l'autre, à phase égale (moyenne si plusieurs compétitions)."
)
PHASE_ORDER = [
    "Interne", "Amicale", "Petite enfance", "Départementale", "Interdépartementale", "Coupe",
    "Coupe régionale Ile de France", "Coupe Aquitaine", "Régionale", "Demi-finale",
    "Finale jeunes", "Finale jeunes G1", "Finale jeunes G2", "Finale jeunes G3", "Finale nationale",
]


### HELPERS ###

def fmt(value, digits=2, suffix=""):
    return "–" if pd.isna(value) else f"{value:.{digits}f}{suffix}"


def rank_text(rank, nb):
    return "–" if pd.isna(rank) else f"{int(rank)}/{int(nb)}"


def compact(df):
    df = df.copy()
    for col in df.select_dtypes("category"):
        df[col] = df[col].cat.remove_unused_categories()
    return df


def orders(df, *cols):
    return {col: list(df[col].cat.categories) for col in cols if isinstance(df[col].dtype, pd.CategoricalDtype)}


def stats_table(df, columns):
    table = df[list(columns)].describe().T.rename(index=columns, columns=STAT_NAMES)
    table["N"] = table["N"].astype(int)
    return table.round(2)


def ordinal(rank):
    return "1re" if rank == 1 else f"{int(rank)}e"


def reverse_prop_axis(fig):
    return fig.update_yaxes(range=[105, 0], title="Proportion de classement")


def rank_axis(fig, max_rank):
    return fig.update_yaxes(range=[math.ceil(max_rank) + 0.5, 0.5], dtick=1,
                            title="Classement dans l'équipe (1 = meilleure)")


def radar(series, title, radial_range=(0, 100)):
    fig = go.Figure()
    for name, values in series.items():
        axes, r = list(values), list(values.values())
        fig.add_trace(go.Scatterpolar(r=r + r[:1], theta=axes + axes[:1], name=name, mode="lines+markers"))
    fig.update_layout(
        title=title,
        polar=dict(radialaxis=dict(range=list(radial_range))),
        legend=dict(orientation="h", y=-0.1),
        margin=dict(t=60, b=40, l=50, r=50),
        height=430,
    )
    return fig


def apparatus_index(rows, suffix=""):
    return {label: 100 - rows[f"prop{suffix}_{m}"].mean() for m, label in APPARATUS.items()}


def score_evolution(rows, title):
    return px.line(
        rows, x="date", y="note_total", color="niveau", markers=True, hover_name="competition",
        hover_data={"date": False, "categorie": True}, category_orders=orders(rows, "niveau"),
        labels={"note_total": "Note totale", "date": "", "niveau": "Niveau", "categorie": "Catégorie"},
        title=title,
    )


def apparatus_evolution(rows, title):
    long = rows.melt(id_vars=["date", "competition"], value_vars=[f"note_{m}" for m in APPARATUS],
                     var_name="agrès", value_name="note").dropna(subset=["note"])
    long["agrès"] = long["agrès"].str.removeprefix("note_").map(APPARATUS)
    return px.line(long, x="date", y="note", color="agrès", markers=True, hover_name="competition",
                   labels={"date": "", "note": "Note"}, title=title)


def phase_order(rows):
    present = set(rows["phase"].astype(str))
    return [p for p in PHASE_ORDER if p in present] + sorted(present - set(PHASE_ORDER))


def by_season(rows, columns):
    long = rows.astype({"saison": str, "phase": str}).melt(
        id_vars=["saison", "phase"], value_vars=list(columns), var_name="série", value_name="valeur"
    ).dropna(subset=["valeur"])
    long["série"] = long["série"].map(columns)
    return (long.groupby(["saison", "phase", "série"])["valeur"]
            .agg(valeur="mean", competitions="count").reset_index().sort_values("saison"))


def season_axis(fig, data, column):
    fig.update_xaxes(type="category", categoryorder="array", categoryarray=sorted(data["saison"].unique()))
    if column.startswith("rang"):
        return rank_axis(fig, data["valeur"].max())
    return reverse_prop_axis(fig)


def ranking_by_phase(rows, column, title):
    data = by_season(rows, {column: ""})
    fig = px.line(
        data, x="saison", y="valeur", color="phase", markers=True,
        category_orders={"phase": phase_order(rows)}, hover_data={"competitions": True},
        labels={"saison": "", "phase": "Phase", "competitions": "Compétitions"}, title=title,
    )
    return season_axis(fig, data, column)


def apparatus_ranking_by_phase(rows, value, title, key):
    phases = phase_order(rows)
    most_frequent = rows["phase"].astype(str).value_counts().idxmax()
    phase = st.selectbox("Phase", phases, index=phases.index(most_frequent), key=key)
    data = by_season(rows[rows["phase"] == phase], {f"{value}_{m}": label for m, label in APPARATUS.items()})
    fig = px.line(data, x="saison", y="valeur", color="série", markers=True, hover_data={"competitions": True},
                  labels={"saison": "", "série": "", "competitions": "Compétitions"}, title=f"{title} · {phase}")
    return season_axis(fig, data, value)


def phase_bars(rows, title):
    phases = phase_order(rows)
    grouped = rows.groupby(rows["phase"].astype(str))
    means = grouped[NOTES].mean().reindex(phases).rename(columns={f"note_{m}": l for m, l in METRICS.items()})
    long = means.reset_index(names="phase").melt(id_vars="phase", var_name="note", value_name="moyenne")
    long["compétitions"] = long["phase"].map(grouped.size())
    fig = px.bar(
        long, x="phase", y="moyenne", color="note", barmode="group", text_auto=".2f",
        category_orders={"phase": phases, "note": list(METRICS.values())},
        color_discrete_map={"Total": "#7f7f7f"}, hover_data={"compétitions": True},
        labels={"phase": "", "note": "", "moyenne": "Note moyenne"}, title=title,
    )
    fig.update_traces(textposition="outside", cliponaxis=False)
    fig.update_layout(height=450, legend=dict(orientation="h", y=-0.12))
    return fig


def heatmap(z, text, title, legend, mean_format=None, **scale):
    y, values, text = list(z.index), z.to_numpy(dtype=float), list(text)
    if mean_format and len(z) > 1:
        mean = z.mean()
        values = np.vstack([mean.to_numpy(dtype=float), values])
        text = [[mean_format.format(v) if pd.notna(v) else "–" for v in mean]] + text
        y = ["<b>Moyenne</b>"] + y
    fig = go.Figure(go.Heatmap(
        z=values, x=list(z.columns), y=y, text=text, texttemplate="%{text}", colorbar=dict(title=legend),
        hovertemplate="%{y}<br>%{x} : %{text}<extra></extra>", **scale,
    ))
    fig.update_layout(title=title, height=max(300, 34 * len(y) + 130), yaxis=dict(autorange="reversed"))
    return fig


def rank_heatmap(rows, suffix, title):
    rows = rows.sort_values("date", ascending=False)
    text = [[rank_text(r[f"rang{suffix}_{m}"], r[f"nb{suffix}_{m}"]) for m in METRICS] for _, r in rows.iterrows()]
    if suffix == "_eq":
        z, legend, mean_format = f"rang{suffix}", "Rang", "moy. {:.1f}"
        scale = dict(zmin=1, zmax=rows[f"nb{suffix}_total"].max())
    else:
        z, legend, mean_format = f"prop{suffix}", "Prop. %", "{:.1f} %"
        scale = dict(zmin=0, zmax=100)
    values = rows[[f"{z}_{m}" for m in METRICS]].set_axis(list(METRICS.values()), axis=1)
    return heatmap(values.set_axis(list(rows["competition"])), text, title, legend, mean_format,
                   colorscale="RdYlGn_r", **scale)


def results_table(rows, suffixes):
    rows = rows.sort_values("date", ascending=False)
    table = pd.DataFrame({"Compétition": rows["competition"], "Niveau": rows["niveau"], "Catégorie": rows["categorie"]})
    for m, label in METRICS.items():
        table[label] = rows[f"note_{m}"]
        for suffix, name in suffixes.items():
            table[f"{label} · rang{name}"] = [
                rank_text(r, n) for r, n in zip(rows[f"rang{suffix}_{m}"], rows[f"nb{suffix}_{m}"])
            ]
            if suffix != "_eq":
                table[f"{label} · prop.{name} (%)"] = rows[f"prop{suffix}_{m}"].round(1)
    return table


def rank_distribution(rows, title):
    long = rows.melt(value_vars=[f"rang_eq_{m}" for m in METRICS], var_name="agrès", value_name="rang").dropna()
    long["agrès"] = long["agrès"].str.removeprefix("rang_eq_").map(METRICS)
    counts = long.groupby(["agrès", "rang"]).size().reset_index(name="Compétitions")
    ranks = sorted(counts["rang"].unique())
    counts["Classement"] = counts["rang"].map(ordinal)
    colors = px.colors.sample_colorscale("RdYlGn_r", [i / max(len(ranks) - 1, 1) for i in range(len(ranks))])
    return px.bar(
        counts, x="agrès", y="Compétitions", color="Classement", title=title,
        category_orders={"agrès": list(METRICS.values()), "Classement": [ordinal(r) for r in ranks]},
        color_discrete_sequence=colors, labels={"agrès": ""},
    )


def ranking_table(rows, by, min_competitions, extra=None):
    g = rows.groupby(by, observed=True)
    table = pd.DataFrame({
        **(extra(g) if extra else {}),
        "Compétitions": g["note_total"].count(),
        "Total moyen": g["note_total"].mean(),
        "Prop. moyenne (%)": g["prop_total"].mean(),
        "Prop. médiane (%)": g["prop_total"].median(),
        "Écart-type prop.": g["prop_total"].std(),
        "Meilleure prop. (%)": g["prop_total"].min(),
        **{f"Prop. {label} (%)": g[f"prop_{m}"].mean() for m, label in APPARATUS.items()},
    })
    table = table[table["Compétitions"] >= min_competitions].sort_values("Prop. moyenne (%)")
    table.insert(0, "Rang", range(1, len(table) + 1))
    return table.round(2)


def show_ranking(table, index_label):
    st.dataframe(table.rename_axis(index_label), column_config={"Prop. moyenne (%)": PROP_COLUMN})


def ranking_bars(table, label, color=None, top=30, title=""):
    data = table.head(top).reset_index()
    fig = px.bar(data, x="Prop. moyenne (%)", y=label, color=color, orientation="h",
                 error_x="Écart-type prop.", hover_data=["Compétitions", "Prop. médiane (%)"], title=title)

    fig.update_traces(texttemplate="%{x:.1f} %", textposition="inside", insidetextanchor="end",
                      error_x=dict(color="rgba(110, 110, 110, 0.55)", thickness=1, width=3))
    fig.update_layout(height=max(300, 30 * len(data) + 120), bargap=0.12,
                      yaxis=dict(autorange="reversed", title=""))
    fig.update_xaxes(title="Proportion moyenne (%) · plus court = mieux")
    return fig


def no_data():
    st.info("Aucune note pour les filtres sélectionnés.")


### GYMNAST ###

def gymnast_view(rows, ind, team):
    rows = compact(rows.dropna(subset=["note_total"]).sort_values("date"))
    if rows.empty:
        return no_data()

    st.subheader(rows["gymnaste"].iloc[0])
    st.caption(
        f"Club(s) : {', '.join(rows['club'].astype(str).unique())} · "
        f"Saisons : {', '.join(sorted(rows['saison'].astype(str).unique()))}"
    )
    k = st.columns(5)
    k[0].metric("Compétitions", len(rows))
    k[1].metric("Total moyen", fmt(rows["note_total"].mean()),
                help=f"Écart-type : {fmt(rows['note_total'].std())}")
    k[2].metric("Meilleur total", fmt(rows["note_total"].max()))
    k[3].metric("Prop. moyenne · palmarès", fmt(rows["prop_total"].mean(), 1, " %"), help=PROP_HELP)
    k[4].metric("Rang moyen dans l'équipe", fmt(rows["rang_eq_total"].mean(), 1),
                help="Classement au total parmi les filles de son équipe (1 = meilleure).")

    tab_evol, tab_app, tab_team, tab_detail = st.tabs(["Évolution", "Par agrès", "Au sein de l'équipe",
                                                       "Détail par compétition"])

    with tab_evol:
        st.plotly_chart(phase_bars(rows, "Notes moyennes par phase (saisons des filtres)"))
        c1, c2 = st.columns(2)
        c1.plotly_chart(score_evolution(rows, "Note totale"))
        c2.plotly_chart(ranking_by_phase(rows, "prop_total", "Classement au total face au palmarès"))
        c1, c2 = st.columns(2)
        c1.plotly_chart(apparatus_evolution(rows, "Notes par agrès"))
        with c2:
            st.plotly_chart(apparatus_ranking_by_phase(rows, "prop", "Classement par agrès face au palmarès",
                                                       key="gymnast_phase"))
        st.caption(PHASE_HELP)

    with tab_app:
        c1, c2 = st.columns(2)
        c1.plotly_chart(radar({"Face au palmarès": apparatus_index(rows)}, "Indice de classement moyen par agrès"))
        c1.caption(INDEX_HELP)
        long = rows.melt(value_vars=[f"note_{m}" for m in APPARATUS], var_name="agrès", value_name="note")
        long["agrès"] = long["agrès"].str.removeprefix("note_").map(APPARATUS)
        c2.plotly_chart(px.box(long.dropna(), x="agrès", y="note", points="all",
                               title="Distribution des notes par agrès", labels={"agrès": ""}))
        st.markdown("**Notes**")
        st.dataframe(stats_table(rows, {f"note_{m}": label for m, label in METRICS.items()}))
        st.markdown("**Proportions de classement face au palmarès (%)**")
        st.dataframe(stats_table(rows, {f"prop_{m}": label for m, label in METRICS.items()}))
        st.plotly_chart(rank_heatmap(rows, "", "Classements face aux individuelles du palmarès"))

    with tab_team:
        in_team = rows.dropna(subset=["id_resultat_equipe"])
        if in_team.empty:
            st.info("Aucune compétition en équipe pour les filtres sélectionnés.")
        else:
            mates = ind[ind["id_resultat_equipe"].isin(in_team["id_resultat_equipe"])
                        & (ind["id_gymnaste"] != in_team["id_gymnaste"].iloc[0])]
            compare = pd.DataFrame({
                "Elle": in_team[[f"note_{m}" for m in APPARATUS]].mean().values,
                "Moyenne des coéquipières": mates[[f"note_{m}" for m in APPARATUS]].mean().values,
            }, index=list(APPARATUS.values()))
            c1, c2 = st.columns(2)
            fig = px.bar(compare.reset_index(names="agrès").melt(id_vars="agrès", var_name="qui", value_name="note"),
                         x="agrès", y="note", color="qui", barmode="group", text_auto=".2f",
                         title="Note moyenne par agrès face aux coéquipières",
                         labels={"agrès": "", "qui": "", "note": "Note moyenne"})
            c1.plotly_chart(fig)
            c2.plotly_chart(rank_distribution(in_team, "Ses classements dans l'équipe, par agrès"))
            c1, c2 = st.columns(2)
            c1.plotly_chart(ranking_by_phase(in_team, "rang_eq_total", "Classement au total dans l'équipe"))
            with c2:
                st.plotly_chart(apparatus_ranking_by_phase(in_team, "rang_eq", "Classement par agrès dans l'équipe",
                                                           key="gymnast_team_phase"))
            st.caption("Rang moyen dans l'équipe, par saison et à phase égale (moyenne si plusieurs compétitions).")
            st.plotly_chart(rank_heatmap(in_team, "_eq", "Classements au sein de l'équipe"))
            st.markdown("**Rangs dans l'équipe**")
            st.dataframe(stats_table(in_team, {f"rang_eq_{m}": label for m, label in METRICS.items()}))

            team_rows = team.set_index("id_resultat_equipe").loc[in_team["id_resultat_equipe"].astype(int)]
            st.markdown("**Résultats de ses équipes**")
            st.dataframe(pd.DataFrame({
                "Compétition": in_team["competition"].values,
                "Équipe": in_team["equipe"].values,
                "Total équipe": team_rows["note_total"].values,
                "Classement équipe": [rank_text(r, n) for r, n in zip(team_rows["rang_total"], team_rows["nb_total"])],
                "Prop. équipe (%)": team_rows["prop_total"].round(1).values,
                "Son rang dans l'équipe": [rank_text(r, n) for r, n in zip(in_team["rang_eq_total"], in_team["nb_eq_total"])],
            }).iloc[::-1], hide_index=True, column_config={"Prop. équipe (%)": PROP_COLUMN})

    with tab_detail:
        st.caption(PROP_HELP + " « rang équipe » : rang parmi les filles de son équipe.")
        st.dataframe(results_table(rows, {"": "", "_eq": " équipe"}), hide_index=True)


### TEAM ###

def team_view(rows, ind, team):
    rows = compact(rows.dropna(subset=["note_total"]).sort_values("date"))
    if rows.empty:
        return no_data()

    st.subheader(rows["equipe"].iloc[0])
    k = st.columns(5)
    k[0].metric("Compétitions", len(rows))
    k[1].metric("Total moyen", fmt(rows["note_total"].mean()))
    k[2].metric("Écart-type total", fmt(rows["note_total"].std()))
    k[3].metric("Classement moyen", fmt(rows["rang_total"].mean(), 1))
    k[4].metric("Prop. moyenne", fmt(rows["prop_total"].mean(), 1, " %"), help=PROP_HELP)

    tab_res, tab_palmares, tab_profiles = st.tabs(["Résultats", "Face aux autres équipes", "Profils des gymnastes"])

    with tab_res:
        st.plotly_chart(phase_bars(rows, "Notes moyennes de l'équipe par phase (saisons des filtres)"))
        c1, c2 = st.columns(2)
        c1.plotly_chart(score_evolution(rows, "Note totale de l'équipe"))
        c2.plotly_chart(ranking_by_phase(rows, "prop_total", "Classement au total face aux équipes du palmarès"))
        c1, c2 = st.columns(2)
        c1.plotly_chart(apparatus_evolution(rows, "Notes par agrès"))
        with c2:
            st.plotly_chart(apparatus_ranking_by_phase(rows, "prop", "Classement par agrès face aux équipes",
                                                       key="team_phase"))
        st.caption(PHASE_HELP)
        c1, _ = st.columns(2)
        c1.plotly_chart(radar({"Équipe": apparatus_index(rows)}, "Indice de classement moyen par agrès"))
        c1.caption(INDEX_HELP)
        st.markdown("**Notes**")
        st.dataframe(stats_table(rows, {f"note_{m}": label for m, label in METRICS.items()}))
        st.markdown("**Proportions de classement (%)**")
        st.dataframe(stats_table(rows, {f"prop_{m}": label for m, label in METRICS.items()}))
        st.plotly_chart(rank_heatmap(rows, "", "Classements face aux équipes du palmarès"))
        st.dataframe(results_table(rows, {"": ""}), hide_index=True)

    with tab_palmares:
        choice = st.selectbox("Compétition", rows["competition"].iloc[::-1], key="team_palmares")
        selected = rows[rows["competition"] == choice].iloc[0]
        others = team[team["id_palmares"] == selected["id_palmares"]].dropna(subset=["note_total"])
        others = others.sort_values("note_total", ascending=False)
        names = (others["club"].astype(str) + " · Éq. " + others["numero_equipe"].astype(str)).where(
            others["id_equipe"] != selected["id_equipe"],
            "▶ " + others["club"].astype(str) + " · Éq. " + others["numero_equipe"].astype(str),
        )
        others = others.assign(nom=names.values)
        table = results_table(others.assign(competition=others["nom"]), {"": ""})
        st.dataframe(table.rename(columns={"Compétition": "Équipe"})
                     .drop(columns=["Niveau", "Catégorie"])
                     .sort_values("Total", ascending=False),
                     hide_index=True)

    with tab_profiles:
        members = compact(ind[ind["id_resultat_equipe"].isin(rows["id_resultat_equipe"])])
        options = ["Moyenne sur toutes les compétitions"] + list(rows["competition"].iloc[::-1])
        choice = st.selectbox("Compétition", options, key="team_profiles")
        if choice != options[0]:
            members = members[members["competition"] == choice]
        if members.empty:
            return st.info("Composition inconnue pour cette sélection.")

        profiles = members.groupby("gymnaste", observed=True)[NOTES].mean()
        notes = profiles[[f"note_{m}" for m in APPARATUS]].set_axis(list(APPARATUS.values()), axis=1)
        team_mean = notes.mean()
        gaps = notes - team_mean

        c1, c2 = st.columns(2)
        series = {g: v.to_dict() for g, v in notes.iterrows()}
        series["Moyenne de l'équipe"] = team_mean.to_dict()
        c1.plotly_chart(radar(series, "Profil des gymnastes par agrès (note moyenne)",
                              (max(0, math.floor(notes.min().min()) - 1), math.ceil(notes.max().max()))))
        c1.caption("L'axe ne démarre pas à 0 pour mieux distinguer les profils.")
        text = [[f"{n:.2f}<br>({d:+.2f})" if pd.notna(n) else "–" for n, d in zip(rn, rd)]
                for rn, rd in zip(notes.to_numpy(), gaps.to_numpy())]
        c2.plotly_chart(heatmap(
            gaps, text, "Points forts et points faibles<br><sup>note moyenne (écart à la moyenne de l'équipe)</sup>",
            "Écart", colorscale="RdYlGn", zmid=0,
        ))
        c2.caption("Vert : au-dessus de la moyenne de l'équipe à cet agrès ; rouge : en dessous. Une équipe "
                   "complémentaire a du vert dans chaque colonne, porté par des gymnastes différentes.")

        c1, c2 = st.columns(2)
        long = notes.reset_index().melt(id_vars="gymnaste", var_name="agrès", value_name="note").dropna()
        fig = px.scatter(long, x="agrès", y="note", color="gymnaste", hover_name="gymnaste",
                         title="Où se place chaque gymnaste à chaque agrès", labels={"agrès": "", "gymnaste": ""})
        fig.update_traces(marker=dict(size=13, line=dict(width=1, color="white")))
        fig.add_trace(go.Scatter(x=list(team_mean.index), y=team_mean.values, mode="markers", name="Moyenne de l'équipe",
                                 marker=dict(symbol="line-ew-open", size=45, color="black", line=dict(width=2))))
        c1.plotly_chart(fig)
        c1.caption("Points serrés : agrès homogène. Points dispersés : l'équipe dépend de quelques gymnastes.")

        kept_count = members["niveau"].astype(str).str.split().str[-1].astype(int).map(lambda n: 3 if n <= 4 else 4)
        kept = pd.DataFrame({label: members[f"rang_eq_{m}"] <= kept_count for m, label in APPARATUS.items()})
        kept["gymnaste"] = members["gymnaste"]
        kept = kept.groupby("gymnaste", observed=True).sum()
        passages = (members.groupby("gymnaste", observed=True)[[f"note_{m}" for m in APPARATUS]].count()
                    .set_axis(list(APPARATUS.values()), axis=1))
        text = [[f"{k}/{p}" if p else "–" for k, p in zip(rk, rp)]
                for rk, rp in zip(kept.to_numpy(), passages.to_numpy())]
        c2.plotly_chart(heatmap(
            100 * kept / passages.replace(0, np.nan), text, "Notes retenues dans la note de l'équipe",
            "% retenues", colorscale="Blues", zmin=0, zmax=100,
        ))
        c2.caption("Note d'équipe à un agrès = somme des 3 meilleures notes (niveaux 1 à 4) ou des 4 meilleures "
                   "(niveaux 5 à 8). Case : nombre de fois où la note de la gymnaste a compté / nombre de passages. "
                   "Une équipe complémentaire répartit les cases foncées entre plusieurs gymnastes.")

        g = members.groupby("gymnaste", observed=True)
        best = lambda row: row.idxmax() if row.notna().any() else None
        worst = lambda row: row.idxmin() if row.notna().any() else None
        summary = profiles.rename(columns={f"note_{m}": label for m, label in METRICS.items()}).assign(**{
            "Point fort": gaps.apply(best, axis=1),
            "Point faible": gaps.apply(worst, axis=1),
            "Notes retenues (%)": 100 * kept.sum(axis=1) / passages.sum(axis=1).replace(0, np.nan),
            "Compétitions": g["note_total"].count(),
            "Écart-type total": g["note_total"].std(),
            "Rang moyen dans l'équipe": g["rang_eq_total"].mean(),
            "Prop. moyenne palmarès (%)": g["prop_total"].mean(),
        }).sort_values("Total", ascending=False).round(2)
        st.markdown("**Synthèse par gymnaste** (point fort / faible : agrès le plus au-dessus / en dessous de la moyenne de l'équipe)")
        st.dataframe(summary, column_config={"Prop. moyenne palmarès (%)": PROP_COLUMN})


### CLUB ###

def group_comparison(ind_rows, team_rows, col, label):
    box = pd.concat([
        ind_rows[[col, "prop_total"]].assign(type="Individuel"),
        team_rows[[col, "prop_total"]].assign(type="Équipe"),
    ]).dropna(subset=["prop_total"])
    fig = px.box(box, x=col, y="prop_total", color="type", category_orders=orders(ind_rows, col),
                 title=f"Distribution des proportions par {label.lower()}", labels={col: "", "type": ""})
    st.plotly_chart(reverse_prop_axis(fig))
    st.caption(PROP_HELP)

    extra = lambda g: {"Gymnastes": g["id_gymnaste"].nunique()}
    st.markdown("**Individuel**")
    show_ranking(ranking_table(ind_rows, col, 1, extra), label)
    st.markdown("**Équipes**")
    show_ranking(ranking_table(team_rows, col, 1, lambda g: {"Équipes": g["id_equipe"].nunique()}), label)


def club_view(ind_rows, team_rows):
    ind_rows = compact(ind_rows.dropna(subset=["note_total"]))
    team_rows = compact(team_rows.dropna(subset=["note_total"]))
    if ind_rows.empty and team_rows.empty:
        return no_data()

    st.subheader(", ".join(ind_rows["club"].astype(str).unique()))
    k = st.columns(5)
    k[0].metric("Gymnastes", ind_rows["id_gymnaste"].nunique())
    k[1].metric("Équipes", team_rows["id_equipe"].nunique())
    k[2].metric("Compétitions", pd.concat([ind_rows["id_competition"], team_rows["id_competition"]]).nunique())
    k[3].metric("Prop. moyenne · individuel", fmt(ind_rows["prop_total"].mean(), 1, " %"), help=PROP_HELP)
    k[4].metric("Prop. moyenne · équipes", fmt(team_rows["prop_total"].mean(), 1, " %"), help=PROP_HELP)

    min_comp = st.number_input("Nombre minimum de compétitions pour apparaître dans les classements",
                               min_value=1, value=1, step=1)
    st.caption("Les classements du club comparent les proportions moyennes, ce qui permet de mélanger "
               "niveaux et catégories. " + PROP_HELP)

    tab_gym, tab_team, tab_level, tab_cat, tab_app = st.tabs(["Gymnastes", "Équipes", "Niveaux", "Catégories d'âge",
                                                              "Agrès"])

    with tab_gym:
        table = ranking_table(ind_rows, "gymnaste", min_comp, lambda g: {
            "Niveaux": g["niveau"].agg(lambda s: ", ".join(sorted(set(s.astype(str))))),
        })
        st.plotly_chart(ranking_bars(table, "gymnaste", title="Top 30 des gymnastes (proportion moyenne au total)"))
        show_ranking(table, "Gymnaste")

    with tab_team:
        table = ranking_table(team_rows, "equipe", min_comp, lambda g: {
            "Niveau": g["niveau"].first().astype(str),
            "Catégorie": g["categorie"].first().astype(str),
        })
        st.plotly_chart(ranking_bars(table, "equipe", color="Niveau", top=50,
                                     title="Classement des équipes (proportion moyenne au total)"))
        show_ranking(table, "Équipe")

    with tab_level:
        group_comparison(ind_rows, team_rows, "niveau", "Niveau")

    with tab_cat:
        group_comparison(ind_rows, team_rows, "categorie", "Catégorie")

    with tab_app:
        c1, c2 = st.columns(2)
        c1.plotly_chart(radar({"Individuel": apparatus_index(ind_rows), "Équipes": apparatus_index(team_rows)},
                              "Indice de classement moyen par agrès"))
        c1.caption(INDEX_HELP)
        heat = (ind_rows.groupby("niveau", observed=True)[[f"prop_{m}" for m in APPARATUS]].mean()
                .set_axis(list(APPARATUS.values()), axis=1))
        heat.index = heat.index.astype(str)
        text = [[fmt(v, 1, " %") for v in row] for row in heat.to_numpy()]
        c2.plotly_chart(heatmap(heat, text, "Proportion moyenne (individuel) par niveau et par agrès", "Prop. %",
                                "{:.1f} %", colorscale="RdYlGn_r", zmin=0, zmax=100))
        st.markdown("**Proportions (%) par agrès**")
        st.dataframe(pd.concat({
            "Individuel": stats_table(ind_rows, {f"prop_{m}": label for m, label in METRICS.items()}),
            "Équipes": stats_table(team_rows, {f"prop_{m}": label for m, label in METRICS.items()}),
        }))
