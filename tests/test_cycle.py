"""Scénarios de bout en bout sur les faux outils : chaque test raconte une situation réelle."""

from __future__ import annotations

from datetime import UTC, datetime

from faux import Banc, projet, tache, temps, utilisateur

from dolop.cycle import MARQUEUR_COMMENTAIRES, Commentaire


def banc_relie() -> tuple[Banc, dict[str, str]]:
    """Un utilisateur, un projet et une tâche déjà reliés des deux côtés."""
    b = Banc()
    b.dol["utilisateur"].ajouter("du1", **utilisateur("alice@exemple.fr"))
    b.op["utilisateur"].ajouter("ou1", **utilisateur("Alice@Exemple.fr", login="amartin"))
    b.dol["projet"].ajouter("dp1", **projet())
    b.dol["tache"].ajouter("dt1", **tache("dp1"))
    r = b.cycle()
    assert r.statut == "réussi", r.message
    ids = {
        "du": "du1",
        "ou": "ou1",
        "dp": "dp1",
        "op": b.dol["projet"].refs["dp1"] or "",
        "dt": "dt1",
        "ot": b.dol["tache"].refs["dt1"] or "",
    }
    assert ids["op"] and ids["ot"]
    b.oublier_ecritures()
    return b, ids


# ---------------------------------------------------------------------- créations


def test_projet_cree_dans_dolibarr_apparait_dans_openproject_sans_echo() -> None:
    b = Banc()
    b.dol["projet"].ajouter("dp1", **projet())
    assert b.cycle().statut == "réussi"
    op_id, champs = b.op["projet"].seul()
    assert champs["titre"] == "Site Acme"
    assert b.op["projet"].refs[op_id] == "dp1", "l'identifiant Dolibarr est embarqué côté OpenProject"
    assert b.dol["projet"].refs["dp1"] == op_id, "et l'identifiant OpenProject côté Dolibarr"
    b.oublier_ecritures()
    b.cycle()
    assert b.ecritures() == []


def test_projet_cree_dans_openproject_avec_client_tape_a_la_main() -> None:
    b = Banc()
    b.op["projet"].ajouter("op1", **projet("Refonte boutique", client="chateau elegance"))
    b.cycle()
    _, champs = b.dol["projet"].seul()
    assert champs["client"] == "Château Élégance", "rapproché sans casse ni accents"


def test_client_introuvable_projet_cree_sans_client_puis_rattrape() -> None:
    b = Banc(tiers=["Acme"])
    b.op["projet"].ajouter("op1", **projet("Salon annuel", client="Nouveau Client SAS"))
    b.cycle()
    dol_id, champs = b.dol["projet"].seul()
    assert champs.get("client") is None
    assert any("Nouveau Client SAS" in m for _, m in b.envoyees)
    b.tiers.append("Nouveau Client SAS")  # le tiers est créé dans Dolibarr
    b.cycle()
    assert b.dol["projet"].objets[dol_id]["client"] == "Nouveau Client SAS"


def test_utilisateurs_apparies_par_email_sans_doublon() -> None:
    b, ids = banc_relie()
    assert len(b.op["utilisateur"].objets) == 1
    assert len(b.dol["utilisateur"].objets) == 1
    assert b.etat.lien_par_paire("utilisateur", ids["du"], ids["ou"]) is not None


def test_sous_tache_et_parent_crees_dans_le_meme_cycle() -> None:
    b, ids = banc_relie()
    b.op["tache"].ajouter("ot-parent", **tache(ids["op"], "Lot 1"))
    b.op["tache"].ajouter("ot-enfant", **tache(ids["op"], "Lot 1.1", parent="ot-parent"))
    b.cycle()
    dol_parent = b.op["tache"].refs["ot-parent"]
    dol_enfant = b.op["tache"].refs["ot-enfant"]
    assert dol_parent and dol_enfant
    assert b.dol["tache"].objets[dol_enfant]["parent"] == dol_parent


