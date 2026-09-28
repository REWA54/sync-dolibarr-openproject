"""Configuration par variables d'environnement (celles du conteneur).

Chaque secret (jetons, webhook) peut aussi être lu dans un fichier : ``DOLIBARR_API_KEY_FILE=/run/secrets/…``
à la place de ``DOLIBARR_API_KEY``. C'est la forme attendue par les secrets Docker et Kubernetes.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
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
    # Volumes importants : taille des pages lues, délai d'attente d'une réponse, lectures simultanées.
    taille_page_dolibarr: int = 100
    taille_page_openproject: int = 200
    delai_http: int = 60
    lectures_paralleles: int = 4
    # « dolop sante » : délai sans cycle réussi (ni cycle en cours qui avance) avant de passer au rouge.
    sante_minutes: int = 15
    # Périmètre : « tout » (tous les projets actifs) ou « choisi » (projets cochés d'un côté ou de l'autre).
    perimetre: str = "tout"
    dolibarr_attribut_synchro: str = "synchro_openproject"
    op_champ_synchro: str = "Synchroniser avec Dolibarr"
    # Types de lots synchronisés (noms) ; vide : tous.
    op_types: tuple[str, ...] = ()
    # Temps passés avant cette date : ni lus ni synchronisés (historique souvent déjà facturé).
    temps_depuis: date | None = None
    # Disjoncteur de créations : au-delà, rien n'est écrit sans « --confirmer-creations ».
    seuil_creations: int = 50
    # Disjoncteur de suppressions proportionnel : seuil = max(SEUIL_SUPPRESSIONS, ce % des objets reliés).
    seuil_suppressions_pourcent: int = 1
    # Lecture incrémentale : entre deux lectures complètes (au plus tous les N minutes), le service ne
    # lit que les tâches et temps modifiés. 0 : lecture complète à chaque cycle.
    lecture_complete_minutes: int = 60
    # Dolibarr compare les dates dans le fuseau de sa base, que l'API ne dit pas : marge ajoutée à
    # « modifié depuis » (couvre une base en UTC ou à l'heure de Paris).
    marge_dolibarr_minutes: int = 180

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

        # Le code des attributs entre dans un filtre de l'API Dolibarr : les caractères de Dolibarr, sans plus.
        def attribut(nom: str, defaut: str) -> str:
            code = texte(nom, defaut)
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", code):
                raise ErreurConfig(f"{nom} : code d'attribut invalide {code!r} (lettres, chiffres, _)")
            return code

        perimetre = texte("PERIMETRE", "tout").casefold()
        if perimetre not in ("tout", "choisi"):
            raise ErreurConfig(f"PERIMETRE doit valoir « tout » ou « choisi » (reçu {perimetre!r})")
        temps_depuis = None
        if env.get("TEMPS_DEPUIS"):
            try:
                temps_depuis = date.fromisoformat(env["TEMPS_DEPUIS"])
            except ValueError as e:
                raise ErreurConfig(f"TEMPS_DEPUIS doit être une date AAAA-MM-JJ (reçu {env['TEMPS_DEPUIS']!r})") from e

        exclus = env.get("EXCLURE_LOGINS", "admin")
        return cls(
            dolibarr_url=url("DOLIBARR_URL"),
            dolibarr_cle=str(secrets["DOLIBARR_API_KEY"]),
            openproject_url=url("OPENPROJECT_URL"),
            openproject_cle=str(secrets["OPENPROJECT_API_KEY"]),
            openproject_hote=optionnel("OPENPROJECT_HOST"),
            dolibarr_attribut=attribut("DOLIBARR_ATTRIBUT", "openproject_id"),
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
            taille_page_dolibarr=entier("TAILLE_PAGE_DOLIBARR", 100, minimum=10, maximum=1000),
            taille_page_openproject=entier("TAILLE_PAGE_OPENPROJECT", 200, minimum=10, maximum=1000),
            delai_http=entier("DELAI_HTTP_SECONDES", 60, minimum=5, maximum=600),
            lectures_paralleles=entier("LECTURES_PARALLELES", 4, minimum=1, maximum=16),
            sante_minutes=entier("SANTE_MINUTES", 15, minimum=5, maximum=1440),
            perimetre=perimetre,
            dolibarr_attribut_synchro=attribut("DOLIBARR_ATTRIBUT_SYNCHRO", "synchro_openproject"),
            op_champ_synchro=texte("OPENPROJECT_CHAMP_SYNCHRO", "Synchroniser avec Dolibarr"),
            op_types=tuple(x.strip() for x in env.get("OPENPROJECT_TYPES", "").split(",") if x.strip()),
            temps_depuis=temps_depuis,
            seuil_creations=entier("SEUIL_CREATIONS", 50, minimum=1),
            seuil_suppressions_pourcent=entier("SEUIL_SUPPRESSIONS_POURCENT", 1, minimum=0, maximum=100),
            lecture_complete_minutes=entier("LECTURE_COMPLETE_MINUTES", 60, minimum=0, maximum=1440),
            marge_dolibarr_minutes=entier("MARGE_DOLIBARR_MINUTES", 180, minimum=0, maximum=1440),
        )


def _fuseau_valide(nom: str | None) -> bool:
    if not nom:
        return False
    try:
        ZoneInfo(nom)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True
