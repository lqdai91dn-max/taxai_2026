FROM python:3.11-slim

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Source code
COPY src/ ./src/
COPY app.py .

# Data directory (mounted as volume in docker-compose)
RUN mkdir -p data/parsed data/chroma data/graph data/patches

ENV PYTHONUNBUFFERED=1
EXPOSE 8000 8501