def test_tache_clonee_dans_dolibarr_devient_une_nouvelle_tache() -> None:
    b, ids = banc_relie()
    # Un clonage Dolibarr recopie l'attribut « ID OpenProject » de l'original.
    b.dol["tache"].ajouter("dt-clone", ref_autre=ids["ot"], **tache(ids["dp"], "Maquette (copie)"))
    b.cycle()
    assert len(b.op["tache"].objets) == 2
    assert b.dol["tache"].refs["dt-clone"] != ids["ot"]


# ------------------------------------------------------------------ modifications


def test_modification_propagee_puis_aucun_echo() -> None:
    b, ids = banc_relie()
    b.op["tache"].changer(ids["ot"], avancement=50, titre="Maquette v2")
    b.cycle()
    assert b.dol["tache"].objets[ids["dt"]]["avancement"] == 50
    assert b.dol["tache"].objets[ids["dt"]]["titre"] == "Maquette v2"
    b.oublier_ecritures()
    b.cycle()
    assert b.ecritures() == []


def test_conversion_imparfaite_ne_revient_pas_en_echo() -> None:
    b, ids = banc_relie()
    # OpenProject réécrit le texte à sa façon (comme le ferait un aller-retour HTML ↔ Markdown).
    b.op["tache"].stockage = lambda c: {k: (f"{v}\n" if k == "description" else v) for k, v in c.items()}
    b.dol["tache"].changer(ids["dt"], description="**Important**")
    b.cycle()
    assert b.op["tache"].objets[ids["ot"]]["description"] == "**Important**\n"
    b.oublier_ecritures()
    b.cycle()
    b.cycle()
    assert b.ecritures() == []


def test_conflit_le_plus_recent_gagne_et_alerte() -> None:
    b, ids = banc_relie()
    b.dol["projet"].changer(ids["dp"], titre="Titre Dolibarr")
    b.horloge.avancer(1)
    b.op["projet"].changer(ids["op"], titre="Titre OpenProject")
    b.cycle()
    assert b.dol["projet"].objets[ids["dp"]]["titre"] == "Titre OpenProject"
    assert b.op["projet"].objets[ids["op"]]["titre"] == "Titre OpenProject"
    assert any("modifié des deux côtés" in m for _, m in b.envoyees)


def test_conflit_sans_horodatage_tache_openproject_gagne() -> None:
    b, ids = banc_relie()
    b.dol["tache"].changer(ids["dt"], titre="Dolibarr")
    b.op["tache"].changer(ids["ot"], titre="OpenProject")
    b.cycle()
    assert b.dol["tache"].objets[ids["dt"]]["titre"] == "OpenProject"


def test_meme_modification_des_deux_cotes_nest_pas_un_conflit() -> None:
    b, ids = banc_relie()
    b.dol["projet"].changer(ids["dp"], titre="Nouveau")
    b.op["projet"].changer(ids["op"], titre="  Nouveau ")
    b.cycle()
    assert not any("deux côtés" in m for _, m in b.envoyees)


# ------------------------------------------------------------------------- temps


def test_temps_saisi_dans_openproject_arrive_dans_dolibarr() -> None:
    b, ids = banc_relie()
    b.op["temps"].ajouter("oe1", **temps(ids["op"], ids["ot"], ids["ou"], duree=5400, note="Réunion"))
    b.cycle()
    dol_id, champs = b.dol["temps"].seul()
    assert champs == temps(ids["dp"], ids["dt"], ids["du"], duree=5400, note="Réunion")
    assert b.op["temps"].refs["oe1"] == dol_id


def test_temps_sans_tache_openproject() -> None:
    b, ids = banc_relie()
    b.op["temps"].ajouter("oe1", **temps(ids["op"], None, ids["ou"]))
    b.cycle()
    _, champs = b.dol["temps"].seul()
    assert champs["tache"] is None and champs["projet"] == ids["dp"]


