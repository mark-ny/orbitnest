# Security Lab (command line): Kali Linux with the common tools. For AUTHORIZED testing only.
#
# Runs as root INSIDE the container, but the container itself is unprivileged: all Linux capabilities
# are dropped except a small default set plus the ones the user ticks when creating the workspace,
# no host network, no devices, no Docker socket, no-new-privileges, memory/CPU/PID limits.
# Build a bigger toolset with:  docker build --build-arg TOOLS=kali-linux-headless ...   (many GB)
# UNVERIFIED: not built in the development sandbox (no Docker there).
FROM kalilinux/kali-rolling
ARG TOOLS="kali-tools-top10"
ARG OSINT="theharvester recon-ng spiderfoot sherlock maltego-teeth exiftool amass dnsrecon whatweb metagoofil whois dnsenum fierce"
ARG DEFENSE="wireshark-common tshark suricata yara lynis clamav rkhunter chkrootkit binwalk tcpdump nftables fail2ban aide auditd"
ARG REDTEAM="nmap metasploit-framework hydra john hashcat sqlmap nikto gobuster ffuf impacket-scripts netexec responder wfuzz dirb enum4linux smbclient proxychains4"
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ${TOOLS} iproute2 iputils-ping net-tools dnsutils curl wget git vim nano tmux python3 python3-pip \
      procps ca-certificates tor torsocks \
 && for p in ${OSINT} ${DEFENSE} ${REDTEAM}; do \
      apt-get install -y --no-install-recommends "$p" >/dev/null 2>&1 || echo "orbitnest: skipped $p (not in this Kali snapshot)"; \
    done \
 && rm -rf /var/lib/apt/lists/*
# NOTE: when the workspace uses Tor egress, only TCP connects work (use "nmap -sT -Pn"); SYN/UDP/ICMP
# scans cannot cross Tor. Tools are listed by purpose in /etc/orbitnest-tools.txt.
RUN printf 'OSINT: %s\nDEFENSE: %s\nRED TEAM: %s\n' "${OSINT}" "${DEFENSE}" "${REDTEAM}" > /etc/orbitnest-tools.txt
# Files root creates in the (host-visible) workspace folder should stay removable by the control plane.
RUN printf 'umask 000\n' > /etc/profile.d/orbitnest-umask.sh \
 && printf '\numask 000\n' >> /etc/bash.bashrc
WORKDIR /home/workspace
CMD ["sleep", "infinity"]
