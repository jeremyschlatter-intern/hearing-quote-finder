FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY database.py .
COPY embeddings.py .
COPY extract.py .
COPY ingest.py .
COPY server.py .
COPY index.html .

EXPOSE 8080

CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--worker-class", "uvicorn.workers.UvicornWorker", "--timeout", "120", "server:app"]
