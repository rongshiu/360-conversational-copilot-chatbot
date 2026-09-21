FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        gcc \
        wget \
        curl \
        dnsutils \
        ca-certificates \
        openssl \
    && update-ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml /app/pyproject.toml

RUN pip install --no-cache-dir -U pip \
    && pip install --no-cache-dir .

COPY alembic.ini /app/alembic.ini
COPY alembic /app/alembic
COPY app /app/app
COPY data /app/data
# scripts/ holds generate_mock_data.py (the seed profile runs it in-container) and
# bootstrap_roles.sql, which the postgres init mounts from the host.
COPY scripts /app/scripts

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]