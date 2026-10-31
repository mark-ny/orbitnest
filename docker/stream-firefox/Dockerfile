FROM orbitnest/stream-base:debian-1
USER root
RUN apt-get update && apt-get install -y --no-install-recommends firefox-esr \
 && rm -rf /var/lib/apt/lists/*
# Privacy: resist fingerprinting, no WebRTC, no telemetry, no geolocation, no prefetching.
RUN mkdir -p /usr/lib/firefox-esr/distribution \
 && printf '%s\n' '{"policies":{"DisableTelemetry":true,"DisableFirefoxStudies":true,"DisablePocket":true,' \
  '"DisableFormHistory":true,"OfferToSaveLogins":false,"DontCheckDefaultBrowser":true,' \
  '"Preferences":{"media.peerconnection.enabled":{"Value":false,"Status":"locked"},' \
  '"privacy.resistFingerprinting":{"Value":true,"Status":"locked"},' \
  '"geo.enabled":{"Value":false,"Status":"locked"},' \
  '"network.dns.disablePrefetch":{"Value":true,"Status":"locked"},' \
  '"network.prefetch-next":{"Value":false,"Status":"locked"},' \
  '"network.http.speculative-parallel-limit":{"Value":0,"Status":"locked"},' \
  '"network.http.http3.enable":{"Value":false,"Status":"locked"},' \
  '"beacon.enabled":{"Value":false,"Status":"locked"}}}}' > /usr/lib/firefox-esr/distribution/policies.json
USER 1000:1000
ENV ORBITNEST_RESOLUTION=1280x720x16 TZ=UTC LANG=en_US.UTF-8
# Lighter profile: fewer content processes, software rendering, no smooth scrolling.
CMD ["sh", "-c", "P=\"$HOME/.mozilla-profile\"; mkdir -p \"$P\"; printf '%s\\n' 'user_pref(\"dom.ipc.processCount\", 2);' 'user_pref(\"layers.acceleration.disabled\", true);' 'user_pref(\"general.smoothScroll\", false);' 'user_pref(\"browser.tabs.unloadOnLowMemory\", true);' 'user_pref(\"network.http.http3.enable\", false);' 'user_pref(\"intl.accept_languages\", \"en-US, en\");' > \"$P/user.js\"; exec firefox-esr --no-remote --profile \"$P\""]