def test_panne_apres_creation_dans_dolibarr_pas_de_doublon() -> None:
    b, ids = banc_relie()
    b.op["temps"].ajouter("oe1", **temps(ids["op"], ids["ot"], ids["ou"]))
    b.dol["temps"].panne_apres_creation = True
    r = b.cycle()
    assert r.bilan.erreurs, "l'interruption est signalée"
    assert len(b.dol["temps"].objets) == 1
    b.cycle()
    assert len(b.dol["temps"].objets) == 1, "le jumeau est retrouvé au lieu d'être recréé"
    assert len(b.etat.liens("temps")) == 1
    assert b.etat.en_cours() == []


def test_panne_apres_creation_dans_openproject_pas_de_doublon() -> None:
    b, ids = banc_relie()
    b.dol["tache"].ajouter("dt2", **tache(ids["dp"], "Recette"))
    b.op["tache"].panne_apres_creation = True
    b.cycle()
    b.cycle()
    assert len(b.op["tache"].objets) == 2
    assert b.etat.en_cours() == []


def test_suppression_de_temps_propagee() -> None:
    b, ids = banc_relie()
    b.op["temps"].ajouter("oe1", **temps(ids["op"], ids["ot"], ids["ou"]))
    b.cycle()
    del b.op["temps"].objets["oe1"]
    b.cycle()
    assert b.dol["temps"].objets == {}
    assert b.etat.liens("temps") == []


def test_temps_facture_modifie_dans_openproject_est_restaure() -> None:
    b, ids = banc_relie()
    b.dol["temps"].ajouter("de1", **temps(ids["dp"], ids["dt"], ids["du"], duree=7200))
    b.cycle()
    oe = next(iter(b.op["temps"].objets))
    b.dol["temps"].verrous["de1"] = "temps déjà facturé"
    b.op["temps"].changer(oe, duree=600)
    b.cycle()
    assert b.dol["temps"].objets["de1"]["duree"] == 7200
    assert b.op["temps"].objets[oe]["duree"] == 7200, "OpenProject est remis d'aplomb"
    assert any("facturé" in m for _, m in b.envoyees)


def test_temps_facture_supprime_dans_openproject_est_recree() -> None:
    b, ids = banc_relie()
    b.dol["temps"].ajouter("de1", **temps(ids["dp"], ids["dt"], ids["du"]))
    b.cycle()
    oe = next(iter(b.op["temps"].objets))
    b.dol["temps"].verrous["de1"] = "temps déjà facturé"
    del b.op["temps"].objets[oe]
    b.cycle()
    assert "de1" in b.dol["temps"].objets
    assert len(b.op["temps"].objets) == 1
    b.oublier_ecritures()
    b.cycle()
    assert b.ecritures() == []


# ------------------------------------------------------------------- suppressions


def test_tache_supprimee_qui_porte_du_temps_de_lautre_cote_nest_pas_supprimee() -> None:
    b, ids = banc_relie()
    b.op["tache"].refus[ids["ot"]] = "2 temps y sont saisis"
    del b.dol["tache"].objets[ids["dt"]]
    b.cycle()
    assert ids["ot"] in b.op["tache"].objets
    assert any("2 temps" in m for _, m in b.envoyees)
    assert b.etat.liens("tache")[0].rompu


def test_projet_supprime_dans_dolibarr_est_archive_et_ses_taches_restent() -> None:
    b, ids = banc_relie()
    del b.dol["projet"].objets[ids["dp"]]
    del b.dol["tache"].objets[ids["dt"]]  # Dolibarr supprime les tâches avec le projet
    r = b.cycle()
    assert r.statut == "réussi", r.message
    assert b.op["projet"].objets[ids["op"]]["actif"] is False
    assert ids["ot"] in b.op["tache"].objets, "rien n'est effacé dans OpenProject"


