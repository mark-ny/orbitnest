# Tor gateway: owns a workspace's network namespace and forces ALL of its traffic through Tor.
#
# The workspace container joins this container's network namespace (--network container:...), has
# every capability dropped, and therefore cannot change the firewall installed here. Anything that
# is not Tor's own traffic is either redirected into Tor (TCP, DNS) or dropped (UDP, ICMP, IPv6).
# If this container stops, the workspace has no route at all: it fails closed, never open.
# UNVERIFIED: not built or run in the development sandbox (no Docker there).
FROM debian:bookworm-slim
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends tor iptables ca-certificates procps curl \
 && rm -rf /var/lib/apt/lists/*
COPY torrc /etc/tor/orbitnest-torrc
COPY orbitnest-gateway /usr/local/bin/orbitnest-gateway
COPY orbitnest-check /usr/local/bin/orbitnest-check
RUN chmod 0755 /usr/local/bin/orbitnest-gateway /usr/local/bin/orbitnest-check
ENTRYPOINT ["/usr/local/bin/orbitnest-gateway"]
