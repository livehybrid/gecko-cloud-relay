FROM python:3.12-alpine

LABEL org.opencontainers.image.title="gecko-cloud-relay" \
      org.opencontainers.image.description="Local stand-in for the Gecko in.touch2 cloud service" \
      org.opencontainers.image.source="https://github.com/livehybrid/gecko-cloud-relay"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Standard library only, so there is nothing to install. That is deliberate:
# this runs on a box that may have no route to the internet.
COPY gecko_relay/ ./gecko_relay/

RUN adduser -D -H -u 10022 gecko
USER gecko

EXPOSE 10022/udp
EXPOSE 8080/tcp

# The dashboard doubles as the liveness signal. With GECKO_NO_WEB set there is
# nothing to probe, so the check just confirms the process is still there.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python3 -c "import os,sys,urllib.request; sys.exit(0) if os.environ.get('GECKO_NO_WEB') else urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('GECKO_WEB_PORT','8080')+'/api/state', timeout=3)"

ENTRYPOINT ["python3", "-m", "gecko_relay"]
