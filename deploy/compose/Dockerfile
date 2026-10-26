# Control-plane image. UNVERIFIED: not built in the development sandbox (no Docker there).
FROM python:3.12-slim
RUN useradd --create-home --uid 10001 orbitnest \
 && apt-get update && apt-get install -y --no-install-recommends docker.io \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt argon2-cffi
COPY backend/app ./app
USER orbitnest
EXPOSE 8765
CMD ["python", "-m", "app", "serve"]
