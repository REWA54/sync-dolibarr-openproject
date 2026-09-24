# syntax=docker/dockerfile:1

# Image de base épinglée par empreinte : une image rebâtie plus tard part exactement de la même
# base. Dependabot propose chaque nouvelle empreinte (correctifs Debian et Python).
FROM python:3.13-slim@sha256:8d9d0b8bcf6506481eae4907c18f5e3e7902e629f5f6d684f9e7c32e85e3ddf0 AS base

# ------------------------------------------------------------------------ construction
FROM base AS construction

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /src

# Tout ce qui est téléchargé est vérifié par empreinte : dépendances et outil de construction.
COPY requirements.txt requirements-build.txt ./
RUN python -m venv --without-pip /opt/dolop \
 && pip --python /opt/dolop/bin/python install --require-hashes --no-deps -r requirements.txt \
 && python -m venv /opt/construction \
 && /opt/construction/bin/pip install --require-hashes --no-deps -r requirements-build.txt

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN /opt/construction/bin/pip wheel --no-deps --no-build-isolation --wheel-dir /roues . \
 && pip --python /opt/dolop/bin/python install --no-deps /roues/*.whl

# ---------------------------------------------------------------------------- exécution
FROM base

LABEL org.opencontainers.image.title="sync-dolibarr-openproject" \
      org.opencontainers.image.description="Synchronisation Dolibarr ↔ OpenProject (projets, tâches, temps)" \
      org.opencontainers.image.source="https://github.com/REWA54/sync-dolibarr-openproject" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/dolop/bin:$PATH \
    BASE_ETAT=/data/etat.sqlite

# pip (et le setuptools qu'il embarque) ne sert à rien une fois l'image bâtie : c'est seulement
# de la surface d'attaque, et la source des failles signalées par les scanners.
# Le service tourne en uid 1000 : le dossier de l'hôte monté en /data doit appartenir à cet uid.
RUN python -m pip uninstall --yes --quiet pip \
 && rm -rf /usr/local/lib/python3.*/ensurepip \
 && groupadd --gid 1000 dolop \
 && useradd --uid 1000 --gid 1000 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin dolop \
 && mkdir -p /data \
 && chown 1000:1000 /data

COPY --from=construction /opt/dolop /opt/dolop

USER 1000:1000
WORKDIR /data
VOLUME ["/data"]

# « unhealthy » si aucun cycle n'a réussi depuis 15 minutes.
HEALTHCHECK --interval=60s --timeout=10s --start-period=5m --retries=2 CMD ["dolop", "sante"]

CMD ["dolop", "service"]
