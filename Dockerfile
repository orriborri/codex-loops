FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends git docker.io ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY loop.py /app/loop.py
COPY workflow.py /app/workflow.py
COPY telemetry.py /app/telemetry.py
COPY reviewing.py baseline.py /app/
ENTRYPOINT ["python", "-u", "/app/loop.py"]
