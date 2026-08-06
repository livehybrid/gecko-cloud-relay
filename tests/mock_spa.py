"""Emulates an in.touch2 EN module well enough to exercise the relay.

Covers the parts of the module's behaviour the relay actually depends on:

  * the cloud keepalive, ``<PINGS>1</PINGS>`` out to whatever it thinks the
    in.touch 2 server is
  * LAN discovery, ``<HELLO>1</HELLO>`` in, ``<HELLO>{id}|{name}</HELLO>`` out
  * client announcement, ``<HELLO>{client_id}</HELLO>``
  * the handshake verbs ``SFILE`` and ``AVERS``
  * status block reads (``STATU`` -> a run of ``STATV`` chunks)
  * change notification (``STATP``) and its re-arm (``STATQ``)
  * the two ``SPACK`` commands the app sends: keypress and write

The state it holds is a real 637 byte block at the real offsets, so the decode
path in protocol.py is exercised rather than mocked.

Run standalone against a relay:

    python3 tests/mock_spa.py --port 10023 --cloud 127.0.0.1:10022

Or check itself:

    python3 tests/mock_spa.py --selftest
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gecko_relay import protocol as P    # noqa: E402

LOG = logging.getLogger("mock.spa")

SPA_ID = "SPA80:1f:12:50:a5:d1"
SPA_NAME = "Spa"
PACK_FILES = ",inXM_C06.xml,inXM_S06.xml"
VERSION = b"\x01\x2c\x00\x03\x00\x02"

CLOUD_PING_INTERVAL = 21.0      # what a real EN module does
PUMP_RUN_MINUTES = 15
LIGHT_RUN_MINUTES = 30


def default_block() -> bytearray:
    """A plausible idle spa.

    Filled with a deterministic pattern first, so a chunk landing at the wrong
    offset shows up as nonsense rather than as a convincing zero.
    """
    b = bytearray(i & 0xFF for i in range(P.BLOCK_LEN))

    b[P.OFF_LIGHT_TIMEOUT_CFG] = LIGHT_RUN_MINUTES
    b[P.OFF_PUMP_TIMEOUT_CFG] = 20

    def word(off, val):
        b[off:off + 2] = int(val).to_bytes(2, "big")

    word(P.OFF_SETPOINT, P.c_to_raw(36.0))      # 648
    word(P.OFF_WATER_TEMP, P.c_to_raw(35.5))
    word(P.OFF_HEATER_TEMP, P.c_to_raw(35.8))
    word(P.OFF_WATER_TEMP_2, P.c_to_raw(35.5))

    b[P.OFF_PUMP_STATUS] = 0x00
    b[P.OFF_LIGHT] = 0x00
    b[P.OFF_AUX] = 0x00
    b[P.OFF_PUMP_DEMAND] = 0x00
    b[P.OFF_PUMP_TIMER] = 0
    b[P.OFF_LIGHT_TIMER] = 0
    return b


class GeckoMockServer:
    def __init__(self, host: str = "0.0.0.0", port: int = P.PORT,
                 spa_id: str = SPA_ID, name: str = SPA_NAME):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.sock.bind((host, port))
        self.sock.settimeout(0.25)
        self.spa_id = spa_id
        self.name = name
        self.state = P.SpaStatus(block=default_block(),
                                 have=bytearray(b"\x01" * P.BLOCK_LEN))
        self.clients: dict[str, tuple[str, int]] = {}
        self.cloud: tuple[str, int] | None = None
        self.cloud_interval = CLOUD_PING_INTERVAL
        self.cloud_acks = 0
        self.running = True
        self.rx = 0
        self.tx = 0
        self._seq = 0
        self._last_cloud_ping = 0.0
        self._lock = threading.RLock()

    # -- plumbing ----------------------------------------------------------

    def set_cloud(self, addr: tuple[str, int], interval: float = CLOUD_PING_INTERVAL) -> None:
        self.cloud = addr
        self.cloud_interval = interval

    def next_seq(self) -> int:
        self._seq = (self._seq + 1) & 0xFF
        return self._seq

    def send(self, addr: tuple[str, int], payload: bytes) -> None:
        try:
            self.sock.sendto(payload, addr)
            self.tx += 1
        except OSError as exc:
            LOG.warning("mock spa send failed: %s", exc)

    def reply(self, addr: tuple[str, int], data: bytes, dst: str) -> None:
        self.send(addr, P.build_packt(data, self.spa_id, dst))

    def serve_forever(self) -> None:
        LOG.info("mock spa %s listening on %s:%s", self.spa_id, *self.sock.getsockname())
        while self.running:
            self.cloud_tick()
            try:
                payload, addr = self.sock.recvfrom(8192)
            except socket.timeout:
                continue
            except OSError:
                break
            self.rx += 1
            try:
                self.handle(payload, addr)
            except Exception:
                LOG.exception("mock spa error handling %r", payload[:60])
        try:
            self.sock.close()
        except OSError:
            pass

    def cloud_tick(self) -> None:
        if not self.cloud:
            return
        now = time.time()
        if now - self._last_cloud_ping >= self.cloud_interval:
            self._last_cloud_ping = now
            self.send(self.cloud, P.build_tag("PINGS", "1"))

    # -- dispatch ----------------------------------------------------------

    def handle(self, payload: bytes, addr) -> None:
        msg = P.parse(payload)
        if msg is None:
            LOG.debug("mock spa ignoring %r from %s", payload[:40], addr)
            return
        if msg.kind == "PINGS":
            if msg.value == b"0":
                self.cloud_acks += 1          # the server answered our keepalive
            elif msg.value == b"1":
                self.send(addr, P.build_tag("PINGS", "0"))
        elif msg.kind == "HELLO":
            self.on_hello(msg, addr)
        elif msg.kind == "PACKT":
            self.on_packt(msg, addr)

    def on_hello(self, msg: P.Message, addr) -> None:
        value = msg.value.decode("latin1")
        if value == "1":
            self.send(addr, P.build_tag("HELLO", f"{self.spa_id}|{self.name}"))
        elif value and value != self.spa_id:
            self.add_client(value, addr)

    def add_client(self, ident: str, addr) -> None:
        with self._lock:
            if self.clients.get(ident) != addr:
                LOG.info("mock spa client %s at %s:%s", ident, *addr)
            self.clients[ident] = addr

    def on_packt(self, msg: P.Message, addr) -> None:
        if msg.dst and msg.dst != self.spa_id:
            LOG.debug("mock spa ignoring packet addressed to %s", msg.dst)
            return
        if msg.src and not msg.src.startswith("SPA"):
            self.add_client(msg.src, addr)

        body, verb, src = msg.body, msg.verb, msg.src
        if verb == "APING":
            self.reply(addr, b"PACKS", src)
        elif verb == "SFILE":
            self.reply(addr, b"FILES" + PACK_FILES.encode(), src)
        elif verb == "AVERS":
            self.reply(addr, b"SVERS" + VERSION, src)
        elif verb == "STATU":
            start = int.from_bytes(body[1:3], "big") if len(body) >= 5 else 0
            length = int.from_bytes(body[3:5], "big") if len(body) >= 5 else P.BLOCK_LEN
            self.send_status_block(addr, src, start, length)
        elif verb == "STATQ":
            pass                              # notification re-armed
        elif verb == "SPACK":
            self.on_spack(body)

    # -- status block ------------------------------------------------------

    def send_status_block(self, addr, dst: str, start: int, length: int) -> None:
        """Answer a STATU with a run of STATV chunks.

        The client reconstructs by multiplying the chunk index by STATV_CHUNK,
        so chunks have to sit on the chunk grid. Every request seen in the wild
        starts at zero, which does.
        """
        start -= start % P.STATV_CHUNK
        end = min(start + length, P.BLOCK_LEN)
        pos, index = start, start // P.STATV_CHUNK
        with self._lock:
            while pos < end:
                chunk = bytes(self.state.block[pos:min(pos + P.STATV_CHUNK, end)])
                pos += len(chunk)
                nxt = (index + 1) if pos < end else 0
                self.reply(addr, b"STATV" + bytes([index & 0xFF, nxt & 0xFF, len(chunk)])
                           + chunk, dst)
                index += 1

    def write(self, pos: int, data: bytes) -> list[int]:
        """Apply a write, returning the positions that actually changed."""
        changed = []
        with self._lock:
            for i, b in enumerate(data):
                o = pos + i
                if o < P.BLOCK_LEN and self.state.block[o] != b:
                    self.state.block[o] = b
                    changed.append(o)
        return changed

    def notify(self, positions: list[int]) -> None:
        """Push a STATP change notification to every client we know about."""
        if not positions:
            return
        with self._lock:
            entries = b""
            count = 0
            for pos in sorted(set(positions)):
                entries += pos.to_bytes(2, "big") + bytes(self.state.block[pos:pos + 2])
                count += 1
            data = b"STATP" + bytes([count]) + entries
            targets = list(self.clients.items())
        for ident, addr in targets:
            self.reply(addr, data, ident)

    # -- commands ----------------------------------------------------------

    def on_spack(self, body: bytes) -> None:
        if len(body) < 4:
            return
        cmd = body[3]
        if cmd == P.CMD_KEYPRESS and len(body) >= 5:
            self.notify(self.keypress(body[4]))
        elif cmd == P.CMD_SETVALUE and len(body) >= 10:
            pos = int.from_bytes(body[6:8], "big")
            self.notify(self.write(pos, body[8:]))

    def keypress(self, key: int) -> list[int]:
        LOG.info("mock spa keypress %s", P.KEYPAD.get(key, f"0x{key:02x}"))
        if key in (0x01, 0x02, 0x03):
            return self.toggle_pump(key)
        if key == 0x06:
            return self.toggle_field(P.OFF_PUMP_STATUS, 0, 1)
        if key == 0x10:
            return self.toggle_light()
        if key in (0x15, 0x16):
            step = P.c_to_raw(0.5) * (1 if key == 0x15 else -1)
            raw = int.from_bytes(self.state.block[P.OFF_SETPOINT:P.OFF_SETPOINT + 2], "big")
            return self.write(P.OFF_SETPOINT, max(0, raw + step).to_bytes(2, "big"))
        return []

    def toggle_field(self, off: int, shift: int, on_value: int) -> list[int]:
        cur = (self.state.block[off] >> shift) & 0x03
        new = 0 if cur else on_value
        byte = (self.state.block[off] & ~(0x03 << shift)) | (new << shift)
        return self.write(off, bytes([byte & 0xFF]))

    def toggle_pump(self, n: int) -> list[int]:
        """Off or high. Real packs cycle low then high on two speed pumps, but
        the relay does not care which and one toggle keeps the tests honest."""
        changed = self.toggle_field(P.OFF_PUMP_STATUS, P.PUMP_STATUS_SHIFT[n], 2)
        changed += self.toggle_field(P.OFF_PUMP_DEMAND, P.PUMP_DEMAND_SHIFT[n], 2)
        any_on = any((self.state.block[P.OFF_PUMP_STATUS] >> s) & 0x03
                     for s in P.PUMP_STATUS_SHIFT.values())
        changed += self.write(P.OFF_PUMP_TIMER, bytes([PUMP_RUN_MINUTES if any_on else 0]))
        return changed

    def toggle_light(self) -> list[int]:
        on = self.state.block[P.OFF_LIGHT] != 0
        changed = self.write(P.OFF_LIGHT, bytes([0 if on else 1]))
        changed += self.write(P.OFF_LIGHT_TIMER, bytes([0 if on else LIGHT_RUN_MINUTES]))
        return changed


# -- standalone ------------------------------------------------------------


def selftest() -> int:
    """Drive the mock with a plain socket. Proves the mock before the relay
    leans on it, so a broken test fixture cannot look like a broken relay."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    failures = []

    def check(name, cond, detail=""):
        print(("  PASS  " if cond else "  FAIL  ") + name + (f"   {detail}" if detail else ""))
        if not cond:
            failures.append(name)

    spa = GeckoMockServer(host="127.0.0.1", port=0)
    port = spa.sock.getsockname()[1]
    threading.Thread(target=spa.serve_forever, daemon=True).start()

    cli = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    cli.settimeout(3.0)
    dest = ("127.0.0.1", port)
    cid = "IOSselftest"

    cli.sendto(P.build_tag("HELLO", "1"), dest)
    m = P.parse(cli.recvfrom(4096)[0])
    check("discovery names the spa", m and m.kind == "HELLO"
          and m.value.decode().startswith(SPA_ID), m.value if m else None)

    cli.sendto(P.build_tag("HELLO", cid), dest)
    cli.sendto(P.build_packt(b"SFILE\x01", cid, SPA_ID), dest)
    m = P.parse(cli.recvfrom(4096)[0])
    check("SFILE returns the pack files", m and m.verb == "FILES"
          and b"inXM_C06.xml" in m.body, m.verb if m else None)

    cli.sendto(P.build_packt(b"STATU\x02" + (0).to_bytes(2, "big")
                             + P.BLOCK_LEN.to_bytes(2, "big"), cid, SPA_ID), dest)
    st = P.SpaStatus()
    deadline = time.time() + 3
    while not st.complete and time.time() < deadline:
        try:
            m = P.parse(cli.recvfrom(4096)[0])
        except socket.timeout:
            break
        if m and m.verb == "STATV":
            b = m.body
            st.apply_block(b[0], b[3:3 + b[2]])
    check("STATU rebuilds a complete 637 byte block", st.complete)
    d = st.as_dict()
    check("set point reads back as 36.0 C", abs(d.get("setpoint_c", 0) - 36.0) < 0.05,
          d.get("setpoint_c"))
    check("block matches the server side byte for byte",
          bytes(st.block) == bytes(spa.state.block))

    cli.sendto(P.build_packt(b"SPACK\x03\x06\x02\x39\x01", cid, SPA_ID), dest)
    got = None
    deadline = time.time() + 3
    while time.time() < deadline:
        try:
            m = P.parse(cli.recvfrom(4096)[0])
        except socket.timeout:
            break
        if m and m.verb == "STATP":
            got = m
            break
    check("keypress produces a STATP notification", got is not None)
    if got:
        st.apply_statp(got.body)
        check("pump 1 reads as on after the keypress", st.pump(1) == 2, st.pump(1))
        check("pump timer armed", st.block[P.OFF_PUMP_TIMER] == PUMP_RUN_MINUTES)

    cli.sendto(P.build_packt(b"SPACK\x04\x06\x02\x39\x01", cid, SPA_ID), dest)
    time.sleep(0.4)
    check("keypress again turns pump 1 off", spa.state.pump(1) == 0, spa.state.pump(1))

    spa.running = False
    print()
    if failures:
        print(f"{len(failures)} FAILURE(S): {failures}")
        return 1
    print("mock spa self-test passed")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Mock in.touch2 module.")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=P.PORT)
    p.add_argument("--cloud", default=None,
                   help="host:port of the relay to send keepalives to")
    p.add_argument("--interval", type=float, default=CLOUD_PING_INTERVAL)
    p.add_argument("--spa-id", default=SPA_ID)
    p.add_argument("--selftest", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)

    if a.selftest:
        return selftest()

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
                        datefmt="%H:%M:%S")
    spa = GeckoMockServer(host=a.host, port=a.port, spa_id=a.spa_id)
    if a.cloud:
        host, _, port = a.cloud.rpartition(":")
        spa.set_cloud((host, int(port)), a.interval)
        LOG.info("keepalives to %s every %.0fs", a.cloud, a.interval)
    try:
        spa.serve_forever()
    except KeyboardInterrupt:
        spa.running = False
    return 0


if __name__ == "__main__":
    sys.exit(main())
