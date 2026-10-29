# Tableau de bord UFOLEP GYM

Application Streamlit pour explorer les résultats des compétitions UFOLEP de gymnastique artistique féminine : notes et classements par gymnaste, par équipe et par club.

Les données viennent de deux sources publiques :
- [Flipsalto](https://competition.flipsalto.fr), pour les saisons récentes ;
- les archives de [ufolepgym.com](https://ufolepgym.com/ufolepgym/plan_archives.htm), pour les saisons 2015-2016 à 2024-2025.

## Structure

```
1_data_extraction/
  create_empty_database.sql   schéma SQLite
  extract_flipsalto.py        API Flipsalto -> base
  extract_ufolepgym.py        archives ufolepgym.com -> base
  get_all_competitions.py     liste des compétitions des archives
  add_data.py                 insertions communes aux deux scripts
  clean_db.py                 harmonisation des noms et fusion des doublons
  Competitions_gym.db
2_data_viz/
  app.py                      filtres et onglets
  data.py                     chargement de la base
  views.py                    vues gymnaste, équipe et club
```

## Lancer l'application

```
pip install -r requirements.txt
cd 2_data_viz
streamlit run app.py
```

## Reconstruire la base

Depuis `1_data_extraction/` :

```
python -c "import sqlite3; sqlite3.connect('Competitions_gym.db').executescript(open('create_empty_database.sql').read())"
python extract_flipsalto.py
python extract_ufolepgym.py
python clean_db.py
```

Flipsalto doit être chargé en premier : `extract_ufolepgym.py` ignore les compétitions déjà présentes. Les pages des archives sont mises en cache dans `data/`, donc une relance ne les retélécharge pas.
