FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN useradd --create-home --shell /bin/bash botuser
RUN mkdir -p /data && chown -R botuser:botuser /data

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY bot /app/bot

VOLUME ["/data"]

USER botuser

CMD ["python", "-m", "bot.main"]
