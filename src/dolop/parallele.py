"""Lectures simultanées, pour les données qui ne se lisent qu'objet par objet (contacts d'une tâche…).

Réservé aux lectures : une écriture ne part jamais en parallèle, l'ordre des écritures compte
(un projet avant ses tâches) et une écriture en échec ne doit pas en entraîner d'autres.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor


def en_parallele[E, R](lecture: Callable[[E], R], elements: Iterable[E], simultanees: int) -> list[R]:
    """``[lecture(e) for e in elements]``, au plus ``simultanees`` à la fois ; même ordre, première erreur levée."""
    liste = list(elements)
    if simultanees <= 1 or len(liste) <= 1:
        return [lecture(e) for e in liste]
    with ThreadPoolExecutor(max_workers=min(simultanees, len(liste)), thread_name_prefix="lecture") as pool:
        return list(pool.map(lecture, liste))
