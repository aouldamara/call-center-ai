# Base container
FROM docker.io/library/python:3.11-slim-bullseye@sha256:9f35f3a6420693c209c11bba63dcf103d88e47ebe0b205336b5168c122967edf AS base

# Build container
FROM base AS build

RUN --mount=target=/var/lib/apt/lists,type=cache,sharing=locked --mount=target=/root/.cache/pip,type=cache,sharing=locked \
  apt-get update \
  && apt-get install --yes gcc python3-dev \
  && python3 -m pip install --upgrade pip setuptools wheel

RUN python -m venv /venv
ENV PATH=/venv/bin:$PATH

COPY requirements.txt .
RUN --mount=target=/root/.cache/pip,type=cache,sharing=locked \
  python3 -m pip install --requirement requirements.txt

# Output container
FROM base

ARG VERSION
ENV VERSION=${VERSION}

RUN useradd -m appuser \
  && mkdir /app \
  && chown -R appuser:appuser /app

USER appuser

COPY --from=build /venv /venv
ENV PATH=/venv/bin:$PATH

COPY --chown=appuser:appuser . /app

CMD ["bash", "-c", "cd /app && uvicorn main:api --host 0.0.0.0 --port 8080 --proxy-headers --no-server-header --timeout-keep-alive 60 --header x-version:${VERSION}"]