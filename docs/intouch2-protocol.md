# in.touch2 on the wire

What this relay needs to know about Gecko's in.touch2 protocol, and where it
came from. Everything below was derived from packet captures of a real
in.touch2 system (pack `inXM_C06` / `inXM_S06`) plus the protocol groundwork
done by [geckolib](https://github.com/gazoodle/geckolib), which decoded this
first and properly.

Anything marked **unverified** was inferred rather than observed. Treat it
accordingly.

---

## Transport

UDP, port 10022, both directions, no encryption and no authentication of any
kind. The same port carries LAN traffic and cloud traffic; the module does not
distinguish them.

Datagrams are one message each. Trailing NUL padding is common, so the parser
strips it before matching.

## Envelopes

Two shapes. Bare tags, always five uppercase letters:

```
<PINGS>1</PINGS>
<HELLO>1</HELLO>
<ISTHR>SPA80:1f:12:50:a5:d1</ISTHR>
<BONJR>0</BONJR>
```

And `PACKT`, which wraps everything substantive:

```
<PACKT><SRCCN>{sender}</SRCCN><DESCN>{recipient}</DESCN><DATAS>{body}</DATAS></PACKT>
```

`SRCCN` and `DESCN` are optional in the grammar and the relay tolerates both
being absent, but a real client sets both. The first five bytes of `DATAS` are
the verb; the rest is the verb's payload.

Identifiers:

| Kind | Form | Example |
|---|---|---|
| Spa | `SPA` + the module's MAC | `SPA80:1f:12:50:a5:d1` |
| Client | free-form, chosen by the client | `IOS8f2c…`, `GRLY…` |

The `SPA` prefix is load-bearing here: the relay uses it to tell a module apart
from an app without being told which is which.

## Verbs

### Session

| Message | Direction | Meaning |
|---|---|---|
| `<PINGS>1</PINGS>` | module → server | Keepalive, roughly every 21 s. |
| `<PINGS>0</PINGS>` | server → module | Acknowledgement. This is what keeps the status LED blue. |
| `<HELLO>1</HELLO>` | client → spa | LAN discovery, normally broadcast. Unicast works. |
| `<HELLO>{id}\|{name}</HELLO>` | spa → client | Discovery reply. |
| `<HELLO>{client_id}</HELLO>` | client → spa | The client announcing itself. The module ignores clients it has not been introduced to. |
| `<ISTHR>{spa_id}</ISTHR>` | client → server | "Is this spa reachable?" Cloud path only. |
| `<BONJR>0</BONJR>` | server → client | Yes. |
| `<BONJR>{client_id}</BONJR>` | client → server | The client announcing itself over the cloud path. |

### Data, inside `PACKT`

| Verb | Direction | Payload |
|---|---|---|
| `APING` | client → spa | none. Must repeat about every 2 s, see below. |
| `PACKS` | spa → client | none. Ping reply. |
| `SFILE` | client → spa | seq(1) |
| `FILES` | spa → client | comma-separated pack file list, e.g. `,inXM_C06.xml,inXM_S06.xml` |
| `AVERS` | client → spa | seq(1) |
| `SVERS` | spa → client | version bytes |
| `STATU` | client → spa | seq(1), start(2 BE), length(2 BE) |
| `STATV` | spa → client | index(1), next(1), length(1), data |
| `STATP` | spa → client | count(1), then count × [ position(2 BE), data(2) ] |
| `STATQ` | client → spa | seq(1). Re-arms change notification. |
| `SPACK` | client → spa | see below |

`STATV` carries the status block in chunks of 39 bytes. The client reassembles
by writing each chunk at `index × 39`, so chunks sit on a fixed grid. A full
block is 637 bytes (0x027D), which is 16 full chunks plus a 13-byte tail.

`STATP` is a partial update, pushed when something changes. Each entry writes
two bytes, so changing one byte means sending it together with its neighbour.

**The 2 second ping is not optional.** If a client's `APING` lapses, the module
stops sending that client `STATP` notifications. It keeps answering direct
requests, so the symptom is a dashboard that goes stale while the wire log
keeps moving. This is a documented finding from the geckolib project.

### `SPACK`

The body after the verb is `seq(1), 0x06, len(1), cmd(1), …`.

Keypress, `cmd = 0x39`:

```
SPACK {seq} 06 02 39 {key}
```

| Key | Button |
|---|---|
| 0x01, 0x02, 0x03 | Pump 1, 2, 3 |
| 0x06 | Aux / O3 |
| 0x10 | Lights |
| 0x15, 0x16 | Temp up, temp down |

Write, `cmd = 0x46`:

```
SPACK {seq} 06 07 46 06 06 {position(2 BE)} {value(2)}
```

Position is an offset into the status block. Writing the set point means
writing offset 15.

## Status block offsets

637 bytes. Confirmed offsets only; the rest of the block is carried and stored
but not decoded.

| Offset | Hex | Width | Field |
|---|---|---|---|
| 2 | 0x002 | byte | Light timeout, minutes (config) |
| 4 | 0x004 | byte | Pump timeout, minutes (config) |
| 15 | 0x00F | word | Set point |
| 261 | 0x105 | word | Water temperature |
| 263 | 0x107 | word | Heater temperature |
| 265 | 0x109 | word | Water temperature, second sensor |
| 275 | 0x113 | byte | Pump status, 2-bit fields; pump 1 in bits 7:6, aux in bits 1:0 |
| 306 | 0x132 | byte | Light |
| 355 | 0x163 | byte | Aux **(unverified)** |
| 356 | 0x164 | byte | Pump demand, 2-bit fields; pump 1 in bits 1:0 |
| 358 | 0x166 | byte | Pump timer, minutes remaining |
| 360 | 0x168 | byte | Light timer, minutes remaining |

Words are big-endian. The 2-bit pump fields read 0 off, 1 low, 2 high.

Note that pump status and pump demand pack their fields in opposite directions:
pump 1 is at bits 7:6 in status but bits 1:0 in demand. That is not a
transcription error, it is what the captures show.

## Temperature encoding

```
raw = (°F − 32) × 10 = °C × 18
```

Raw 648 is 36.0 °C, or 96.8 °F. Worth stating plainly because the obvious
guess, tenths of °F, gives 64.8 °F and is wrong in a way that looks almost
plausible.

## What the cloud service appears to do

From the captures, very little:

1. Answer `<PINGS>1</PINGS>` with `<PINGS>0</PINGS>`.
2. Answer `<ISTHR>{spa}</ISTHR>` with `<BONJR>0</BONJR>`, and remember that the
   asking client wants that spa.
3. Forward `PACKT` envelopes between client and spa, byte for byte.

No authentication was observed at any point. That is why this relay can stand
in for it, and also why port 10022 must not be exposed to the internet.

## Known gaps

- Push notifications, firmware update and account registration are not
  implemented, and it is not known whether the module needs any of them.
- Whether the module wants a time source from the server is unresolved. It is
  an open question in the geckolib project too.
- Only the LAN dialect is covered. There is no support for reaching the spa
  from outside the network.
- Offset 355 is labelled Aux from context, not from an observed change.
