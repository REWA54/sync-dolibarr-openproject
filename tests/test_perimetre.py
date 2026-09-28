"""Premier contact avec des outils déjà remplis : ne recopier que ce qui est choisi, ne rien doubler."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import httpx
from faux import Banc, projet, tache, temps, utilisateur
from test_adaptateurs import CONFIG, Serveur, _page, dolibarr, openproject
from test_cycle import banc_relie

from dolop import appariement
from dolop.cycle import lire_les_deux

CHOISI = {"perimetre_choisi": True}


# -------------------------------------------------------------- disjoncteur de créations


def test_outil_rempli_branche_par_erreur_rien_nest_recopie_sans_confirmation() -> None:
    b = Banc()
    for i in range(60):
        b.dol["projet"].ajouter(f"dp{i}", **projet(f"Projet {i}"))
    r = b.cycle()
    assert r.statut == "bloqué" and "60 créations prévues" in r.message
    assert b.ecritures() == [] and b.op["projet"].objets == {}
    assert any("créations" in m for _, m in b.envoyees)

    simulation = b.cycle(mode="simuler")
    assert simulation.statut == "bloqué", "la simulation dit que le cycle serait bloqué…"
    assert sum(e.action == "créer" for e in simulation.plan) == 60, "… et montre quand même tout le plan"

    assert b.cycle(confirmer_creations=True).statut == "réussi"
    assert len(b.op["projet"].objets) == 60
    assert b.cycle().statut == "réussi", "une fois recopiés, les cycles suivants passent sans confirmation"


# ------------------------------------------------------------------ périmètre choisi


def test_seuls_les_projets_coches_et_leurs_objets_sont_recopies() -> None:
    b = Banc()
    b.dol["projet"].ajouter("dp1", **projet("Site Acme"))
    b.dol["tache"].ajouter("dt1", **tache("dp1"))
    b.dol["projet"].ajouter("dp2", **projet("Interne — comptabilité"))
    b.dol["tache"].ajouter("dt2", **tache("dp2", "Clôture"))
    b.dol["projet"].hors_perimetre.add("dp2")
    b.op["projet"].ajouter("op9", **projet("Veille technique"))
    b.op["projet"].hors_perimetre.add("op9")
    assert b.cycle(**CHOISI).statut == "réussi"
    assert [o["titre"] for o in b.op["projet"].objets.values()] == ["Veille technique", "Site Acme"]
    assert [o["titre"] for o in b.op["tache"].objets.values()] == ["Maquette"]
    assert [o["titre"] for o in b.dol["projet"].objets.values()] == ["Site Acme", "Interne — comptabilité"]


def test_projet_relie_decoche_des_deux_cotes_se_fige_sans_rien_supprimer() -> None:
    b, ids = banc_relie()
    b.dol["projet"].hors_perimetre.add(ids["dp"])
    b.op["projet"].hors_perimetre.add(ids["op"])
    b.dol["projet"].changer(ids["dp"], titre="Nouveau titre")
    b.op["tache"].changer(ids["ot"], titre="Maquette v2")
    b.op["tache"].ajouter("ot9", **tache(ids["op"], "Nouvelle tâche"))
    assert b.cycle(**CHOISI).statut == "réussi"
    assert b.ecritures() == [], "ni modification, ni création, ni suppression"
    b.op["projet"].hors_perimetre.clear()  # recoché dans OpenProject : la synchro reprend
    b.cycle(**CHOISI)
    assert b.op["projet"].objets[ids["op"]]["titre"] == "Nouveau titre"
    assert b.dol["tache"].objets[ids["dt"]]["titre"] == "Maquette v2"


def test_seuls_les_utilisateurs_concernes_par_un_projet_choisi_sont_invites() -> None:
    b = Banc()
    b.dol["utilisateur"].ajouter("du1", **utilisateur("alice@exemple.fr"))
    b.dol["utilisateur"].ajouter("du2", **utilisateur("bob@exemple.fr", login="bob"))
    b.dol["utilisateur"].ajouter("du3", **utilisateur("chloe@exemple.fr", login="chloe"))
    b.dol["projet"].ajouter("dp1", **projet())
    b.dol["membre"].ajouter("dp1:du1", projet="dp1", utilisateur="du1", role="chef")
    b.dol["projet"].ajouter("dp2", **projet("Interne"))
    b.dol["projet"].hors_perimetre.add("dp2")
    b.dol["membre"].ajouter("dp2:du3", projet="dp2", utilisateur="du3", role="chef")
    b.cycle(**CHOISI)
    assert [o["email"] for o in b.op["utilisateur"].objets.values()] == ["alice@exemple.fr"]


def test_temps_anterieurs_a_la_date_de_depart_ni_lus_ni_supprimes() -> None:
    b, ids = banc_relie()
    b.op["temps"].ajouter("oe1", **temps(ids["op"], ids["ot"], ids["ou"], date="2025-06-30"))
    b.cycle()
    ancien = next(iter(b.dol["temps"].objets))
    b.oublier_ecritures()
    # TEMPS_DEPUIS : les listes ne donnent plus ces temps, et les chercher un par un est interdit.
    for cote, identifiant in (("op", "oe1"), ("dol", ancien)):
        adaptateur = (b.op if cote == "op" else b.dol)["temps"]
        adaptateur.caches.add(identifiant)
        adaptateur.interdits.add(identifiant)
    assert b.cycle(temps_depuis=date(2026, 1, 1)).statut == "réussi"
    assert b.ecritures() == []
    assert ancien in b.dol["temps"].objets


# ----------------------------------------------------- seuil de suppressions proportionnel


def test_quelques_suppressions_sur_beaucoup_dobjets_ne_bloquent_pas() -> None:
    b, ids = banc_relie()
    for i in range(800):
        b.op["temps"].ajouter(f"oe{i}", **temps(ids["op"], ids["ot"], ids["ou"], duree=60 * (i + 1)))
    assert b.cycle(confirmer_creations=True).statut == "réussi"
    for i in range(7):
        del b.op["temps"].objets[f"oe{i}"]
    r = b.cycle()
    assert r.statut == "réussi", r.message
    assert len(b.dol["temps"].objets) == 793


# ------------------------------------------------------------------------ appariement


def _outils_deja_remplis() -> Banc:
    b = Banc()
    for cote, prefixe in ((b.dol, "d"), (b.op, "o")):
        cote["utilisateur"].ajouter(f"{prefixe}u1", **utilisateur("alice@exemple.fr"))
        cote["projet"].ajouter(f"{prefixe}p1", **projet("Site Acme"))
        cote["projet"].ajouter(f"{prefixe}p2", **projet("Site vitrine"))
        cote["tache"].ajouter(f"{prefixe}t1", **tache(f"{prefixe}p1", "Maquette"))
        cote["temps"].ajouter(f"{prefixe}e1", **temps(f"{prefixe}p1", f"{prefixe}t1", f"{prefixe}u1"))
    b.op["projet"].ajouter("op3", **projet("Site vitrine"))  # deux projets du même nom : on ne devine pas
    return b


def test_apparier_propose_ce_qui_existe_deja_puis_plus_aucun_doublon(tmp_path: Path) -> None:
    b = _outils_deja_remplis()
    ctx = b.contexte()
    lus, geles = lire_les_deux(ctx, b.etat)
    propositions, doutes = appariement.proposer(lus, b.etat.liens(), geles, ctx.convertisseurs)
    assert {(p.type, p.dol_id, p.op_id) for p in propositions} == {
        ("utilisateur", "du1", "ou1"),
        ("projet", "dp1", "op1"),
        ("tache", "dt1", "ot1"),
        ("temps", "de1", "oe1"),
    }
    assert any("Site vitrine" in d for d in doutes)

    fichier = tmp_path / "appariement.csv"
    appariement.ecrire(propositions, fichier)
    relues = appariement.lire(fichier)
    assert appariement.appliquer(relues, ctx.adaptateurs, b.etat, oui=False, echo=lambda _m: None) == 4
    assert b.etat.liens() == [], "sans --oui : rien n'est relié"
    assert appariement.appliquer(relues, ctx.adaptateurs, b.etat, oui=True, echo=lambda _m: None) == 4
    assert b.op["projet"].refs["op1"] == "dp1" and b.dol["tache"].refs["dt1"] == "ot1"

    b.dol["projet"].hors_perimetre.add("dp2")  # le doute tranché à la main : on ne recopie pas celui-là
    b.op["projet"].hors_perimetre.update({"op2", "op3"})
    assert b.cycle().statut == "réussi"
    assert not any("créer" in e for e in b.ecritures()), "rien n'est recopié en double"


def test_apparier_ignore_une_ligne_deja_reliee_ou_en_double(tmp_path: Path) -> None:
    b, ids = banc_relie()
    fichier = tmp_path / "a.csv"
    fichier.write_text(
        "type;dolibarr;libelle_dolibarr;openproject;libelle_openproject\n"
        f"projet;{ids['dp']};x;{ids['op']};x\n"
        "projet;dp7;x;op7;x\n"
        "projet;dp7;x;op8;x\n",
        encoding="utf-8",
    )
    b.dol["projet"].ajouter("dp7", **projet("Autre"))
    b.op["projet"].ajouter("op7", **projet("Autre"))
    b.op["projet"].ajouter("op8", **projet("Autre bis"))
    messages: list[str] = []
    adaptateurs = b.contexte().adaptateurs
    n = appariement.appliquer(appariement.lire(fichier), adaptateurs, b.etat, oui=True, echo=messages.append)
    assert n == 1
    assert any("déjà relié" in m for m in messages) and any("plus haut" in m for m in messages)


# ---------------------------------------------------------------------- adaptateurs


def test_dolibarr_case_a_synchroniser_lue_et_posee_sur_les_projets_recopies() -> None:
    config = replace(CONFIG, perimetre="choisi")
    s = Serveur()
    coche = {"id": "1", "array_options": {"options_synchro_openproject": "1"}}
    s.route("GET", "/projects", (200, [coche, {"id": "2"}]))
    s.route("POST", "/projects", (200, 3))
    s.route("POST", "/projects/3/validate", (200, {}))
    d_, a = dolibarr(s)
    d_.config = config
    lus = a["projet"].lister()
    assert lus["1"].perimetre and not lus["2"].perimetre
    a["projet"].creer({"titre": "Recopié"}, "44")
    (_, corps), *_ = s.ecrits("POST")
    assert corps["array_options"] == {"options_openproject_id": "44", "options_synchro_openproject": 1}


def test_openproject_case_a_synchroniser_et_filtres() -> None:
    config = replace(CONFIG, perimetre="choisi", op_types=("Tâche",), temps_depuis=date(2026, 1, 1))
    s = Serveur()
    schema = {
        "customField1": {"name": "ID Dolibarr", "type": "String"},
        "customField2": {"name": "Synchroniser avec Dolibarr", "type": "Boolean"},
    }
    s.route("GET", "/api/v3/projects/schema", (200, schema))
    s.route("GET", "/api/v3/projects", (200, _page([{"id": 5, "active": True, "customField2": True}, {"id": 6}])))
    s.route("GET", "/api/v3/types", (200, _page([{"id": 1, "name": "Task"}, {"id": 7, "name": "Tache"}])))
    s.route("GET", "/api/v3/work_packages", (200, _page([])))
    s.route("GET", "/api/v3/users", (200, _page([])))
    s.route("GET", "/api/v3/time_entries", (200, _page([])))
    s.route("POST", "/api/v3/projects", (201, {"id": 8}))
    o, a = openproject(s)
    o.config = config
    lus = a["projet"].lister()
    assert lus["5"].perimetre and not lus["6"].perimetre
    a["tache"].lister()
    a["temps"].lister()
    requetes = " ".join(httpx.URL(c).query.decode() for _, c, _ in s.appels if c.startswith("/api/v3/"))
    assert "%22type%22" in requetes and "%227%22" in requetes, "seuls les lots du type choisi"
    assert "spentOn" in requetes and "2026-01-01" in requetes, "et les temps depuis la date de départ"
    a["projet"].creer({"titre": "Recopié"}, "3")
    corps: Any = s.ecrits("POST")[0][1]
    assert corps["customField2"] is True and corps["customField1"] == "3"
