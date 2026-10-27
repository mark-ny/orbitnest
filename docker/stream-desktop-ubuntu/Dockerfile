FROM orbitnest/stream-base:ubuntu-1
USER root
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends xfce4 xfce4-terminal thunar mousepad \
 && rm -rf /var/lib/apt/lists/*
USER 1000:1000
ENV ORBITNEST_WM=none ORBITNEST_RESOLUTION=1440x810x24
CMD ["dbus-launch", "--exit-with-session", "startxfce4"]
