"""Command line entry point: ``python3 -m gecko_relay``.

Wires the three pieces together (relay, status poller, dashboard), then blocks
until the process is asked to stop. Every flag also has a ``GECKO_*``
environment variable, because passing flags through a Portainer stack is more
awkward than setting environment values.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading

from . import __version__
from . import protocol as P
from .upstream import UpstreamError, resolve_upstream
from .poller import StatusPoller
from .relay import GeckoCloudRelay
from .web import start_web

LOG = logging.getLogger("gecko")


def _env(name: str, default=None):
    v = os.environ.get("GECKO_" + name)
    return default if v is None or v == "" else v


def _env_flag(name: str) -> bool:
    return str(_env(name, "")).strip().lower() in ("1", "true", "yes", "on")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="gecko_relay",
        description="Local stand-in for the Gecko in.touch2 cloud service.",
    )
    p.add_argument("--host", default=_env("HOST", "0.0.0.0"),
                   help="UDP bind address (default: %(default)s)")
    p.add_argument("--port", type=int, default=int(_env("PORT", P.PORT)),
                   help="UDP bind port (default: %(default)s)")
    p.add_argument("--web-host", default=_env("WEB_HOST", "0.0.0.0"),
                   help="Dashboard bind address (default: %(default)s)")
    p.add_argument("--web-port", type=int, default=int(_env("WEB_PORT", 8080)),
                   help="Dashboard bind port (default: %(default)s)")
    p.add_argument("--no-web", action="store_true", default=_env_flag("NO_WEB"),
                   help="Do not start the dashboard")
    p.add_argument("--no-poll", action="store_true", default=_env_flag("NO_POLL"),
                   help="Do not join as a client to read spa status")
    p.add_argument("--spa-id", default=_env("SPA_ID"),
                   help="Pin the spa id, e.g. SPA80:1f:12:50:a5:d1")
    p.add_argument("--client-id", default=_env("CLIENT_ID"),
                   help="Client id used by the status poller (default: random)")
    p.add_argument("--upstream", default=_env("UPSTREAM"),
                   help="Also forward to Gecko's own service, e.g. "
                        "intouch2.geckoal.com or 23.101.153.137. Keeps remote "
                        "access working; local control no longer depends on it")
    p.add_argument("--upstream-dns", default=_env("UPSTREAM_DNS"),
                   help="Resolver to look --upstream up with, e.g. 1.1.1.1. "
                        "Needed when your own DNS points those names back here")
    p.add_argument("--answer-discovery", action="store_true",
                   default=_env_flag("ANSWER_DISCOVERY"),
                   help="Answer LAN <HELLO>1</HELLO> broadcasts. Off by default: "
                        "the spa answers these itself and two answers break the app")
    p.add_argument("--log-size", type=int, default=int(_env("LOG_SIZE", 500)),
                   help="Wire log ring buffer size (default: %(default)s)")
    p.add_argument("-v", "--verbose", action="store_true", default=_env_flag("VERBOSE"),
                   help="Log every datagram, including keepalives")
    p.add_argument("--version", action="version",
                   version=f"gecko-cloud-relay {__version__}")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-12s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    LOG.info("gecko-cloud-relay %s", __version__)

    upstream = None
    if args.upstream:
        try:
            upstream = resolve_upstream(args.upstream, args.upstream_dns,
                                        our_port=args.port)
        except UpstreamError as exc:
            LOG.error("--upstream %s: %s", args.upstream, exc)
            return 1
        LOG.info("forwarding to Gecko at %s:%s as well as answering locally",
                 *upstream)

    try:
        relay = GeckoCloudRelay(host=args.host, port=args.port,
                                log_size=args.log_size, spa_id=args.spa_id,
                                answer_discovery=args.answer_discovery,
                                upstream=upstream)
    except OSError as exc:
        LOG.error("cannot bind %s:%s (%s)", args.host, args.port, exc)
        if args.port < 1024:
            LOG.error("ports below 1024 need root or CAP_NET_BIND_SERVICE")
        return 1

    # The poller is always constructed, even when disabled, so the dashboard can
    # say so rather than showing an empty panel with no explanation.
    poller = StatusPoller(relay, client_id=args.client_id,
                          enabled=not args.no_poll, spa_id=args.spa_id)

    httpd = None
    if not args.no_web:
        try:
            httpd = start_web(relay, poller, args.web_host, args.web_port)
        except OSError as exc:
            LOG.error("cannot bind dashboard to %s:%s (%s)",
                      args.web_host, args.web_port, exc)
            relay.close()
            return 1

    threading.Thread(target=poller.run, daemon=True, name="poller").start()

    def _stop(signum, _frame):
        # Closing the socket drops serve_forever out of its recvfrom loop.
        LOG.info("signal %s, shutting down", signal.Signals(signum).name)
        relay.close()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _stop)

    if args.spa_id:
        LOG.info("spa id pinned to %s", args.spa_id)
    LOG.info("point %s at this host to take over from the cloud",
             "intouch.geckoal.com / intouch2.geckoal.com")

    try:
        relay.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        poller.stop()
        relay.close()
        if httpd is not None:
            httpd.shutdown()
    LOG.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
