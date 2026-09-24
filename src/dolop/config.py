"""Configuration par variables d'environnement (celles du conteneur).

Chaque secret (jetons, webhook) peut aussi être lu dans un fichier : ``DOLIBARR_API_KEY_FILE=/run/secrets/…``
à la place de ``DOLIBARR_API_KEY``. C'est la forme attendue par les secrets Docker et Kubernetes.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ErreurConfig(Exception):
    pass


@dataclass(frozen=True)
class Config:
    dolibarr_url: str
    # repr=False : un secret ne doit jamais sortir dans un journal ou une trace d'erreur.
    dolibarr_cle: str = field(repr=False)
    openproject_url: str
    openproject_cle: str = field(repr=False)
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
    webhook: str | None = field(default=None, repr=False)
    base: Path = Path("/data/etat.sqlite")
    fuseau_nom: str = "Europe/Paris"
    # Sauvegardes quotidiennes de la base d'état gardées à côté d'elle (0 : aucune).
    sauvegardes: int = 7
    # Âge au-delà duquel l'historique des cycles est purgé.
    conservation_jours: int = 180

    @property
    def fuseau(self) -> ZoneInfo:
        return ZoneInfo(self.fuseau_nom)

    @property
    def verrou(self) -> Path:
        """Fichier verrou : un seul cycle à la fois sur une même base d'état."""
        return self.base.with_name(self.base.name + ".verrou")

    @property
    def dossier_sauvegardes(self) -> Path:
        return self.base.parent / "sauvegardes"

    @classmethod
    def depuis_env(cls, env: Mapping[str, str] | None = None) -> Config:
        env = os.environ if env is None else env

        def secret(nom: str) -> str | None:
            fichier = env.get(f"{nom}_FILE")
            if env.get(nom) and fichier:
                raise ErreurConfig(f"{nom} et {nom}_FILE sont tous deux définis : n'en garder qu'un")
            if fichier:
                try:
                    return Path(fichier).read_text(encoding="utf-8").strip() or None
                except OSError as e:
                    raise ErreurConfig(f"{nom}_FILE : lecture de {fichier} impossible ({e.strerror})") from e
            return env.get(nom) or None

        def texte(nom: str, defaut: str) -> str:
            return env.get(nom) or defaut

        def optionnel(nom: str) -> str | None:
            return env.get(nom) or None

        def entier(nom: str, defaut: int, minimum: int, maximum: int | None = None) -> int:
            valeur = env.get(nom)
            if not valeur:
                return defaut
            try:
                n = int(valeur)
            except ValueError as e:
                raise ErreurConfig(f"{nom} doit être un nombre entier (reçu {valeur!r})") from e
            if n < minimum or (maximum is not None and n > maximum):
                borne = f"entre {minimum} et {maximum}" if maximum is not None else f"au moins {minimum}"
                raise ErreurConfig(f"{nom} doit valoir {borne} (reçu {n})")
            return n

        def url(nom: str) -> str:
            valeur = env.get(nom, "")
            morceaux = urlsplit(valeur)
            if morceaux.scheme not in ("http", "https") or not morceaux.hostname:
                raise ErreurConfig(f"{nom} doit être une adresse http(s)://… (reçu {valeur!r})")
            if morceaux.username or morceaux.password:
                raise ErreurConfig(f"{nom} ne doit pas contenir d'identifiants : utiliser la variable du jeton")
            return valeur

        secrets = {
            "DOLIBARR_API_KEY": secret("DOLIBARR_API_KEY"),
            "OPENPROJECT_API_KEY": secret("OPENPROJECT_API_KEY"),
        }
        manquantes = [v for v in ("DOLIBARR_URL", "OPENPROJECT_URL") if not env.get(v)]
        manquantes += [nom for nom, valeur in secrets.items() if not valeur]
        if manquantes:
            raise ErreurConfig(f"variables d'environnement manquantes : {', '.join(manquantes)}")

        # FUSEAU d'abord, puis TZ (celle du conteneur) si c'est un nom de fuseau, sinon Paris.
        fuseau_nom = env.get("FUSEAU") or (env["TZ"] if _fuseau_valide(env.get("TZ")) else "Europe/Paris")
        if not _fuseau_valide(fuseau_nom):
            raise ErreurConfig(f"FUSEAU : fuseau horaire inconnu {fuseau_nom!r} (exemple : Europe/Paris)")

        webhook = secret("ALERTE_WEBHOOK_URL")
        if webhook and urlsplit(webhook).scheme not in ("http", "https"):
            raise ErreurConfig("ALERTE_WEBHOOK_URL doit être une adresse http(s)://…")

        exclus = env.get("EXCLURE_LOGINS", "admin")
        return cls(
            dolibarr_url=url("DOLIBARR_URL"),
            dolibarr_cle=str(secrets["DOLIBARR_API_KEY"]),
            openproject_url=url("OPENPROJECT_URL"),
            openproject_cle=str(secrets["OPENPROJECT_API_KEY"]),
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
            intervalle=entier("INTERVALLE_SECONDES", 120, minimum=10),
            seuil_suppressions=entier("SEUIL_SUPPRESSIONS", 5, minimum=0),
            seuil_pourcent=entier("SEUIL_POURCENT", 20, minimum=1, maximum=100),
            webhook=webhook,
            base=Path(texte("BASE_ETAT", "/data/etat.sqlite")),
            fuseau_nom=fuseau_nom,
            sauvegardes=entier("SAUVEGARDES", 7, minimum=0),
            conservation_jours=entier("CONSERVATION_JOURS", 180, minimum=7),
        )


def _fuseau_valide(nom: str | None) -> bool:
    if not nom:
        return False
    try:
        ZoneInfo(nom)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True
