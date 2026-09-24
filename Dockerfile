# syntax=docker/dockerfile:1

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    BASE_ETAT=/data/etat.sqlite

WORKDIR /app

# Dépendances figées et vérifiées par empreinte : une image rebâtie plus tard reste identique.
COPY requirements.lock ./
RUN pip install --require-hashes -r requirements.lock

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-deps .

# Le service tourne en uid 1000 : le dossier de l'hôte monté en /data doit appartenir
# à cet uid, sinon la base d'état ne peut pas être écrite.
RUN useradd --uid 1000 --create-home dolop && mkdir -p /data && chown dolop /data
USER dolop
VOLUME ["/data"]

# « unhealthy » si aucun cycle n'a réussi depuis 15 minutes.
HEALTHCHECK --interval=60s --timeout=10s --start-period=5m --retries=2 CMD ["dolop", "sante"]

CMD ["dolop", "service"]