def test_projet_clos_jamais_synchronise_reste_dans_dolibarr_jusqua_reouverture() -> None:
    b = Banc()
    b.dol["projet"].ajouter("dp1", **projet("Site Acme", actif=False))
    b.dol["tache"].ajouter("dt1", **tache("dp1"))
    b.cycle()
    assert b.op["projet"].objets == {} and b.op["tache"].objets == {}
    b.dol["projet"].changer("dp1", actif=True)
    b.cycle()
    assert len(b.op["projet"].objets) == 1
    b.cycle()
    assert len(b.op["tache"].objets) == 1, "rouvert, il arrive avec ses tâches"


def test_projet_clos_gele_ses_taches() -> None:
    b, ids = banc_relie()
    b.dol["projet"].changer(ids["dp"], actif=False)
    b.cycle()
    assert b.op["projet"].objets[ids["op"]]["actif"] is False
    b.dol["tache"].ajouter("dt9", **tache(ids["dp"], "Tâche tardive"))
    b.cycle()
    assert len(b.op["tache"].objets) == 1, "rien n'est créé dans un projet archivé"


def test_objet_absent_de_la_liste_mais_lisible_nest_pas_supprime() -> None:
    b, ids = banc_relie()
    b.op["tache"].caches.add(ids["ot"])
    b.cycle()
    assert ids["dt"] in b.dol["tache"].objets


def test_acces_refuse_nest_jamais_pris_pour_une_suppression() -> None:
    b, ids = banc_relie()
    b.op["tache"].caches.add(ids["ot"])
    b.op["tache"].interdits.add(ids["ot"])
    r = b.cycle()
    assert r.statut == "échec"
    assert ids["dt"] in b.dol["tache"].objets


def test_disjoncteur_bloque_une_vague_de_suppressions() -> None:
    b, ids = banc_relie()
    for i in range(6):
        b.op["temps"].ajouter(f"oe{i}", **temps(ids["op"], ids["ot"], ids["ou"], duree=600 * (i + 1)))
    b.cycle()
    assert len(b.dol["temps"].objets) == 6
    b.op["temps"].objets.clear()
    b.oublier_ecritures()
    r = b.cycle()
    assert r.statut == "bloqué"
    assert b.ecritures() == [], "rien n'est écrit quand le disjoncteur saute"
    assert len(b.dol["temps"].objets) == 6
    assert any("suppressions" in m for _, m in b.envoyees)
    r = b.cycle(confirmer_suppressions=True)
    assert r.statut == "réussi"
    assert b.dol["temps"].objets == {}
    assert any("rétabli" in t for t, _ in b.envoyees)


def test_liste_vide_alors_que_des_liens_existent_bloque() -> None:
    b, _ = banc_relie()
    b.dol["projet"].ajouter("dp2", **projet("Site vitrine"))
    b.cycle()
    b.oublier_ecritures()
    # Le compte technique perd ses droits : OpenProject ne montre plus rien et répond 404.
    b.op["tache"].objets.clear()
    b.op["projet"].objets.clear()
    b.oublier_ecritures()
    r = b.cycle()
    assert r.statut == "bloqué"
    assert b.ecritures() == []


# ---------------------------------------------------------------------- membres


def test_membre_ajoute_dans_openproject_devient_contact_dolibarr() -> None:
    b, ids = banc_relie()
    b.op["membre"].ajouter("om1", projet=ids["op"], utilisateur=ids["ou"], role="contributeur")
    b.cycle()
    _, champs = b.dol["membre"].seul()
    assert champs == {"projet": ids["dp"], "utilisateur": ids["du"], "role": "contributeur"}


def test_membre_existant_des_deux_cotes_est_relie_pas_duplique() -> None:
    b, ids = banc_relie()
    b.op["membre"].ajouter("om1", projet=ids["op"], utilisateur=ids["ou"], role="chef")
    b.dol["membre"].ajouter("dm1", projet=ids["dp"], utilisateur=ids["du"], role="chef")
    b.cycle()
    assert len(b.op["membre"].objets) == 1 and len(b.dol["membre"].objets) == 1
    assert b.etat.lien_par_paire("membre", "dm1", "om1") is not None


