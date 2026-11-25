# One image for every Python service (gateway, payment, account, bank-mock,
# worker, migrate). docker-compose.yml chooses what each container runs.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first so code changes don't invalidate this (slow) layer.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY common ./common
COPY gateway ./gateway
COPY payment_service ./payment_service
COPY account_service ./account_service
COPY bank_mock ./bank_mock
COPY worker ./worker
COPY db ./db

RUN useradd --create-home --uid 10001 app
USER app

EXPOSE 8000
CMD ["python", "-c", "print('Pick a command in docker-compose.yml')"]
