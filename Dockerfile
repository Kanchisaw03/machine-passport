FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend backend
COPY frontend frontend
COPY eval eval
COPY scripts scripts
COPY README.md README.md
COPY docs docs
RUN mkdir -p data/audio
ENV PORT=8080 DATA_DIR=/app/data
EXPOSE 8080
CMD ["sh","-c","uvicorn backend.app.main:app --host 0.0.0.0 --port ${PORT}"]