# ----------------------------------------------------------------- commentaires


def test_commentaires_copies_sans_effacer_la_note_existante() -> None:
    b, ids = banc_relie()
    b.notes.notes[ids["dt"]] = "<p>Code d'accès du client : voir coffre</p>"
    b.commentaires.par_lot[ids["ot"]] = [
        Commentaire(datetime(2026, 9, 24, 12, 5, tzinfo=UTC), "Alice Martin", "Maquette **validée**"),
    ]
    b.op["tache"].changer(ids["ot"])  # un commentaire change la date de mise à jour du lot
    b.cycle()
    note = b.notes.notes[ids["dt"]]
    assert note.startswith("<p>Code d'accès du client")
    assert MARQUEUR_COMMENTAIRES in note and "<strong>validée</strong>" in note
    assert "24/09/2026 14:05" in note, "heure de Paris"
    appels = b.commentaires.appels
    b.cycle()
    assert b.commentaires.appels == appels, "lot inchangé : on ne relit pas ses commentaires"
    b.commentaires.par_lot[ids["ot"]].append(
        Commentaire(datetime(2026, 9, 25, 9, 0, tzinfo=UTC), "Alice Martin", "Envoyée au client")
    )
    b.op["tache"].changer(ids["ot"])
    b.cycle()
    note = b.notes.notes[ids["dt"]]
    assert note.count(MARQUEUR_COMMENTAIRES) == 1 and "Envoyée au client" in note
    assert note.startswith("<p>Code d'accès du client")


# ----------------------------------------------------------- simulation et alertes


def test_simulation_nectrit_rien() -> None:
    b, ids = banc_relie()
    b.notes.notes[ids["dt"]] = "<p>Note</p>"
    b.commentaires.par_lot[ids["ot"]] = [Commentaire(datetime(2026, 9, 24, 12, tzinfo=UTC), "Alice", "Validé")]
    b.op["tache"].changer(ids["ot"], titre="Maquette v3")
    b.dol["projet"].ajouter("dp2", **projet("Site vitrine"))
    b.dol["tache"].ajouter("dt2", **tache("dp2"))
    liens_avant, cycles_avant = b.etat.liens(), b.etat.derniers_cycles()
    r = b.cycle(mode="simuler")
    assert r.statut == "réussi"
    assert b.ecritures() == []
    assert b.notes.notes[ids["dt"]] == "<p>Note</p>", "les commentaires ne sont pas copiés pour de vrai"
    assert b.etat.liens() == liens_avant and len(b.etat.derniers_cycles()) == len(cycles_avant)
    assert any("commentaires" in e for e in r.ecritures)
    assert any("modifier" in e for e in r.ecritures)
    assert any("créer" in e for e in r.ecritures)
    assert sum("tache" in e and "créer" in e for e in r.ecritures) == 1, "la tâche suit son projet simulé"


def test_alerte_non_repetee_avant_24_heures() -> None:
    b = Banc(tiers=[])
    b.op["projet"].ajouter("op1", **projet(client="Inconnu"))
    b.cycle()
    b.cycle()
    b.cycle()
    assert sum("Inconnu" in m for _, m in b.envoyees) == 1


def test_trois_echecs_de_suite_alertent_puis_retablissement() -> None:
    b, ids = banc_relie()
    b.op["tache"].caches.add(ids["ot"])
    b.op["tache"].interdits.add(ids["ot"])
    for _ in range(3):
        assert b.cycle(mode="service").statut == "échec"
    assert any("3 cycles en échec" in m for _, m in b.envoyees)
    b.op["tache"].caches.clear()
    b.op["tache"].interdits.clear()
    assert b.cycle(mode="service").statut == "réussi"
    assert any("rétabli" in t for t, _ in b.envoyees)
