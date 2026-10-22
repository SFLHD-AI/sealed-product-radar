# Hosted Live Sealed Product Radar (Railway service from this repo).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data

WORKDIR /app
COPY pyproject.toml ./
COPY edge ./edge
# Editable so the package finds board.seed.json and the demo fixtures next to it in /app.
RUN pip install --no-cache-dir -e .
COPY board.seed.json watchlist.example.yaml dealernet.example.csv ./
COPY tests/fixtures/demo ./tests/fixtures/demo

EXPOSE 8080
CMD ["python", "-m", "edge.webapp"]
