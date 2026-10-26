FROM orbitnest/stream-base:debian-1
USER root
RUN apt-get update && apt-get install -y --no-install-recommends chromium \
 && rm -rf /var/lib/apt/lists/*
USER 1000:1000
# The container is the security boundary, so Chromium's own sandbox (which needs privileges we drop)
# is disabled; --test-type hides the resulting "unsupported flag" banner.
CMD ["chromium", "--no-sandbox", "--test-type", "--no-first-run", "--start-maximized", \
     "--user-data-dir=/home/workspace/.chromium", "--window-size=1280,720", "about:blank"]
