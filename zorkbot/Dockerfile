FROM python:3.13-slim-bookworm

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends procps \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
COPY src ./src

RUN pip install --no-cache-dir .

ENTRYPOINT ["zorkbot"]
