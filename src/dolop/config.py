"""Configuration par variables d'environnement (celles du conteneur)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo


class ErreurConfig(Exception):
    pass


@dataclass(frozen=True)
class Config:
    dolibarr_url: str
    dolibarr_cle: str
    openproject_url: str
    openproject_cle: str
    # En-tête Host à envoyer quand on passe par l'adresse interne (http://openproject-proxy).
    openproject_hote: str | None = None
    dolibarr_attribut: str = "openproject_id"
    op_champ_ref: str = "ID Dolibarr"
    op_champ_client: str = "Client"
    op_role_chef: str = "Project admin"
    op_role_contributeur: str = "Member"
    op_type_tache: str = "Task"
    op_activite: str | None = None
    exclure_logins: frozenset[str] = frozenset({"admin"})
    tache_hors_tache: str = "Temps hors tâche"
    intervalle: int = 120
    seuil_suppressions: int = 5
    seuil_pourcent: int = 20
    webhook: str | None = None
    base: Path = Path("/data/etat.sqlite")
    fuseau_nom: str = "Europe/Paris"

    @property
    def fuseau(self) -> ZoneInfo:
        return ZoneInfo(self.fuseau_nom)

    @classmethod
    def depuis_env(cls, env: Mapping[str, str] | None = None) -> Config:
        env = os.environ if env is None else env
        obligatoires = ["DOLIBARR_URL", "DOLIBARR_API_KEY", "OPENPROJECT_URL", "OPENPROJECT_API_KEY"]
        manquantes = [v for v in obligatoires if not env.get(v)]
        if manquantes:
            raise ErreurConfig(f"variables d'environnement manquantes : {', '.join(manquantes)}")

        def texte(nom: str, defaut: str) -> str:
            return env.get(nom) or defaut

        def optionnel(nom: str) -> str | None:
            return env.get(nom) or None

        def entier(nom: str, defaut: int) -> int:
            valeur = env.get(nom)
            if not valeur:
                return defaut
            try:
                return int(valeur)
            except ValueError as e:
                raise ErreurConfig(f"{nom} doit être un nombre entier (reçu {valeur!r})") from e

        exclus = env.get("EXCLURE_LOGINS", "admin")
        return cls(
            dolibarr_url=env["DOLIBARR_URL"],
            dolibarr_cle=env["DOLIBARR_API_KEY"],
            openproject_url=env["OPENPROJECT_URL"],
            openproject_cle=env["OPENPROJECT_API_KEY"],
            openproject_hote=optionnel("OPENPROJECT_HOST"),
            dolibarr_attribut=texte("DOLIBARR_ATTRIBUT", "openproject_id"),
            op_champ_ref=texte("OPENPROJECT_CHAMP_REF", "ID Dolibarr"),
            op_champ_client=texte("OPENPROJECT_CHAMP_CLIENT", "Client"),
            op_role_chef=texte("OPENPROJECT_ROLE_CHEF", "Project admin"),
            op_role_contributeur=texte("OPENPROJECT_ROLE_CONTRIBUTEUR", "Member"),
            op_type_tache=texte("OPENPROJECT_TYPE", "Task"),
            op_activite=optionnel("OPENPROJECT_ACTIVITE"),
            exclure_logins=frozenset(x.strip().casefold() for x in exclus.split(",") if x.strip()),
            tache_hors_tache=texte("TACHE_HORS_TACHE", "Temps hors tâche"),
            intervalle=entier("INTERVALLE_SECONDES", 120),
            seuil_suppressions=entier("SEUIL_SUPPRESSIONS", 5),
            seuil_pourcent=entier("SEUIL_POURCENT", 20),
            webhook=optionnel("ALERTE_WEBHOOK_URL"),
            base=Path(texte("BASE_ETAT", "/data/etat.sqlite")),
            fuseau_nom=texte("FUSEAU", "Europe/Paris"),
        )
