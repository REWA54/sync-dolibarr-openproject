"""Traduction des valeurs d'un côté vers l'autre.

Les références (projet d'une tâche, utilisateur d'un temps…) passent par l'index des liens,
tenu à jour au fil du cycle : une tâche créée à l'instant est aussitôt traduisible.
Les autres valeurs sont déjà canoniques, sauf celles qui ont un convertisseur dédié
(le client : texte libre côté OpenProject, tiers existant côté Dolibarr).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from .modele import COTES, Champ, Cote, Intraduisible, Lien, Regles, autre

# (valeur, de) → valeur exprimée de l'autre côté ; lève Intraduisible.
Convertisseur = Callable[[Any, Cote], Any]


class IndexLiens:
    def __init__(self) -> None:
        self._vers: dict[tuple[str, Cote], dict[str, str]] = {}

    def charger(self, liens: Iterable[Lien]) -> None:
        for lien in liens:
            if not lien.rompu:
                self.ajouter(lien.type, lien.dol_id, lien.op_id)

    def ajouter(self, type_: str, dol_id: str, op_id: str) -> None:
        self._vers.setdefault((type_, "dol"), {})[dol_id] = op_id
        self._vers.setdefault((type_, "op"), {})[op_id] = dol_id

    def retirer(self, type_: str, dol_id: str, op_id: str) -> None:
        self._vers.get((type_, "dol"), {}).pop(dol_id, None)
        self._vers.get((type_, "op"), {}).pop(op_id, None)

    def remplacer(self, type_: str, cote: Cote, ancien: str, nouveau: str) -> None:
        """Un objet a été recréé d'un côté sous un nouvel identifiant."""
        vis_a_vis = self._vers.get((type_, cote), {}).pop(ancien, None)
        if vis_a_vis is None:
            return
        self._vers.setdefault((type_, cote), {})[nouveau] = vis_a_vis
        self._vers.setdefault((type_, autre(cote)), {})[vis_a_vis] = nouveau

    def vers(self, type_: str, identifiant: str, de: Cote) -> str | None:
        return self._vers.get((type_, de), {}).get(identifiant)


class Traducteur:
    def __init__(self, index: IndexLiens, convertisseurs: Mapping[tuple[str, str], Convertisseur] | None = None):
        self.index = index
        self.convertisseurs = dict(convertisseurs or {})

    def valeur(self, regles: Regles, champ: Champ, valeur: Any, de: Cote) -> Any:
        if champ.ref is not None:
            if valeur is None:
                return None
            cible = self.index.vers(champ.ref, str(valeur), de)
            if cible is None:
                raise Intraduisible(f"{champ.nom} : {champ.ref} {valeur} pas encore relié", alerter=False)
            return cible
        conv = self.convertisseurs.get((regles.type, champ.nom))
        if conv is not None:
            return conv(valeur, de)
        return valeur

    def champs(self, regles: Regles, valeurs: Mapping[str, Any], de: Cote) -> dict[str, Any]:
        """Tout ou rien : lève Intraduisible au premier champ impossible (créations)."""
        return {nom: self.valeur(regles, regles.champ(nom), v, de) for nom, v in valeurs.items()}

    def partiel(
        self, regles: Regles, valeurs: Mapping[str, Any], de: Cote
    ) -> tuple[dict[str, Any], dict[str, Intraduisible]]:
        """Champ par champ (mises à jour) : renvoie ce qui passe et ce qui est différé."""
        ok: dict[str, Any] = {}
        differes: dict[str, Intraduisible] = {}
        for nom, v in valeurs.items():
            try:
                ok[nom] = self.valeur(regles, regles.champ(nom), v, de)
            except Intraduisible as e:
                differes[nom] = e
        return ok, differes

    def egaux(self, regles: Regles, champ: Champ, valeur_dol: Any, valeur_op: Any) -> bool:
        """Les deux valeurs disent-elles la même chose ? (après traduction et normalisation)"""
        try:
            traduite = self.valeur(regles, champ, valeur_dol, "dol")
        except Intraduisible:
            return False
        norm = champ.normaliser or (lambda v: v)
        return bool(norm(traduite) == norm(valeur_op))


__all__ = ["COTES", "Convertisseur", "IndexLiens", "Traducteur"]
