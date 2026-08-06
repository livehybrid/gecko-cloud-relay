"""Optional status poller.

The relay alone is blind: it forwards bytes but never learns what the spa is
doing. This joins the conversation as an ordinary client so the dashboard can
show real temperatures and pump states.

It behaves like the app does:

  1. ``<HELLO>1</HELLO>`` to discover the module's id, unless one was pinned
  2. ``<HELLO>{client_id}</HELLO>`` to announce itself
  3. pull the pack file list, the version and the full status block
  4. ``APING`` every 2 seconds

Every step after discovery names the spa in ``DESCN``. Both of those matter:
the module ignores clients it has not been introduced to, and geckolib's
captures show the real app always addressing the spa explicitly.

The 2 second ping is not arbitrary either. If the ping lapses, the module stops
sending change notifications to that client.

Sending and receiving are deliberately asymmetric, and both halves were settled
by watching a real module rather than by reasoning:

  * It **sends from its own ephemeral socket.** A module will not serve the
    status block to anything asking from port 10022; it answers SFILE and AVERS
    but silently ignores STATU, presumably because that port is the cloud and
    the cloud is not a client.
  * It **receives through the relay.** The module replies to port 10022 whatever
    port asked, and answers a LAN-dialect client without naming it, so the
    replies land on the relay's socket with an empty ``DESCN``. The relay hands
    those here via ``deliver``.

Get either half wrong and the symptom is identical and silent: a dashboard that
never fills in. Its own socket is drained too, in case a firmware answers the
sender directly; the message handlers are idempotent, so either path is safe.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
import uuid
from typing import Optional

from . import protocol as P

LOG = logging.getLogger("gecko.poller")

PING_INTERVAL = 2.0
REFRESH_INTERVAL = 300.0
HANDSHAKE_RETRY = 5.0
HANDSHAKE_RETRY_MAX = 60.0     # back off rather than hammer a module that is not answering
HELLO_RETRY = 2.0
# If the module never answers discovery, stop waiting and talk to it the way the
# original capture showed, with no DESCN. Better a degraded dashboard than none.
DISCOVERY_GIVE_UP = 30.0


class StatusPoller:
    def __init__(self, relay, client_id: Optional[str] = None,
                 enabled: bool = True, spa_id: Optional[str] = None):
        self.relay = relay
        # A module remembers its clients indefinitely: ours is still pinging a
        # phone session it was given by Gecko's cloud long before this existed.
        # So do not invent a fresh identity on every restart. Derive a stable
        # one from the spa once it is known, and until then use a placeholder.
        self.pinned_client_id = bool(client_id)
        self.client_id = client_id or f"GRLY{uuid.uuid4()}"
        self.enabled = enabled
        self.spa_id = spa_id or ""          # DESCN; learned by discovery if unset
        self.pinned_spa_id = bool(spa_id)
        self.status = P.SpaStatus()
        # Send-side only. Requests must not come from port 10022, or the module
        # will not answer STATU. Replies come back through the relay.
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.sock.settimeout(0.3)
        self.seq = 0x20
        self.running = True
        self.connected = False
        self.discovered = bool(spa_id)
        self.last_full_refresh = 0.0
        self.last_ping = 0.0
        self.last_update = 0.0
        self.pack_files = ""
        self.errors = 0
        # Which path replies actually took. Against real hardware this should be
        # all relay and no direct; if that ever flips, the asymmetry above is
        # wrong for that firmware and the dashboard is running on luck.
        self.via_relay = 0
        self.via_socket = 0
        self._target: Optional[tuple[str, int]] = None
        self._candidates: list[tuple[str, int]] = []
        self._cand_i = 0
        self._announced = False
        self._last_hello = 0.0
        self._discovery_started = 0.0
        self._handshake_backoff = HANDSHAKE_RETRY
        self._lock = threading.RLock()

    def next_seq(self) -> int:
        self.seq = (self.seq + 1) & 0xFF
        return self.seq

    # -- transmit ----------------------------------------------------------

    def send_raw(self, payload: bytes, note: str = "poller") -> None:
        if not self._target:
            return
        try:
            self.sock.sendto(payload, self._target)
        except OSError as exc:
            LOG.warning("poller send failed: %s", exc)
            return
        self.relay.events.add("out", f"{self._target[0]}:{self._target[1]}",
                              P.parse(payload), payload, note=note)

    def send(self, data: bytes) -> None:
        """Send a PACKT addressed to the spa."""
        self.send_raw(P.build_packt(data, self.client_id, self.spa_id))

    # -- lifecycle ---------------------------------------------------------

    def run(self) -> None:
        if not self.enabled:
            LOG.info("status poller disabled")
            return
        LOG.info("status poller client id %s", self.client_id)
        self.relay.attach_local_client(self)
        while self.running:
            ep = self.relay.spa()
            if ep is None or not self.relay.spa_online():
                self._reset()
                time.sleep(1.0)
                continue
            self._retarget(ep)
            try:
                self.pump()
            except Exception:
                self.errors += 1
                LOG.exception("poller error")
                time.sleep(2.0)
            # Most replies arrive on the relay's thread via deliver(). Draining
            # our own socket covers the other case and paces this loop.
            self.receive()

    def _reset(self) -> None:
        self.connected = False
        self._announced = False
        self._target = None
        self._candidates = []

    def _retarget(self, ep) -> None:
        """Work out where to send. The module's cloud socket is normally also
        its LAN socket, but fall back to the well known port if it is not."""
        if self._candidates and ep.addr in self._candidates:
            return
        self._candidates = [ep.addr]
        if ep.addr[1] != P.PORT:
            self._candidates.append((ep.addr[0], P.PORT))
        self._cand_i = 0
        self._target = self._candidates[0]
        self.connected = False
        self._announced = False
        self._discovery_started = time.time()
        self._handshake_backoff = HANDSHAKE_RETRY
        LOG.info("poller targeting spa at %s:%s", *self._target)

    def pump(self) -> None:
        now = time.time()

        if not self.discovered:
            if now - self._last_hello >= HELLO_RETRY:
                self._last_hello = now
                if self._discovery_started and now - self._discovery_started > DISCOVERY_GIVE_UP:
                    self._give_up_on_discovery()
                else:
                    # Rotate candidates so a module whose cloud socket is not
                    # its LAN socket still gets found.
                    self._target = self._candidates[self._cand_i % len(self._candidates)]
                    self._cand_i += 1
                    self.send_raw(P.build_tag("HELLO", "1"), note="poller discovery")
            return

        if not self._announced:
            self._announced = True
            self._settle_client_id()
            self.send_raw(P.build_tag("HELLO", self.client_id), note="poller announce")

        if not self.connected:
            if now - self.last_ping > self._handshake_backoff:
                self.last_ping = now
                LOG.info("poller starting handshake with %s (retry in %.0fs if quiet)",
                         self.spa_id or "spa", self._handshake_backoff)
                self.send(b"SFILE" + bytes([self.next_seq()]))
                self.send(b"AVERS" + bytes([self.next_seq()]))
                self.request_full_block()
                self._handshake_backoff = min(self._handshake_backoff * 1.5,
                                              HANDSHAKE_RETRY_MAX)
        else:
            if now - self.last_ping >= PING_INTERVAL:
                self.last_ping = now
                self.send(b"APING")
            if now - self.last_full_refresh >= REFRESH_INTERVAL:
                self.request_full_block()

    def _settle_client_id(self) -> None:
        """Adopt a client id that will be the same after a restart.

        Derived from the spa so that two relays pointed at different spas do not
        collide, and so redeploying does not leave another dead session behind
        on a module that never forgets one.
        """
        if self.pinned_client_id or not self.spa_id:
            return
        stable = "GRLY" + str(uuid.uuid5(uuid.NAMESPACE_DNS,
                                         "gecko-cloud-relay|" + self.spa_id))
        if stable != self.client_id:
            LOG.info("poller client id %s (stable, derived from the spa id)", stable)
            self.client_id = stable

    def _give_up_on_discovery(self) -> None:
        ep = self.relay.spa()
        if ep is not None and not ep.provisional:
            self.spa_id = ep.ident
            LOG.warning("no answer to discovery; using the id the relay learned (%s)",
                        self.spa_id)
        else:
            self.spa_id = ""
            LOG.warning("no answer to discovery and no known spa id; "
                        "talking to the module without DESCN")
        self._target = self._candidates[0]
        self.discovered = True

    def request_full_block(self) -> None:
        self.last_full_refresh = time.time()
        self.send(b"STATU" + bytes([self.next_seq()])
                  + (0).to_bytes(2, "big") + P.BLOCK_LEN.to_bytes(2, "big"))

    def receive(self) -> None:
        """Drain our own socket. Normally empty: the module answers the relay."""
        deadline = time.time() + 0.3
        while time.time() < deadline:
            try:
                payload, addr = self.sock.recvfrom(8192)
            except (socket.timeout, OSError):
                return
            msg = P.parse(payload)
            if msg is None:
                continue
            self.relay.events.add("in", f"{addr[0]}:{addr[1]}", msg, payload,
                                  note="poller direct")
            self.deliver(msg, addr, direct=True)

    def deliver(self, msg: P.Message, addr: tuple[str, int],
                direct: bool = False) -> None:
        """Handle a message meant for us, from the relay's thread or our own."""
        if not self.enabled or not self.running:
            return
        # Only PACKT traffic is counted. A discovery answer legitimately comes
        # straight back to the asking socket, since a client broadcasting HELLO
        # is not registered anywhere yet.
        if msg.kind == "PACKT":
            if direct:
                self.via_socket += 1
            else:
                self.via_relay += 1
        try:
            self.on_message(msg, addr)
        except Exception:
            self.errors += 1
            LOG.exception("poller error handling %s", msg.verb or msg.kind)

    def on_message(self, msg: P.Message, addr: tuple[str, int]) -> None:
        if msg.kind == "HELLO":
            return self.on_hello(msg, addr)
        if msg.kind != "PACKT":
            return
        body = msg.body
        with self._lock:
            if msg.verb == "STATV" and len(body) >= 3:
                index, _nxt, ln = body[0], body[1], body[2]
                self.status.apply_block(index, body[3:3 + ln])
                self.last_update = time.time()
                if self.status.complete and not self.connected:
                    self.connected = True
                    self._handshake_backoff = HANDSHAKE_RETRY
                    LOG.info("poller has a complete status block")
            elif msg.verb == "STATP":
                changed = self.status.apply_statp(body)
                self.last_update = time.time()
                if changed:
                    LOG.info("spa change: %s", msg.describe())
                # Re-arm the notification, as the app does.
                self.send(b"STATQ" + bytes([self.next_seq()]))
            elif msg.verb == "FILES":
                self.pack_files = body.decode("latin1", "replace").lstrip(",")
                LOG.info("spa pack files: %s", self.pack_files)

    def on_hello(self, msg: P.Message, addr: tuple[str, int]) -> None:
        """Discovery reply: ``<HELLO>SPAxx:xx:...|Name</HELLO>``."""
        value = msg.value.decode("latin1")
        ident = value.split("|", 1)[0]
        if not ident or ident == "1" or ident == self.client_id:
            return
        if not self.pinned_spa_id:
            self.spa_id = ident
        self.discovered = True
        self._target = addr
        LOG.info("discovered spa %s at %s:%s", ident, *addr)
        # Tell the relay too. A module that has only ever sent keepalives is
        # anonymous, and this is the cheapest way to put a name to it.
        self.relay.adopt_identity(ident, addr)

    def as_dict(self) -> dict:
        with self._lock:
            d = self.status.as_dict()
        d.update({
            "enabled": self.enabled,
            "connected": self.connected,
            "discovered": self.discovered,
            "client_id": self.client_id,
            "spa_id": self.spa_id,
            "pack_files": self.pack_files,
            "errors": self.errors,
            "via_relay": self.via_relay,
            "via_socket": self.via_socket,
            "last_update_s": (round(time.time() - self.last_update, 1)
                              if self.last_update else None),
        })
        return d

    def stop(self) -> None:
        self.running = False
        try:
            self.sock.close()
        except OSError:
            pass
