"""Un seul cycle à la fois sur une même base d'état.

Deux cycles simultanés (le service, plus un « dolop une-fois » lancé à la main dans le conteneur)
verraient les mêmes objets non reliés et les créeraient chacun de leur côté : des doublons.
Le verrou est un ``flock`` sur un fichier voisin de la base : le système le libère tout seul si le
processus meurt, il ne peut donc jamais rester bloqué après un arrêt brutal.

Il ne protège que les processus qui partagent le même fichier : deux machines avec chacune leur
base (un poste et le serveur) ne se voient pas. Ne jamais faire tourner le service à deux endroits.
"""

from __future__ import annotations

import fcntl
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class DejaEnCours(Exception):
    pass


@contextmanager
def verrou(chemin: Path, *, attente: float = 0.0) -> Iterator[None]:
    """Prend le verrou, en attendant au plus ``attente`` secondes qu'un autre cycle se termine."""
    chemin.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(chemin, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        limite = time.monotonic() + attente
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= limite:
                    detenteur = os.pread(fd, 32, 0).decode(errors="replace").strip() or "?"
                    raise DejaEnCours(
                        f"un autre cycle tourne déjà sur cette base (processus {detenteur}). "
                        "Réessayer dans quelques minutes."
                    ) from None
                time.sleep(0.5)
        os.ftruncate(fd, 0)
        os.pwrite(fd, f"{os.getpid()}\n".encode(), 0)
        yield
    finally:
        os.close(fd)  # libère le verrou
