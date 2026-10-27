FROM orbitnest/stream-base:debian-1
USER root
RUN apt-get update && apt-get install -y --no-install-recommends firefox-esr \
 && rm -rf /var/lib/apt/lists/*
USER 1000:1000
CMD ["sh", "-c", "mkdir -p \"$HOME/.mozilla-profile\" && exec firefox-esr --no-remote --profile \"$HOME/.mozilla-profile\""]
