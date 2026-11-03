FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ./
COPY config.example.json ./

ENV CONFIG_PATH=/app/config.json \
    PORT=8080 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

EXPOSE 8080

# Runs as uid 1000 / gid 101 (uid of the `ubuntu` account that owns the
# mounted config file, gid of the `lxd` group) — see docker-compose.yml.
USER 1000:101

CMD ["python", "app.py"]
