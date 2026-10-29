CREATE TABLE "CATEGORIE_AGE" (
	"id"	INTEGER NOT NULL UNIQUE,
	"age_min"	TEXT NOT NULL,
	"age_max"	TEXT NOT NULL,
	PRIMARY KEY("id")
);

CREATE TABLE "CLUB" (
	"id"	INTEGER NOT NULL UNIQUE,
	"nom"	TEXT NOT NULL UNIQUE,
	"ville"	TEXT NOT NULL,
	"departement"	TEXT NOT NULL,
	PRIMARY KEY("id")
);

CREATE TABLE "COMPETITION" (
	"id"	INTEGER NOT NULL UNIQUE,
	"mois"	INTEGER NOT NULL,
	"annee"	INTEGER NOT NULL,
	"ville"	TEXT NOT NULL,
	"id_phase"	INTEGER NOT NULL,
	"url"	TEXT,
	PRIMARY KEY("id"),
	CONSTRAINT "FK_phase" FOREIGN KEY("id_phase") REFERENCES "PHASE"("id")
);

CREATE TABLE "COMPOSITION_EQUIPE" (
	"id"	INTEGER NOT NULL UNIQUE,
	"id_resultat_equipe"	INTEGER NOT NULL,
	"id_gymnaste"	INTEGER NOT NULL,
	PRIMARY KEY("id"),
	CONSTRAINT "UQ_compo" UNIQUE("id_resultat_equipe","id_gymnaste"),
	CONSTRAINT "FK_compo_gymnaste" FOREIGN KEY("id_gymnaste") REFERENCES "GYMNASTE"("id"),
	CONSTRAINT "FK_res_equipe" FOREIGN KEY("id_resultat_equipe") REFERENCES "RESULTAT_EQUIPE"("id")
);

CREATE TABLE "EQUIPE" (
	"id"	INTEGER NOT NULL UNIQUE,
	"id_club"	INTEGER NOT NULL,
	"numero_equipe"	INTEGER NOT NULL,
	"niveau"	INTEGER NOT NULL,
	"id_categorie_age"	INTEGER NOT NULL,
	"saison"	TEXT NOT NULL,
	PRIMARY KEY("id"),
	CONSTRAINT "UQ_equipe" UNIQUE("id_club","numero_equipe","niveau","id_categorie_age","saison"),
	CONSTRAINT "FK_age" FOREIGN KEY("id_categorie_age") REFERENCES "CATEGORIE_AGE"("id"),
	CONSTRAINT "FK_club" FOREIGN KEY("id_club") REFERENCES "CLUB"("id")
);

CREATE TABLE "GYMNASTE" (
	"id"	INTEGER NOT NULL UNIQUE,
	"nom"	TEXT NOT NULL,
	"prenom"	TEXT NOT NULL,
	"annee_naissance"	INTEGER NOT NULL,
	PRIMARY KEY("id"),
	CONSTRAINT "UQ_gymnaste" UNIQUE("nom","prenom","annee_naissance")
);

CREATE TABLE "PALMARES" (
	"id"	INTEGER NOT NULL UNIQUE,
	"id_competition"	INTEGER NOT NULL,
	"niveau"	INTEGER NOT NULL,
	"id_categorie_age"	INTEGER NOT NULL,
	"poule"	TEXT NOT NULL,
	PRIMARY KEY("id"),
	CONSTRAINT "UQ_palmares" UNIQUE("id_competition","niveau","id_categorie_age","poule"),
	CONSTRAINT "FK_categorie_age" FOREIGN KEY("id_categorie_age") REFERENCES "CATEGORIE_AGE"("id"),
	CONSTRAINT "FK_competition" FOREIGN KEY("id_competition") REFERENCES "COMPETITION"("id")
);

CREATE TABLE "PHASE" (
	"id"	INTEGER NOT NULL UNIQUE,
	"nom"	TEXT NOT NULL,
	PRIMARY KEY("id")
);

CREATE TABLE "RESULTAT_EQUIPE" (
	"id"	INTEGER NOT NULL UNIQUE,
	"id_equipe"	INTEGER NOT NULL,
	"classement"	INTEGER NOT NULL,
	"quota"	INTEGER,
	"total"	REAL NOT NULL,
	"note_saut"	REAL NOT NULL,
	"note_barres"	REAL NOT NULL,
	"note_poutre"	REAL NOT NULL,
	"note_sol"	REAL NOT NULL,
	"id_palmares"	INTEGER NOT NULL,
	PRIMARY KEY("id"),
	CONSTRAINT "UQ_res_equipe" UNIQUE("id_equipe","id_palmares"),
	CONSTRAINT "FK_equipe" FOREIGN KEY("id_equipe") REFERENCES "EQUIPE"("id"),
	CONSTRAINT "FK_palmares" FOREIGN KEY("id_palmares") REFERENCES "PALMARES"("id")
);

CREATE TABLE "RESULTAT_INDIVIDUEL" (
	"id"	INTEGER NOT NULL UNIQUE,
	"id_gymnaste"	INTEGER NOT NULL,
	"id_palmares"	INTEGER NOT NULL,
	"total"	REAL NOT NULL,
	"note_saut"	REAL NOT NULL,
	"note_barres"	REAL NOT NULL,
	"note_poutre"	REAL NOT NULL,
	"note_sol"	REAL NOT NULL,
	"classement"	INTEGER NOT NULL,
	"id_club"	INTEGER NOT NULL,
	PRIMARY KEY("id"),
	CONSTRAINT "UQ_res_indiv" UNIQUE("id_gymnaste","id_palmares"),
	CONSTRAINT "FK_club" FOREIGN KEY("id_club") REFERENCES "CLUB"("id"),
	CONSTRAINT "FK_gymnaste" FOREIGN KEY("id_gymnaste") REFERENCES "GYMNASTE"("id"),
	CONSTRAINT "FK_plamares" FOREIGN KEY("id_palmares") REFERENCES "PALMARES"("id")
);
