FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    IS_DOCKER=1

WORKDIR /app

# Sync script version — bump to pull a newer upstream release
ARG SYNC_SCRIPT_URL=https://raw.githubusercontent.com/Salvoxia/immich-folder-album-creator/main/immich_auto_album.py

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt \
 && rm -rf /tmp/* /var/tmp/*

# Upstream sync logic — refresh with: docker compose build --no-cache
ADD https://raw.githubusercontent.com/Salvoxia/immich-folder-album-creator/main/immich_auto_album.py /app/immich_auto_album.py

COPY server.py share_sync.py /app/
COPY templates/ /app/templates/

# Non-root, matching upstream image convention
RUN useradd -u 1000 -m syncuser && chown -R 1000:1000 /app && mkdir -p /app/data /app/config
USER 1000:1000

EXPOSE 8080

CMD ["python3", "/app/server.py"]
