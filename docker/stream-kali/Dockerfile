# Security Lab Desktop: Kali with an Xfce desktop and the common tools, streamed through noVNC.
# FOR AUTHORIZED TESTING ONLY. Built on orbitnest/stream-base:kali-1 (see app/services/images.py).
# Runs as root inside an unprivileged container (see docker/security-lab/Dockerfile for the limits).
# UNVERIFIED: not built in the development sandbox (no Docker there).
FROM orbitnest/stream-base:kali-1
USER root
ARG TOOLS="kali-tools-top10"
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      xfce4 xfce4-terminal thunar mousepad ${TOOLS} iproute2 iputils-ping net-tools dnsutils curl git \
 && rm -rf /var/lib/apt/lists/*
RUN printf 'umask 000\n' > /etc/profile.d/orbitnest-umask.sh \
 && printf '\numask 000\n' >> /etc/bash.bashrc
ENV ORBITNEST_WM=none ORBITNEST_RESOLUTION=1440x810x24
CMD ["dbus-launch", "--exit-with-session", "startxfce4"]
