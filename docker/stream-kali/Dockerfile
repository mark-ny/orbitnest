# Security Lab Desktop: Kali with an Xfce desktop and the common tools, streamed through noVNC.
# FOR AUTHORIZED TESTING ONLY. Built on orbitnest/stream-base:kali-1 (see app/services/images.py).
# Runs as root inside an unprivileged container (see docker/security-lab/Dockerfile for the limits).
# UNVERIFIED: not built in the development sandbox (no Docker there).
FROM orbitnest/stream-base:kali-1
USER root
ARG TOOLS="kali-tools-top10"
ARG OSINT="theharvester recon-ng spiderfoot sherlock maltego-teeth exiftool amass dnsrecon whatweb metagoofil whois dnsenum fierce"
ARG DEFENSE="wireshark-common tshark suricata yara lynis clamav rkhunter chkrootkit binwalk tcpdump nftables fail2ban aide auditd"
ARG REDTEAM="nmap metasploit-framework hydra john hashcat sqlmap nikto gobuster ffuf impacket-scripts netexec responder wfuzz dirb enum4linux smbclient proxychains4"
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      xfce4 xfce4-terminal thunar mousepad ${TOOLS} iproute2 iputils-ping net-tools dnsutils curl git \
      tor torsocks \
 && for p in ${OSINT} ${DEFENSE} ${REDTEAM}; do \
      apt-get install -y --no-install-recommends "$p" >/dev/null 2>&1 || echo "orbitnest: skipped $p (not in this Kali snapshot)"; \
    done \
 && rm -rf /var/lib/apt/lists/*
RUN printf 'OSINT: %s\nDEFENSE: %s\nRED TEAM: %s\n' "${OSINT}" "${DEFENSE}" "${REDTEAM}" > /etc/orbitnest-tools.txt
RUN printf 'umask 000\n' > /etc/profile.d/orbitnest-umask.sh \
 && printf '\numask 000\n' >> /etc/bash.bashrc
ENV ORBITNEST_WM=none ORBITNEST_RESOLUTION=1440x810x24
CMD ["dbus-launch", "--exit-with-session", "startxfce4"]
