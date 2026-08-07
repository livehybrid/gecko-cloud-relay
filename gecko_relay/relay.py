"""The relay itself: stands in for Gecko's cloud service on UDP 10022.

What the real service appears to do, from the captures:
  * answers the EN module's <PINGS>1</PINGS> keepalive with <PINGS>0</PINGS>
  * answers a client's <ISTHR>{spa_id}</ISTHR> with <BONJR>0</BONJR>
  * forwards <PACKT> envelopes between client and spa, unmodified

There is no visible authentication and no encryption. This reimplements that.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from . import protocol as P

LOG = logging.getLogger("gecko.relay")

PING_STALE_AFTER = 90.0      # EN pings every ~21 s; 4 misses and we call it gone

# Traffic for a client that is not here is expected and unfixable, and a module
# that is retrying can send it several times a second. Keep a little of it in
# the wire log for diagnosis and drop the rest on the floor.
UNROUTABLE_LOG_FIRST = 3
UNROUTABLE_LOG_EVERY = 250


@dataclass
class Endpoint:
    ident: str
    addr: tuple[str, int]
    kind: str                       # "spa" or "client"
    provisional: bool = False       # seen pinging, but has not named itself yet
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    rx: int = 0
    tx: int = 0

    @property
    def is_spa(self) -> bool:
        return self.kind == "spa"

    def as_dict(self) -> dict:
        return {
            "id": self.ident,
            "addr": f"{self.addr[0]}:{self.addr[1]}",
            "kind": self.kind + (" (unnamed)" if self.provisional else ""),
            "age_s": round(time.time() - self.first_seen, 1),
            "idle_s": round(time.time() - self.last_seen, 1),
            "rx": self.rx,
            "tx": self.tx,
        }


class EventLog:
    """Bounded ring of decoded datagrams, for the console and the web UI."""

    def __init__(self, size: int = 500):
        self._events = deque(maxlen=size)
        self._lock = threading.Lock()
        self._seq = 0

    def add(self, direction: str, peer: str, msg: Optional[P.Message],
            raw: bytes, note: str = "") -> None:
        with self._lock:
            self._seq += 1
            self._events.append({
                "seq": self._seq,
                "ts": time.time(),
                "dir": direction,          # "in" | "out" | "fwd" | "sys"
                "peer": peer,
                "kind": msg.kind if msg else "?",
                "verb": msg.verb if msg else "",
                "src": msg.src if msg else "",
                "dst": msg.dst if msg else "",
                "summary": msg.describe() if msg else raw[:48].hex(),
                "note": note,
                "len": len(raw),
                "hex": raw[:160].hex(),
            })

    def since(self, seq: int) -> list[dict]:
        with self._lock:
            return [e for e in self._events if e["seq"] > seq]

    def tail(self, n: int) -> list[dict]:
        with self._lock:
            return list(self._events)[-n:]


class GeckoCloudRelay:
    def __init__(self, host: str = "0.0.0.0", port: int = P.PORT,
                 log_size: int = 500, spa_id: Optional[str] = None):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.sock.bind((host, port))
        self.sock.settimeout(0.5)
        self.endpoints: dict[str, Endpoint] = {}
        self.by_addr: dict[tuple[str, int], str] = {}
        self.bindings: dict[str, str] = {}       # client id -> spa id
        self.expected_spa_id = spa_id            # optional pin, from --spa-id
        self.pending_binding: dict[tuple, str] = {}   # addr -> spa id from ISTHR
        self.local_client = None                 # in-process client, i.e. the poller
        self.unroutable: dict[str, int] = {}      # dst id -> how many we have dropped
        self.events = EventLog(log_size)
        self.started = time.time()
        self.packets_in = 0
        self.packets_out = 0
        self.packets_dropped = 0
        self.running = True
        self.lock = threading.RLock()

    # -- registry ----------------------------------------------------------

    def register_ping_source(self, addr: tuple[str, int]) -> Endpoint:
        """A keepalive arrived. It carries no identity, only a source address.

        The EN module pings before it ever names itself, so track it under a
        provisional id and upgrade once we learn the real one, either from a
        PACKT it sends or from a client asking for it by name via ISTHR.
        """
        with self.lock:
            ident = self.by_addr.get(addr)
            if ident and ident in self.endpoints:
                ep = self.endpoints[ident]
                ep.last_seen = time.time()
                ep.rx += 1
                return ep
            if self.expected_spa_id:
                return self.register(self.expected_spa_id, addr, "spa")
            provisional = f"spa@{addr[0]}:{addr[1]}"
            ep = self.register(provisional, addr, "spa")
            ep.provisional = True
            return ep

    def adopt_identity(self, real_id: str, addr: tuple[str, int]) -> Optional[Endpoint]:
        """Attach a real spa id to a provisional endpoint at the same address."""
        with self.lock:
            current = self.by_addr.get(addr)
            if current == real_id:
                return self.endpoints.get(real_id)
            if current and current in self.endpoints:
                ep = self.endpoints.pop(current)
                if ep.provisional:
                    LOG.info("spa at %s:%s identified as %s", addr[0], addr[1], real_id)
                    self.events.add("sys", f"{addr[0]}:{addr[1]}", None, b"",
                                    note=f"identified as {real_id}")
                    ep.ident = real_id
                    ep.provisional = False
                    self.endpoints[real_id] = ep
                    self.by_addr[addr] = real_id
                    return ep
                self.endpoints[current] = ep
            return None

    def register(self, ident: str, addr: tuple[str, int], kind: str) -> Endpoint:
        with self.lock:
            ep = self.endpoints.get(ident)
            if ep is None:
                ep = Endpoint(ident, addr, kind)
                self.endpoints[ident] = ep
                LOG.info("registered %s %s at %s:%s", kind, ident, *addr)
                self.events.add("sys", f"{addr[0]}:{addr[1]}", None, b"",
                                note=f"registered {kind} {ident}")
            elif ep.addr != addr:
                LOG.info("%s %s moved %s -> %s", kind, ident, ep.addr, addr)
                self.by_addr.pop(ep.addr, None)
                ep.addr = addr
            ep.last_seen = time.time()
            ep.rx += 1
            self.by_addr[addr] = ident
            return ep

    def attach_local_client(self, client) -> None:
        """Register a client living inside this process, i.e. the status poller.

        It cannot have its own socket. Against real hardware the module answers
        on port 10022 regardless of which port the request came from, so replies
        to the poller arrive here, on the relay's socket, not on the poller's.
        """
        self.local_client = client

    def spa(self) -> Optional[Endpoint]:
        with self.lock:
            for ep in self.endpoints.values():
                if ep.is_spa:
                    return ep
        return None

    def spa_online(self) -> bool:
        ep = self.spa()
        return bool(ep and time.time() - ep.last_seen < PING_STALE_AFTER)

    # -- transmit ----------------------------------------------------------

    def send(self, addr: tuple[str, int], payload: bytes, note: str = "") -> None:
        try:
            self.sock.sendto(payload, addr)
        except OSError as exc:
            LOG.warning("send to %s failed: %s", addr, exc)
            return
        self.packets_out += 1
        msg = P.parse(payload)
        LOG.debug("TX -> %s:%s %s", addr[0], addr[1], msg.describe() if msg else payload[:40])
        self.events.add("out", f"{addr[0]}:{addr[1]}", msg, payload, note)
        with self.lock:
            ident = self.by_addr.get(addr)
            if ident and ident in self.endpoints:
                self.endpoints[ident].tx += 1

    # -- main loop ---------------------------------------------------------

    def serve_forever(self) -> None:
        LOG.info("relay listening on %s:%s", *self.sock.getsockname())
        while self.running:
            try:
                payload, addr = self.sock.recvfrom(8192)
            except socket.timeout:
                continue
            except OSError:
                break
            self.packets_in += 1
            try:
                self.handle(payload, addr)
            except Exception:
                LOG.exception("error handling datagram from %s", addr)

    def handle(self, payload: bytes, addr: tuple[str, int]) -> None:
        msg = P.parse(payload)
        peer = f"{addr[0]}:{addr[1]}"
        if msg is None:
            self.packets_dropped += 1
            LOG.warning("unrecognised datagram from %s: %r", peer, payload[:60])
            self.events.add("in", peer, None, payload, note="unrecognised")
            return

        LOG.debug("RX <- %s %s", peer, msg.describe())
        self.events.add("in", peer, msg, payload)

        if msg.kind == "PINGS":
            self.on_pings(msg, addr)
        elif msg.kind == "ISTHR":
            self.on_isthr(msg, addr)
        elif msg.kind == "BONJR":
            self.on_bonjr(msg, addr)
        elif msg.kind == "HELLO":
            self.on_hello(msg, addr)
        elif msg.kind == "PACKT":
            self.on_packt(msg, addr, payload)
        else:
            LOG.info("no handler for <%s> from %s", msg.kind, peer)

    # -- handlers ----------------------------------------------------------

    def on_pings(self, msg: P.Message, addr) -> None:
        """The EN keepalive. Answering this is what keeps its LED blue."""
        if msg.value == b"1":
            self.register_ping_source(addr)
            self.send(addr, P.build_tag("PINGS", "0"), note="keepalive ack")

    def on_isthr(self, msg: P.Message, addr) -> None:
        """A client asking whether a given spa is reachable."""
        spa_id = msg.value.decode("latin1")
        with self.lock:
            if spa_id not in self.endpoints:
                # A module that has only ever pinged is anonymous. The client
                # knows the name, so use it to complete the identification.
                ep = self.spa()
                if ep is not None and ep.provisional:
                    self.adopt_identity(spa_id, ep.addr)
                else:
                    LOG.info("client asked for unknown spa %s", spa_id)
            self.pending_binding[addr] = spa_id
        self.send(addr, P.build_tag("BONJR", "0"), note=f"isthr {spa_id}")

    def on_bonjr(self, msg: P.Message, addr) -> None:
        """A client announcing its own id."""
        ident = msg.value.decode("latin1")
        if ident and ident != "0":
            self.register(ident, addr, "client")
            with self.lock:
                spa_id = self.pending_binding.pop(addr, None)
                if spa_id:
                    self.bindings[ident] = spa_id

    def on_hello(self, msg: P.Message, addr) -> None:
        """LAN discovery. Not normally seen by the cloud, answered for convenience."""
        if msg.value == b"1":
            ep = self.spa()
            if ep:
                self.send(addr, P.build_tag("HELLO", f"{ep.ident}|Spa"),
                          note="discovery reply")
        elif self.local_client is not None and msg.value.startswith(b"SPA"):
            # A discovery answer, which lands here because the module replies to
            # port 10022 rather than to whoever asked.
            self.local_client.deliver(msg, addr)

    def on_packt(self, msg: P.Message, addr, payload: bytes) -> None:
        if msg.src.startswith("SPA"):
            # The module has named itself; upgrade any provisional entry.
            if not self.adopt_identity(msg.src, addr):
                self.register(msg.src, addr, "spa")
        elif msg.src:
            self.register(msg.src, addr, "client")

        if (self.local_client is not None and msg.src.startswith("SPA")
                and not msg.dst):
            # The module answers a LAN-dialect request without naming the
            # recipient, so an unaddressed reply from the spa belongs to the
            # only LAN-dialect client here: the poller. App traffic coming
            # through the relay is addressed by DESCN and never lands here.
            self.local_client.deliver(msg, addr)
            return

        target = self.resolve_target(msg)
        if target is None:
            self.packets_dropped += 1
            if not msg.dst:
                LOG.warning("cannot route %s from %s (no dst)", msg.verb, msg.src)
                self.events.add("sys", f"{addr[0]}:{addr[1]}", msg, payload,
                                note="dropped, no route")
                return
            # A module holds its client sessions indefinitely, so one that was
            # paired through Gecko's real cloud keeps pinging that client
            # through us, and retries hard when nothing answers. There is
            # nothing to deliver it to and nothing to be done until that client
            # connects. Count it, say so once, and keep it out of the wire log,
            # which it would otherwise fill entirely.
            n = self.unroutable.get(msg.dst, 0) + 1
            self.unroutable[msg.dst] = n
            if n == 1:
                LOG.info("traffic for unknown client %s, dropping until it connects",
                         msg.dst)
            if n <= UNROUTABLE_LOG_FIRST or n % UNROUTABLE_LOG_EVERY == 0:
                self.events.add("sys", f"{addr[0]}:{addr[1]}", msg, payload,
                                note=f"dropped, no route (x{n})")
            return
        if msg.verb not in ("APING", "PACKS"):
            LOG.info("relay %-5s %s -> %s : %s", msg.verb,
                     msg.src or "?", target.ident, msg.describe())
        # Forward the original bytes untouched. The relay is a reflector, not a
        # participant, so anything we rewrite is a chance to break the session.
        self.send(target.addr, payload, note=f"relayed from {msg.src or '?'}")

    def resolve_target(self, msg: P.Message) -> Optional[Endpoint]:
        with self.lock:
            if msg.dst:
                return self.endpoints.get(msg.dst)
            # No DESCN means client -> spa. Use the binding set up by ISTHR,
            # falling back to the only spa we know about.
            bound = self.bindings.get(msg.src)
            if bound and bound in self.endpoints:
                return self.endpoints[bound]
            if not msg.src.startswith("SPA"):
                return self.spa()
        return None

    # -- introspection -----------------------------------------------------

    def status(self) -> dict:
        ep = self.spa()
        return {
            "uptime_s": round(time.time() - self.started, 1),
            "listen": "%s:%s" % self.sock.getsockname(),
            "spa_online": self.spa_online(),
            "spa_id": ep.ident if ep else None,
            "spa_addr": f"{ep.addr[0]}:{ep.addr[1]}" if ep else None,
            "spa_last_ping_s": round(time.time() - ep.last_seen, 1) if ep else None,
            "packets_in": self.packets_in,
            "packets_out": self.packets_out,
            "packets_dropped": self.packets_dropped,
            "unroutable": dict(self.unroutable),
            "endpoints": [e.as_dict() for e in self.endpoints.values()],
            "bindings": dict(self.bindings),
        }

    def close(self) -> None:
        self.running = False
        try:
            self.sock.close()
        except OSError:
            pass
