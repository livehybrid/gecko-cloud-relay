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

It has no socket of its own, and cannot have one. A real module answers to port
10022 whatever port the request came from, and answers a LAN-dialect request
without naming the recipient, so replies arrive on the relay's socket with an
empty ``DESCN``. The relay hands those to ``deliver``. An earlier version of
this polled on its own socket and, against real hardware, never heard anything
back at all.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Optional

from . import protocol as P

LOG = logging.getLogger("gecko.poller")

PING_INTERVAL = 2.0
REFRESH_INTERVAL = 300.0
HANDSHAKE_RETRY = 5.0
HELLO_RETRY = 2.0
# If the module never answers discovery, stop waiting and talk to it the way the
# original capture showed, with no DESCN. Better a degraded dashboard than none.
DISCOVERY_GIVE_UP = 30.0


class StatusPoller:
    def __init__(self, relay, client_id: Optional[str] = None,
                 enabled: bool = True, spa_id: Optional[str] = None):
        self.relay = relay
        self.client_id = client_id or f"GRLY{uuid.uuid4()}"
        self.enabled = enabled
        self.spa_id = spa_id or ""          # DESCN; learned by discovery if unset
        self.pinned_spa_id = bool(spa_id)
        self.status = P.SpaStatus()
        self.seq = 0x20
        self.running = True
        self.connected = False
        self.discovered = bool(spa_id)
        self.last_full_refresh = 0.0
        self.last_ping = 0.0
        self.last_update = 0.0
        self.pack_files = ""
        self.errors = 0
        self._target: Optional[tuple[str, int]] = None
        self._candidates: list[tuple[str, int]] = []
        self._cand_i = 0
        self._announced = False
        self._last_hello = 0.0
        self._discovery_started = 0.0
        self._lock = threading.RLock()

    def next_seq(self) -> int:
        self.seq = (self.seq + 1) & 0xFF
        return self.seq

    # -- transmit ----------------------------------------------------------

    def send_raw(self, payload: bytes, note: str = "poller") -> None:
        if not self._target:
            return
        self.relay.send(self._target, payload, note=note)

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
            # Replies arrive on the relay's thread via deliver(), so this loop
            # only has to keep time.
            time.sleep(0.2)

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
            self.send_raw(P.build_tag("HELLO", self.client_id), note="poller announce")

        if not self.connected:
            if now - self.last_ping > HANDSHAKE_RETRY:
                self.last_ping = now
                LOG.info("poller starting handshake with %s", self.spa_id or "spa")
                self.send(b"SFILE" + bytes([self.next_seq()]))
                self.send(b"AVERS" + bytes([self.next_seq()]))
                self.request_full_block()
        else:
            if now - self.last_ping >= PING_INTERVAL:
                self.last_ping = now
                self.send(b"APING")
            if now - self.last_full_refresh >= REFRESH_INTERVAL:
                self.request_full_block()

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

    def deliver(self, msg: P.Message, addr: tuple[str, int]) -> None:
        """Called from the relay's thread with a message meant for us."""
        if not self.enabled or not self.running:
            return
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
            "last_update_s": (round(time.time() - self.last_update, 1)
                              if self.last_update else None),
        })
        return d

    def stop(self) -> None:
        self.running = False
