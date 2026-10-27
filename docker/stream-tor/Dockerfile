# Official Tor Browser, downloaded and signature-verified at build time.
#
# The build FAILS (it never falls back to an unverified download) if:
#   * the signing key's fingerprint is not the one below, or
#   * the detached signature does not verify.
# Compare TOR_KEY_FPR with https://support.torproject.org/tbb/how-to-verify-signature/ before first
# use. Pass --build-arg TOR_VERSION=x.y.z to pin a version; otherwise the latest stable is looked up.
# x86_64 only. UNVERIFIED: not built in the development sandbox (no Docker, no internet there).
FROM orbitnest/stream-base:debian-1
USER root
ARG TOR_VERSION=""
ARG TOR_KEY_FPR="EF6E286DDA85EA2A4BA7DE684E2C6E8793298290"

RUN apt-get update && apt-get install -y --no-install-recommends \
      gnupg dirmngr curl xz-utils \
      libgtk-3-0 libdbus-glib-1-2 libasound2 libx11-xcb1 libxt6 libxtst6 libxcomposite1 libpci3 \
 && rm -rf /var/lib/apt/lists/*

RUN set -eu; \
    [ "$(uname -m)" = "x86_64" ] || { echo "Tor Browser image supports x86_64 only" >&2; exit 1; }; \
    V="$TOR_VERSION"; \
    if [ -z "$V" ]; then \
      V="$(curl -fsSL https://aus1.torproject.org/torbrowser/update_3/release/downloads.json \
           | grep -o '"version" *: *"[0-9.]*"' | head -n1 | cut -d'"' -f4)"; \
    fi; \
    case "$V" in ''|*[!0-9.]*) echo "could not determine the Tor Browser version; pass --build-arg TOR_VERSION=x.y.z" >&2; exit 1;; esac; \
    F="tor-browser-linux-x86_64-$V.tar.xz"; \
    cd /tmp; \
    curl -fsSLO "https://dist.torproject.org/torbrowser/$V/$F"; \
    curl -fsSLO "https://dist.torproject.org/torbrowser/$V/$F.asc"; \
    export GNUPGHOME="$(mktemp -d)"; \
    gpg --batch --auto-key-locate nodefault,wkd --locate-keys torbrowser@torproject.org; \
    FOUND="$(gpg --batch --with-colons --fingerprint torbrowser@torproject.org | awk -F: '/^fpr:/ {print $10; exit}')"; \
    [ "$FOUND" = "$TOR_KEY_FPR" ] || { echo "signing key fingerprint mismatch: $FOUND" >&2; exit 1; }; \
    gpg --batch --verify "$F.asc" "$F"; \
    mkdir -p /opt && tar -xJf "$F" -C /opt && mv /opt/tor-browser /opt/tor-browser-app; \
    chown -R 1000:1000 /opt/tor-browser-app; \
    rm -rf "$F" "$F.asc" "$GNUPGHOME"; \
    echo "$V" > /opt/tor-browser-app/ORBITNEST_VERSION

USER 1000:1000
CMD ["/opt/tor-browser-app/Browser/start-tor-browser", "--verbose"]
