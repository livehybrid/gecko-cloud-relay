"""Optional status poller.

The relay alone is blind: it forwards bytes but never learns what the spa is
doing. This joins the conversation as an ordinary client so the dashboard can
show real temperatures and pump states.

The handshake is a **conversation, not a volley**. That distinction cost a day.
Firing the requests off together gets you answers to some of them and silence
on ``STATU``, which reads exactly like a module that has decided not to serve
the status block to this client. It has not decided anything of the sort. It
wants to be asked one thing at a time:

  1. ``<HELLO>1</HELLO>`` to discover the module's id, unless one was pinned
  2. ``AVERS``  -> ``SVERS``   firmware versions
  3. ``CURCH``  -> ``CHCUR``   RF channel and signal strength
  4. ``SFILE``  -> ``FILES``   which pack this is
  5. ``STATU``  -> ``STATV``   the status block, in 39-byte chunks
  6. ``STATQ``                 arm change notifications, then ``APING`` every 2s

Each step waits for its own answer before the next goes out. Skipping the
channel step, or asking for the block before the config file has come back,
loses the block silently.

Sequence numbers are two separate spaces, which is not guessable and is not
optional: **1-191 for protocol requests, 192-255 for pack commands**. A
``STATU`` numbered like a keypress is ignored without complaint. Both facts are
straight out of geckolib, which is the same protocol talking to the same module
from Home Assistant, and is the reference whenever this file and the hardware
disagree.

Every step after discovery names the spa in ``DESCN``. The module ignores
clients it has not been introduced to, and captures show the real app always
addressing the spa explicitly.

The 2 second ping is not arbitrary either. If the ping lapses, the module stops
sending change notifications to that client.

There is no announce here and no socket of its own. Requests go out through the
relay on port 10022 with ``SRCCN``/``DESCN`` set, byte for byte what the relay
forwards on behalf of the app, and the relay hands the replies back via
``deliver``. Unaddressed replies are still accepted, since a module that does
answer LAN-style should not leave the dashboard empty.

``snoop`` remains as a belt-and-braces path: anything the module sends to the
phone app crosses the relay anyway, and reading it costs nothing.
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
# Change notifications carry the traffic; this is the backstop that catches a
# notification we missed and re-reads anything the module never told us about.
REFRESH_INTERVAL = 60.0
STEP_TIMEOUT = 4.0             # geckolib's PROTOCOL_TIMEOUT_IN_SECONDS
STEP_RETRIES = 4               # per step, before restarting the handshake
STEP_PAUSE = 0.3               # geckolib pauses between steps; so do we
HANDSHAKE_RETRY = 5.0
HANDSHAKE_RETRY_MAX = 60.0     # back off rather than hammer a module that is not answering
HELLO_RETRY = 2.0

# Sequence numbers live in two separate spaces and the module cares. A status
# request numbered like a pack command is dropped in silence.
SEQ_PROTOCOL = (1, 191)
SEQ_COMMAND = (192, 255)

# geckolib asks for 1024 bytes, comfortably past the end of a 637-byte block,
# and the module simply stops when it runs out. Asking for exactly BLOCK_LEN
# also works, but there is no reason to differ from the client we know works.
BLOCK_REQUEST_LEN = 1024

# The order the module wants to be asked in.
NEXT_STEP = {"version": "channel", "channel": "config", "config": "block"}
# Steps worth asking for but not worth stalling on. The live module answers
# AVERS and CURCH immediately and has never once answered SFILE, and since the
# status block offsets here are fixed rather than read out of the pack file,
# the answer would not change anything we do.
OPTIONAL_STEPS = {"config"}
# If the module never answers discovery, stop waiting and talk to it the way the
# original capture showed, with no DESCN. Better a degraded dashboard than none.
DISCOVERY_GIVE_UP = 30.0

# Buttons, by the name the dashboard uses. These are the same key codes the
# physical topside panel sends, so a press here is indistinguishable from
# someone leaning over and pressing it.
BUTTONS = {
    "pump1": 0x01,
    "pump2": 0x02,
    "pump3": 0x03,
    "aux": 0x06,
    "light": 0x10,
    "tempup": 0x15,
    "tempdown": 0x16,
}

# A set point write goes straight into the pack's status block, so refuse
# anything absurd rather than find out what the pack does with it. A domestic
# spa lives at the top of this range; the floor is here for freeze protection
# and winterising, not because anyone bathes at 15 degrees.
SETPOINT_MIN_C = 15.0
SETPOINT_MAX_C = 40.0


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
        self.seq = SEQ_PROTOCOL[0] - 1
        self.cmd_seq = SEQ_COMMAND[0] - 1
        self.running = True
        self.connected = False
        # A module may push change notifications while still declining to dump
        # the whole block. That is a working session, so treat it as usable
        # rather than waiting forever for a baseline that is not coming.
        self.live = False
        self.discovered = bool(spa_id)
        self.last_full_refresh = 0.0
        self.last_ping = 0.0
        self.last_update = 0.0
        self.pack_files = ""
        self.versions = ""
        self.channel: Optional[int] = None
        self.signal: Optional[int] = None
        self.errors = 0
        # Where the handshake has got to, so a stall is legible in the
        # dashboard and the log rather than being an unexplained silence.
        self.step = "version"
        self._step_sent = 0.0
        self._step_tries = 0
        # Which path replies actually took. Against real hardware this should be
        # all relay and no direct; if that ever flips, the asymmetry above is
        # wrong for that firmware and the dashboard is running on luck.
        self.via_relay = 0
        self.via_socket = 0
        self.snooped = 0
        self._target: Optional[tuple[str, int]] = None
        self._candidates: list[tuple[str, int]] = []
        self._cand_i = 0
        self._announced = False
        self._last_hello = 0.0
        self._discovery_started = 0.0
        self._handshake_backoff = HANDSHAKE_RETRY
        self._lock = threading.RLock()

    def next_seq(self) -> int:
        """Sequence for a protocol request: AVERS, CURCH, SFILE, STATU, STATQ."""
        lo, hi = SEQ_PROTOCOL
        self.seq = lo if self.seq >= hi else self.seq + 1
        return self.seq

    def next_cmd_seq(self) -> int:
        """Sequence for a pack command: SPACK. A separate space, see the header."""
        lo, hi = SEQ_COMMAND
        self.cmd_seq = lo if self.cmd_seq >= hi else self.cmd_seq + 1
        return self.cmd_seq

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
        self.live = False
        self._begin_handshake()
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
        self._begin_handshake()
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

        if not self.connected:
            self._advance_handshake(now)
        else:
            if now - self.last_ping >= PING_INTERVAL:
                self.last_ping = now
                self.send(b"APING")
            if now - self.last_full_refresh >= REFRESH_INTERVAL:
                LOG.debug("periodic status refresh")
                self.request_full_block()

    # -- handshake ---------------------------------------------------------

    def _send_step(self, step: str) -> None:
        if step == "version":
            self.send(b"AVERS" + bytes([self.next_seq()]))
        elif step == "channel":
            self.send(b"CURCH" + bytes([self.next_seq()]))
        elif step == "config":
            self.send(b"SFILE" + bytes([self.next_seq()]))
        elif step == "block":
            self.request_full_block()

    def _advance_handshake(self, now: float) -> None:
        """One step at a time, waiting for each answer.

        The previous version sent every request in one go and then reported the
        module as refusing to answer. It was not refusing. It was being asked
        four things at once by a client it had not finished admitting.
        """
        if now - self._step_sent < STEP_TIMEOUT:
            return

        if self._step_tries >= STEP_RETRIES:
            if self.step in OPTIONAL_STEPS:
                # Asked for, not answered, not worth stalling over. This module
                # never answers SFILE for us, and the pack it would name is one
                # we do not use: the offsets in protocol.py are fixed. Carrying
                # on is how we find out whether the block actually depended on
                # it or whether it was only ever part of the etiquette.
                LOG.info("no answer to %s after %d tries; carrying on without it",
                         self.step, self._step_tries)
                self._step_done(self.step, NEXT_STEP[self.step])
                return
            # A step that matters is genuinely not being answered. Start the
            # whole conversation again rather than pressing on out of order, and
            # back off so a module that is busy or asleep is not hammered.
            LOG.warning("no answer to %s after %d tries; restarting the handshake "
                        "(next attempt in %.0fs)",
                        self.step, self._step_tries, self._handshake_backoff)
            self._begin_handshake(now + self._handshake_backoff)
            self._handshake_backoff = min(self._handshake_backoff * 1.5,
                                          HANDSHAKE_RETRY_MAX)
            return

        if self._step_tries == 0:
            LOG.info("handshake with %s: %s", self.spa_id or "spa", self.step)
        self._step_sent = now
        self._step_tries += 1
        self._send_step(self.step)

    def _begin_handshake(self, at: float = 0.0) -> None:
        self.step = "version"
        self._step_tries = 0
        # Setting this in the future is how a restart waits out its backoff.
        self._step_sent = at - STEP_TIMEOUT if at else 0.0

    def _step_done(self, step: str, nxt: str) -> None:
        """An expected answer arrived. Move on, after a short breath."""
        if self.connected or self.step != step:
            return                      # a late duplicate, or we have moved on
        self.step = nxt
        self._step_tries = 0
        self._handshake_backoff = HANDSHAKE_RETRY
        # Pause before the next request, as geckolib does between steps.
        self._step_sent = time.time() - STEP_TIMEOUT + STEP_PAUSE

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

    # -- control -----------------------------------------------------------
    #
    # Writes, unlike everything else here. The spa answers with a STATP, so the
    # dashboard catches up on its own within a second and there is no need to
    # guess at the new state locally.

    def ready(self) -> Optional[str]:
        """Why a command cannot be sent right now, or None if it can."""
        if not self.enabled:
            return "status poller is disabled (--no-poll)"
        if not self.relay.spa_online():
            return "spa is not online"
        if not self._target or not self.discovered:
            return "still working out where the spa is"
        if not (self.connected or self.live):
            return "the spa has not accepted us as a client yet"
        return None

    def press(self, button: str) -> None:
        """Send a keypress, exactly as the topside panel does."""
        key = BUTTONS[button]
        LOG.info("keypress %s (%s) from the dashboard", button,
                 P.KEYPAD.get(key, hex(key)))
        self.send(b"SPACK" + bytes([self.next_cmd_seq(), 0x06, 0x02, P.CMD_KEYPRESS, key]))

    def set_setpoint(self, celsius: float) -> float:
        """Write the water set point. Returns the value actually sent."""
        c = max(SETPOINT_MIN_C, min(SETPOINT_MAX_C, float(celsius)))
        raw = P.c_to_raw(c)
        LOG.info("set point -> %.1f C (raw %d) from the dashboard", c, raw)
        self.send(b"SPACK" + bytes([self.next_cmd_seq(), 0x06, 0x07, P.CMD_SETVALUE,
                                    0x06, 0x06])
                  + P.OFF_SETPOINT.to_bytes(2, "big") + raw.to_bytes(2, "big"))
        return c

    def nudge_setpoint(self, delta_c: float) -> float:
        with self._lock:
            current = P.raw_to_c(self.status.word(P.OFF_SETPOINT))
        return self.set_setpoint(current + delta_c)

    def request_full_block(self) -> None:
        self.last_full_refresh = time.time()
        self.send(b"STATU" + bytes([self.next_seq()])
                  + (0).to_bytes(2, "big") + BLOCK_REQUEST_LEN.to_bytes(2, "big"))

    def snoop(self, msg: P.Message) -> None:
        """Learn from a status block the module sent to somebody else.

        This module answers the phone app's request for the block and ignores
        the identical one from us, which is its prerogative. Every byte of the
        answer crosses the relay on its way to the app, though, and reading it
        in passing costs nothing and sends nothing. Being in the middle is the
        one advantage this has over being a client.

        Strictly read-only: no re-arm, no reply, no counters that would suggest
        the module is talking to us when it is not.
        """
        if not self.enabled or not self.running or msg.kind != "PACKT":
            return
        body = msg.body
        with self._lock:
            if msg.verb == "STATV" and len(body) >= 3:
                index, _nxt, ln = body[0], body[1], body[2]
                self.status.apply_block(index, body[3:3 + ln])
            elif msg.verb == "STATP":
                if not self.status.apply_statp(body):
                    return
            else:
                return
            self.snooped += 1
            self.last_update = time.time()
            if self.status.complete and not self.connected:
                self.connected = True
                LOG.info("status block completed from traffic to another client")

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
                # Chunks arrive one datagram each, so hold the step open while
                # they are still coming rather than timing out mid-block.
                self._step_tries = 0
                self._step_sent = time.time()
                if self.status.complete and not self.connected:
                    self.connected = True
                    self.step = "watching"
                    self._handshake_backoff = HANDSHAKE_RETRY
                    LOG.info("poller has a complete status block")
                    # Arm change notifications now the baseline is in hand.
                    self.send(b"STATQ" + bytes([self.next_seq()]))
            elif msg.verb == "STATP":
                self.live = True
                changed = self.status.apply_statp(body)
                self.last_update = time.time()
                if changed:
                    LOG.info("spa change: %s", msg.describe())
                # Re-arm the notification, as the app does.
                self.send(b"STATQ" + bytes([self.next_seq()]))
            elif msg.verb == "FILES":
                self.pack_files = body.decode("latin1", "replace").lstrip(",")
                LOG.info("spa pack files: %s", self.pack_files)
                self._step_done("config", NEXT_STEP["config"])
            elif msg.verb == "SVERS":
                self.versions = body.hex()
                self._step_done("version", NEXT_STEP["version"])
            elif msg.verb == "CHCUR" and len(body) >= 2:
                self.channel, self.signal = body[0], body[1]
                LOG.info("spa RF channel %s, signal %s", self.channel, self.signal)
                self._step_done("channel", NEXT_STEP["channel"])

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
            "live": self.live,
            "controllable": self.ready() is None,
            "discovered": self.discovered,
            "client_id": self.client_id,
            "spa_id": self.spa_id,
            "pack_files": self.pack_files,
            "step": self.step,
            "rf_channel": self.channel,
            "rf_signal": self.signal,
            "errors": self.errors,
            "via_relay": self.via_relay,
            "snooped": self.snooped,
            "via_socket": self.via_socket,
            "last_update_s": (round(time.time() - self.last_update, 1)
                              if self.last_update else None),
        })
        return d

    def stop(self) -> None:
        self.running = False
