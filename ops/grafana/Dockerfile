# Grafana Alloy with the MarketSignalOS scrape config baked in.
#
# Build from the REPO ROOT (the COPY path is repo-relative):
#   docker build -f ops/grafana/Dockerfile -t msos-alloy .
#
# On Railway, set this file as the service's Dockerfile path and leave the
# build context at the repo root.
FROM grafana/alloy:latest

COPY ops/grafana/config.alloy /etc/alloy/config.alloy

# Alloy's own UI (targets, component health) — useful for confirming the
# scrape target is UP before going looking in Grafana Cloud.
EXPOSE 12345

CMD ["run", "/etc/alloy/config.alloy", \
     "--server.http.listen-addr=0.0.0.0:12345", \
     "--storage.path=/var/lib/alloy/data"]
