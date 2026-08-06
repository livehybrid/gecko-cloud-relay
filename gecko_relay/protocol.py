"""in.touch2 wire protocol: envelopes, verbs and status block decoding.

Everything here was derived from packet captures of a real in.touch2 system
(pack inXM_C06 / inXM_S06). Offsets are documented in docs/intouch2-protocol.md.
Stdlib only, on purpose: this runs on a box that may have no internet.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

PORT = 10022
BLOCK_LEN = 637          # 0x027D
STATV_CHUNK = 39

# --- status block offsets ---------------------------------------------------

OFF_LIGHT_TIMEOUT_CFG = 2     # byte, minutes
OFF_PUMP_TIMEOUT_CFG = 4      # byte, minutes
OFF_SETPOINT = 15             # word BE
OFF_WATER_TEMP = 261          # word BE
OFF_HEATER_TEMP = 263         # word BE
OFF_WATER_TEMP_2 = 265        # word BE
OFF_PUMP_STATUS = 275         # 0x0113, 2-bit fields, pump 1 in bits 7:6
OFF_LIGHT = 306               # 0x0132
OFF_AUX = 355                 # 0x0163
OFF_PUMP_DEMAND = 356         # 0x0164, 2-bit fields, pump 1 in bits 1:0
OFF_PUMP_TIMER = 358          # 0x0166, byte, minutes
OFF_LIGHT_TIMER = 360         # 0x0168, byte, minutes

PUMP_STATUS_SHIFT = {1: 6, 2: 4, 3: 2}
PUMP_DEMAND_SHIFT = {1: 0, 2: 2, 3: 4}

KEYPAD = {
    0x01: "Pump 1",
    0x02: "Pump 2",
    0x03: "Pump 3",
    0x06: "Aux / O3",
    0x10: "Lights",
    0x15: "Temp up",
    0x16: "Temp down",
}

CMD_KEYPRESS = 0x39
CMD_SETVALUE = 0x46

# --- temperature ------------------------------------------------------------
# raw = (degF - 32) * 10 = degC * 18


def raw_to_c(raw: int) -> float:
    return raw / 18.0


def raw_to_f(raw: int) -> float:
    return raw / 10.0 + 32.0


def c_to_raw(celsius: float) -> int:
    return int(round(celsius * 18))


# --- envelopes --------------------------------------------------------------

_PACKT_RE = re.compile(
    rb"<PACKT>"
    rb"(?:<SRCCN>(?P<src>[^<]*)</SRCCN>)?"
    rb"(?:<DESCN>(?P<dst>[^<]*)</DESCN>)?"
    rb"<DATAS>(?P<data>.*)</DATAS>"
    rb"</PACKT>",
    re.S,
)
_TAG_RE = re.compile(rb"^<(?P<tag>[A-Z]{5})>(?P<val>.*)</(?P=tag)>$", re.S)


@dataclass
class Message:
    """One decoded datagram."""

    raw: bytes
    kind: str                      # "PACKT" or the bare tag, e.g. "PINGS"
    value: bytes = b""             # bare-tag payload
    src: str = ""
    dst: str = ""
    data: bytes = b""              # PACKT DATAS body, verb included
    verb: str = ""

    @property
    def body(self) -> bytes:
        """DATAS with the 5-byte verb stripped."""
        return self.data[5:]

    def describe(self) -> str:
        """One-line human summary, used by the log and the web UI."""
        if self.kind != "PACKT":
            return f"{self.kind} {self.value.decode('latin1', 'replace')}"
        b = self.body
        if self.verb == "SPACK" and len(b) >= 4:
            cmd = b[3]
            if cmd == CMD_KEYPRESS and len(b) >= 5:
                key = b[4]
                return f"SPACK keypress {KEYPAD.get(key, f'0x{key:02x}')}"
            if cmd == CMD_SETVALUE and len(b) >= 8:
                pos = int.from_bytes(b[6:8], "big")
                val = b[8:]
                extra = ""
                if pos == OFF_SETPOINT and len(val) == 2:
                    raw = int.from_bytes(val, "big")
                    extra = f" ({raw_to_c(raw):.1f} C)"
                return f"SPACK write @{pos} = {val.hex()}{extra}"
            return f"SPACK cmd 0x{cmd:02x}"
        if self.verb == "STATP":
            chs = decode_statp(b)
            parts = [f"@{p}={d.hex()}" for p, d in chs]
            return "STATP " + " ".join(parts)
        if self.verb == "STATU" and len(b) >= 5:
            start = int.from_bytes(b[1:3], "big")
            ln = int.from_bytes(b[3:5], "big")
            return f"STATU read {ln} bytes from {start}"
        if self.verb == "STATV" and len(b) >= 3:
            return f"STATV block {b[0]} (next {b[1]}, {b[2]} bytes)"
        if self.verb in ("APING", "PACKS", "RMSET"):
            return self.verb
        if b:
            return f"{self.verb} {b[:24].hex()}"
        return self.verb


def parse(payload: bytes) -> Optional[Message]:
    """Decode a datagram. Returns None if it is not in.touch2 traffic."""
    payload = payload.rstrip(b"\x00")
    m = _PACKT_RE.search(payload)
    if m:
        data = m.group("data")
        return Message(
            raw=payload,
            kind="PACKT",
            src=(m.group("src") or b"").decode("latin1"),
            dst=(m.group("dst") or b"").decode("latin1"),
            data=data,
            verb=data[:5].decode("latin1", "replace"),
        )
    t = _TAG_RE.match(payload)
    if t:
        return Message(raw=payload, kind=t.group("tag").decode(), value=t.group("val"))
    return None


def build_packt(data: bytes, src: str, dst: str = "") -> bytes:
    return (
        b"<PACKT><SRCCN>" + src.encode()
        + b"</SRCCN><DESCN>" + dst.encode()
        + b"</DESCN><DATAS>" + data + b"</DATAS></PACKT>"
    )


def build_tag(tag: str, value: str) -> bytes:
    return f"<{tag}>{value}</{tag}>".encode()


# --- status block -----------------------------------------------------------


def decode_statp(body: bytes) -> list[tuple[int, bytes]]:
    """STATP body -> [(position, 2 bytes), ...].

    Framing: count(1) then count * [position(2, BE), data(2)].
    """
    if not body:
        return []
    n = body[0]
    out = []
    for i in range(n):
        e = body[1 + i * 4: 5 + i * 4]
        if len(e) < 4:
            break
        out.append((int.from_bytes(e[:2], "big"), e[2:4]))
    return out


@dataclass
class SpaStatus:
    """Decoded view of the 637-byte status block."""

    block: bytearray = field(default_factory=lambda: bytearray(BLOCK_LEN))
    have: bytearray = field(default_factory=lambda: bytearray(BLOCK_LEN))

    def apply_block(self, index: int, data: bytes) -> None:
        off = index * STATV_CHUNK
        self.block[off:off + len(data)] = data
        self.have[off:off + len(data)] = b"\x01" * len(data)

    def apply_statp(self, body: bytes) -> list[int]:
        changed = []
        for pos, data in decode_statp(body):
            for i, b in enumerate(data):
                o = pos + i
                if o < BLOCK_LEN and (not self.have[o] or self.block[o] != b):
                    self.block[o] = b
                    self.have[o] = 1
                    changed.append(o)
        return changed

    @property
    def complete(self) -> bool:
        return all(self.have)

    def word(self, off: int) -> int:
        return int.from_bytes(self.block[off:off + 2], "big")

    def _field(self, off: int, shift: int) -> int:
        return (self.block[off] >> shift) & 0x03

    def pump(self, n: int) -> int:
        """0 = off, 1 = low, 2 = high."""
        return self._field(OFF_PUMP_STATUS, PUMP_STATUS_SHIFT[n])

    def pump_demand(self, n: int) -> int:
        return self._field(OFF_PUMP_DEMAND, PUMP_DEMAND_SHIFT[n])

    def as_dict(self) -> dict:
        if not any(self.have):
            return {"known": False}
        sp = self.word(OFF_SETPOINT)
        wt = self.word(OFF_WATER_TEMP)
        ht = self.word(OFF_HEATER_TEMP)
        return {
            "known": True,
            "complete": self.complete,
            "setpoint_c": round(raw_to_c(sp), 1),
            "setpoint_f": round(raw_to_f(sp), 1),
            "setpoint_raw": sp,
            "water_c": round(raw_to_c(wt), 1),
            "water_f": round(raw_to_f(wt), 1),
            "heater_c": round(raw_to_c(ht), 1),
            "pumps": [
                {"n": n, "state": self.pump(n), "demand": self.pump_demand(n)}
                for n in (1, 2, 3)
            ],
            "aux": self._field(OFF_PUMP_STATUS, 0),
            "light": self.block[OFF_LIGHT],
            "pump_timer_min": self.block[OFF_PUMP_TIMER],
            "light_timer_min": self.block[OFF_LIGHT_TIMER],
            "pump_timeout_cfg": self.block[OFF_PUMP_TIMEOUT_CFG],
            "light_timeout_cfg": self.block[OFF_LIGHT_TIMEOUT_CFG],
            "raw_pump_status": self.block[OFF_PUMP_STATUS],
            "raw_pump_demand": self.block[OFF_PUMP_DEMAND],
        }
