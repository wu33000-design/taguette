FROM python:3.12-bookworm AS build
WORKDIR /build
RUN pip install --no-cache-dir poetry==2.2.1 poetry-plugin-export==1.9.0
COPY pyproject.toml poetry.lock README.rst tests.py ./
COPY scripts/update_translations.sh scripts/
COPY po po
RUN poetry config virtualenvs.create false \
    && poetry install --only=i18n --no-root --no-interaction \
    && scripts/update_translations.sh \
    && poetry export --only=main -E postgres -o requirements.txt

FROM python:3.12-bookworm
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y \
    --no-install-recommends calibre wv tini gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=build /build/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY pyproject.toml poetry.lock README.rst tests.py ./
COPY taguette taguette
COPY --from=build /build/taguette/l10n taguette/l10n
COPY contrib/cloud-run/config.py /app/cloud-config.py
RUN pip install --no-cache-dir --no-deps . \
    && useradd --create-home --uid 10001 taguette
ENV PYTHONUNBUFFERED=1 TAGUETTE_CONVERT_CONCURRENCY=1 QT_QPA_PLATFORM=offscreen \
    XDG_CACHE_HOME=/tmp/taguette-cache XDG_CONFIG_HOME=/tmp/taguette-config
USER taguette
EXPOSE 8080
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["taguette", "--no-browser", "server", "/app/cloud-config.py"]
