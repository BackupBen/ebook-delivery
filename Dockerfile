# E-Book-Auslieferung: eine App, SQLite, kein Root.
#
# Das Image enthält keine Servernamen, Domains oder Secrets. Alles wird beim Start über
# Umgebungsvariablen gesetzt (siehe .env.example).
ARG BASE_IMAGE=ubuntu:24.04

# --- Abhängigkeiten in eine virtuelle Umgebung installieren -------------------------------
FROM ${BASE_IMAGE} AS build
ENV DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/venv
COPY requirements.txt /src/requirements.txt
# Exakt gepinnte Versionen mit Prüfsummen.
RUN /opt/venv/bin/pip install --require-hashes -r /src/requirements.txt
COPY pyproject.toml README.md /src/
COPY src /src/src
RUN /opt/venv/bin/pip install --no-deps /src \
    && /opt/venv/bin/python -m compileall -q /opt/venv/lib

# --- Laufzeit-Image -----------------------------------------------------------------------
FROM ${BASE_IMAGE}
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends python3 restic ca-certificates tzdata \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent \
        --shell /usr/sbin/nologin app \
    && mkdir -p /data /backups \
    && chown 10001:10001 /data /backups \
    && chmod 700 /data /backups

COPY --from=build /opt/venv /opt/venv

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data \
    BACKUP_DIR=/backups \
    PORT=8000

# Die App läuft ohne Root-Rechte. Beide Verzeichnisse müssen als persistente Volumes
# eingebunden werden, sonst gehen Bücher, Links und Backups bei einem Redeploy verloren.
USER 10001:10001
WORKDIR /data
VOLUME ["/data", "/backups"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["ebookctl", "healthcheck"]

STOPSIGNAL SIGTERM
CMD ["ebookctl", "serve"]
