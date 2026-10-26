# Shared base for streamed workspaces: virtual display + VNC (loopback only) + noVNC client files.
# Built twice by `python -m app build-images`:
#   orbitnest/stream-base:debian-1  (BASE_IMAGE=debian:bookworm-slim)  for browsers
#   orbitnest/stream-base:ubuntu-1  (BASE_IMAGE=ubuntu:24.04)          for the Ubuntu desktop
# UNVERIFIED: not built in the development sandbox (no Docker there).
ARG BASE_IMAGE=debian:bookworm-slim
FROM ${BASE_IMAGE}

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      xvfb x11vnc openbox socat novnc dbus-x11 x11-utils x11-xserver-utils xdg-utils \
      fonts-liberation fonts-noto-color-emoji ca-certificates procps \
 && rm -rf /var/lib/apt/lists/*

# A non-root user with uid 1000 (the control plane runs containers as 1000:1000, or as its own uid).
RUN (id ubuntu >/dev/null 2>&1 && userdel -r ubuntu || true) \
 && useradd --create-home --uid 1000 --shell /bin/bash workspace \
 && mkdir -p /home/workspace && chown 1000:1000 /home/workspace

COPY orbitnest-session /usr/local/bin/orbitnest-session
RUN chmod 0755 /usr/local/bin/orbitnest-session \
 && test -f /usr/share/novnc/core/rfb.js

ENV HOME=/home/workspace
WORKDIR /home/workspace
USER 1000:1000
ENTRYPOINT ["/usr/local/bin/orbitnest-session"]
