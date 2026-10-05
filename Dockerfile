FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 financebot \
    && useradd --uid 10001 --gid financebot --no-create-home financebot \
    && mkdir -p /app/data /app/backups \
    && chown financebot:financebot /app/data /app/backups

COPY financebot/ ./financebot/

USER financebot

CMD ["python", "-m", "financebot"]
