"""Alertes par webhook (format {"title", "message"}, celui des webhooks Home Assistant).

Deux sortes d'alertes :
- ponctuelles (conflit, client introuvable…) : envoyées une fois, rappelées toutes les 24 h tant
  que le problème revient à chaque cycle, éteintes en silence quand il disparaît ;
- permanentes (cycles en échec, disjoncteur) : même rythme, plus un message de rétablissement.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta

import httpx

from .conversions import age
from .etat import Etat

log = logging.getLogger("dolop.alertes")

TITRE = "Synchro Dolibarr ↔ OpenProject"

Envoi = Callable[[str, str], None]


def envoi_webhook(url: str) -> Envoi:
    def envoyer(titre: str, message: str) -> None:
        reponse = httpx.post(url, json={"title": titre, "message": message}, timeout=10)
        reponse.raise_for_status()

    return envoyer


class Alertes:
    def __init__(self, etat: Etat, envoi: Envoi | None, rappel: timedelta = timedelta(hours=24)):
        self.etat = etat
        self.envoi = envoi
        self.rappel = rappel
        self.levees: set[str] = set()
        self.envoyees: list[tuple[str, str]] = []

    def lever(self, cle: str, message: str, *, permanente: bool = False, titre: str = TITRE) -> None:
        self.levees.add(cle)
        self.etat.lever_alerte(cle, titre, message, permanente)
        row = self.etat.alerte(cle)
        assert row is not None
        dernier = row["dernier_envoi"]
        du = dernier is None or age(datetime.fromisoformat(dernier)) >= self.rappel
        if du and self._envoyer(titre, message):
            self.etat.noter_envoi(cle)

    def retablir(self, cle: str, message: str) -> None:
        row = self.etat.alerte(cle)
        if row is None or not row["active"]:
            return
        self.etat.eteindre_alerte(cle)
        if row["permanente"] and row["dernier_envoi"] is not None:
            self._envoyer(f"{TITRE} — rétabli", message)

    def fin_de_cycle(self) -> None:
        """Les alertes ponctuelles non relevées pendant ce cycle sont résolues."""
        for row in self.etat.alertes_actives(permanentes=False):
            if row["cle"] not in self.levees:
                self.etat.eteindre_alerte(row["cle"])
        self.levees.clear()

    def _envoyer(self, titre: str, message: str) -> bool:
        self.envoyees.append((titre, message))
        log.warning("ALERTE %s : %s", titre, message)
        if self.envoi is None:
            return True
        try:
            self.envoi(titre, message)
            return True
        except Exception as e:  # une alerte qui échoue ne doit pas faire tomber le cycle
            log.error("envoi de l'alerte impossible : %s", e)
            return False
