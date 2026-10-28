FROM orbitnest/stream-base:debian-1
USER root
RUN apt-get update && apt-get install -y --no-install-recommends firefox-esr \
 && rm -rf /var/lib/apt/lists/*
USER 1000:1000
ENV ORBITNEST_RESOLUTION=1280x720x16
# Lighter profile: fewer content processes, software rendering, no smooth scrolling.
CMD ["sh", "-c", "P=\"$HOME/.mozilla-profile\"; mkdir -p \"$P\"; printf '%s\\n' 'user_pref(\"dom.ipc.processCount\", 2);' 'user_pref(\"layers.acceleration.disabled\", true);' 'user_pref(\"general.smoothScroll\", false);' 'user_pref(\"browser.tabs.unloadOnLowMemory\", true);' > \"$P/user.js\"; exec firefox-esr --no-remote --profile \"$P\""]
