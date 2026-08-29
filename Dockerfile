FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ffmpeg \
       ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app/ /app/

EXPOSE 8085

CMD ["gunicorn", \
     "--workers", "1", \
     "--threads", "16", \
     "--timeout", "0", \
     "--bind", "0.0.0.0:8085", \
     "app:app"]
