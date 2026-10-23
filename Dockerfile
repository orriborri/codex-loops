FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends git docker.io ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY loop.py /app/loop.py
ENTRYPOINT ["python", "-u", "/app/loop.py"]
