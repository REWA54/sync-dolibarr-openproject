"""Client HTTP commun aux deux outils.

Les lectures sont retentées sur les pannes passagères (connexion, 429, 502/503/504). Une écriture
ne l'est que si la connexion n'a jamais pu s'établir : la requête n'est alors jamais partie. Au-delà,
une écriture retentée à l'aveugle peut créer un doublon ; la reprise des écritures interrompues est
l'affaire du moteur (créations « en cours » et recherche de jumeau).
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

from .adaptateurs import ErreurApi

log = logging.getLogger("dolop.http")

_PASSAGERS = {429, 502, 503, 504}
# Erreurs levées avant l'envoi de la requête : la rejouer ne peut rien écrire deux fois.
_AVANT_ENVOI = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
_ATTENTE_MAX = 30.0


class Introuvable(ErreurApi):
    """404 : l'outil confirme que l'objet n'existe pas (ou plus)."""


class ClientHttp:
    def __init__(
        self,
        nom: str,
        base: str,
        *,
        entetes: dict[str, str] | None = None,
        auth: httpx.Auth | tuple[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
        essais: int = 3,
        pause: float = 1.5,
    ):
        self.nom = nom
        self.essais = essais
        self.pause = pause
        self.http = httpx.Client(
            base_url=base.rstrip("/"),
            headers={"Accept": "application/json", **(entetes or {})},
            auth=auth,
            timeout=httpx.Timeout(30.0, connect=10.0),
            transport=transport,
            follow_redirects=False,
        )

    def fermer(self) -> None:
        self.http.close()

    def _erreur(self, methode: str, chemin: str, reponse: httpx.Response) -> ErreurApi:
        corps = reponse.text[:400].replace("\n", " ")
        message = f"{self.nom} {methode} {chemin} → {reponse.status_code} : {corps}"
        if reponse.status_code == 404:
            return Introuvable(message, 404)
        return ErreurApi(message, reponse.status_code)

    def requete(
        self,
        methode: str,
        chemin: str,
        *,
        params: dict[str, Any] | None = None,
        corps: Any = None,
    ) -> Any:
        lecture = methode == "GET"
        for essai in range(1, self.essais + 1):
            try:
                reponse = self.http.request(
                    methode,
                    chemin,
                    params=params,
                    content=None if corps is None else json.dumps(corps, ensure_ascii=False),
                    headers={"Content-Type": "application/json"} if corps is not None else None,
                )
            except httpx.TransportError as e:
                if essai < self.essais and (lecture or isinstance(e, _AVANT_ENVOI)):
                    log.warning("%s %s %s : %s — nouvel essai", self.nom, methode, chemin, e)
                    time.sleep(self.pause * essai)
                    continue
                raise ErreurApi(f"{self.nom} {methode} {chemin} : injoignable ({e})") from e
            if lecture and reponse.status_code in _PASSAGERS and essai < self.essais:
                log.warning("%s %s %s → %s — nouvel essai", self.nom, methode, chemin, reponse.status_code)
                time.sleep(self._attente(reponse, essai))
                continue
            if reponse.status_code >= 400:
                raise self._erreur(methode, chemin, reponse)
            if reponse.status_code == 204 or not reponse.content:
                return None
            try:
                return reponse.json()
            except ValueError as e:
                raise ErreurApi(f"{self.nom} {methode} {chemin} : réponse qui n'est pas du JSON") from e
        raise AssertionError("inatteignable")

    def _attente(self, reponse: httpx.Response, essai: int) -> float:
        """Pause avant de relire : ``Retry-After`` si le serveur en donne un (plafonné), sinon croissante."""
        try:
            demande = float(reponse.headers.get("Retry-After", ""))
        except ValueError:
            return self.pause * essai
        return min(max(demande, 0.0), _ATTENTE_MAX)

    def get(self, chemin: str, **params: Any) -> Any:
        return self.requete("GET", chemin, params={k: v for k, v in params.items() if v is not None})

    def post(self, chemin: str, corps: Any = None) -> Any:
        return self.requete("POST", chemin, corps=corps if corps is not None else {})

    def put(self, chemin: str, corps: Any) -> Any:
        return self.requete("PUT", chemin, corps=corps)

    def patch(self, chemin: str, corps: Any) -> Any:
        return self.requete("PATCH", chemin, corps=corps)

    def delete(self, chemin: str) -> Any:
        return self.requete("DELETE", chemin)
