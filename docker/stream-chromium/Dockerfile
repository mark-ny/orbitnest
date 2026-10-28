FROM orbitnest/stream-base:debian-1
USER root
RUN apt-get update && apt-get install -y --no-install-recommends chromium \
 && rm -rf /var/lib/apt/lists/*
USER 1000:1000
# 16-bit colour halves the screen's memory traffic; software rendering (--disable-gpu) is the lightest
# path on a virtual display. The container is the security boundary, so Chromium's own sandbox (which
# needs privileges we drop) is disabled; --test-type hides the resulting "unsupported flag" banner.
# Only this CMD line changes between tuning rounds, so rebuilding it is fast (cached layers).
ENV ORBITNEST_RESOLUTION=1280x720x16
CMD ["chromium", "--no-sandbox", "--test-type", "--no-first-run", "--no-default-browser-check", \
     "--disable-gpu", "--disable-smooth-scrolling", "--disable-extensions", "--disable-sync", \
     "--disable-background-networking", "--disable-component-update", "--renderer-process-limit=3", \
     "--disable-features=TranslateUI,MediaRouter,OptimizationHints", \
     "--start-maximized", "--user-data-dir=/home/workspace/.chromium", "--window-size=1280,720", "about:blank"]
