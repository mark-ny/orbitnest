# Security Lab (command line): Kali Linux with the common tools. For AUTHORIZED testing only.
#
# Runs as root INSIDE the container, but the container itself is unprivileged: all Linux capabilities
# are dropped except a small default set plus the ones the user ticks when creating the workspace,
# no host network, no devices, no Docker socket, no-new-privileges, memory/CPU/PID limits.
# Build a bigger toolset with:  docker build --build-arg TOOLS=kali-linux-headless ...   (many GB)
# UNVERIFIED: not built in the development sandbox (no Docker there).
FROM kalilinux/kali-rolling
ARG TOOLS="kali-tools-top10"
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ${TOOLS} iproute2 iputils-ping net-tools dnsutils curl wget git vim nano tmux python3 python3-pip \
      procps ca-certificates \
 && rm -rf /var/lib/apt/lists/*
# Files root creates in the (host-visible) workspace folder should stay removable by the control plane.
RUN printf 'umask 000\n' > /etc/profile.d/orbitnest-umask.sh \
 && printf '\numask 000\n' >> /etc/bash.bashrc
WORKDIR /home/workspace
CMD ["sleep", "infinity"]
