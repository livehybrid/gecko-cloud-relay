"""Dashboard. Stdlib http.server, no external assets.

The relay may run on a box with no internet, so there are no CDN fonts, no
frameworks and no build step. The page polls a JSON endpoint once a second.
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import poller as poller_mod

LOG = logging.getLogger("gecko.web")

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>in.touch2 relay</title>
<style>
  :root {
    /* Palette taken from the in.touch2 module's own status LED, plus water. */
    --bg:      #070c10;
    --panel:   #0f1820;
    --panel-2: #131f29;
    --line:    #1e2f3c;
    --text:    #cbd9e2;
    --dim:     #63808f;
    --led-red:   #ff5a5a;
    --led-green: #4ade80;
    --led-blue:  #58a6ff;
    --led-amber: #f5b942;
    --water:     #4fd1d9;
    --mono: ui-monospace, "SF Mono", SFMono-Regular, "Cascadia Mono", Menlo,
            Consolas, "Liberation Mono", monospace;
    --sans: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font-family: var(--sans); font-size: 14px; line-height: 1.5;
  }
  a { color: var(--led-blue); }
  .wrap { max-width: 1180px; margin: 0 auto; padding: 20px 18px 64px; }

  /* ---- header: the module's LED is the thing you diagnose against ---- */
  header {
    display: flex; align-items: center; gap: 18px; flex-wrap: wrap;
    padding: 18px 20px; background: var(--panel);
    border: 1px solid var(--line); border-radius: 10px;
  }
  .led {
    width: 46px; height: 46px; border-radius: 50%; flex: none;
    background: var(--c); box-shadow: 0 0 0 4px rgba(255,255,255,.03),
                0 0 26px -2px var(--c);
    position: relative;
  }
  .led::after {
    content: ""; position: absolute; inset: 0; border-radius: 50%;
    background: var(--c); opacity: .55;
    animation: pulse 2.4s ease-out infinite;
  }
  @keyframes pulse { 0% { transform: scale(1); opacity: .5; }
                     70% { transform: scale(1.7); opacity: 0; }
                     100% { opacity: 0; } }
  @media (prefers-reduced-motion: reduce) { .led::after { animation: none; } }
  .led-off { --c: #33454f; }
  .led-off::after { animation: none; }

  .hstate { flex: 1 1 260px; min-width: 0; }
  .hstate h1 { margin: 0; font-size: 19px; font-weight: 600; letter-spacing: -.01em; }
  .hstate p { margin: 2px 0 0; color: var(--dim); font-size: 13px; }
  .hmeta { display: flex; gap: 22px; flex-wrap: wrap; font-family: var(--mono); font-size: 12px; }
  .hmeta div span { display: block; color: var(--dim); font-size: 10px;
                    letter-spacing: .09em; text-transform: uppercase; }

  /* ---- panels ---- */
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-top: 14px; }
  @media (max-width: 820px) { .grid { grid-template-columns: 1fr; } }
  .panel { background: var(--panel); border: 1px solid var(--line);
           border-radius: 10px; padding: 16px 18px; }
  .panel > h2 {
    margin: 0 0 14px; font-size: 10px; letter-spacing: .13em;
    text-transform: uppercase; color: var(--dim); font-weight: 600;
  }

  /* ---- temperature ---- */
  .temp { display: flex; align-items: baseline; gap: 10px; font-family: var(--mono); }
  .temp b { font-size: 52px; font-weight: 500; color: var(--water);
            letter-spacing: -.03em; line-height: 1; }
  .temp i { font-style: normal; font-size: 18px; color: var(--dim); }
  .tsub { margin-top: 10px; display: flex; gap: 26px; font-family: var(--mono);
          font-size: 13px; flex-wrap: wrap; }
  .tsub span { color: var(--dim); }
  .bar { margin-top: 14px; height: 4px; background: var(--panel-2);
         border-radius: 2px; overflow: hidden; }
  .bar div { height: 100%; background: var(--water); transition: width .5s ease; }

  /* ---- accessories ---- */
  .acc { display: grid; grid-template-columns: repeat(auto-fit, minmax(84px, 1fr)); gap: 9px; }
  .dev { background: var(--panel-2); border: 1px solid var(--line);
         border-radius: 7px; padding: 10px 11px; text-align: left;
         font: inherit; color: inherit; width: 100%; }
  .dev .n { font-size: 10px; letter-spacing: .09em; text-transform: uppercase;
            color: var(--dim); }
  .dev .v { font-family: var(--mono); font-size: 15px; margin-top: 3px; }
  .dev.on { border-color: var(--led-green); }
  .dev.on .v { color: var(--led-green); }
  .dev.off .v { color: #4a6373; }
  button.dev { cursor: pointer; transition: border-color .15s, background .15s; }
  button.dev:hover:not(:disabled) { background: #18262f; border-color: var(--dim); }
  button.dev:active:not(:disabled) { transform: translateY(1px); }
  button.dev:disabled { cursor: not-allowed; opacity: .5; }
  .dev.busy { border-color: var(--led-amber); }

  /* ---- set point stepper ---- */
  .step { display: flex; align-items: center; gap: 8px; margin-top: 12px; }
  .step button {
    width: 34px; height: 34px; flex: none; border-radius: 7px; cursor: pointer;
    background: var(--panel-2); border: 1px solid var(--line); color: var(--text);
    font-size: 19px; line-height: 1; font-family: var(--mono);
  }
  .step button:hover:not(:disabled) { border-color: var(--water); color: var(--water); }
  .step button:disabled { opacity: .4; cursor: not-allowed; }
  .step .lbl { font-size: 12px; color: var(--dim); }
  .msg { margin-top: 10px; font-size: 12px; min-height: 16px; color: var(--dim); }
  .msg.bad { color: var(--led-red); }
  .msg.good { color: var(--led-green); }

  /* ---- wire log ---- */
  .logpanel { margin-top: 14px; }
  .logbar { display: flex; gap: 14px; align-items: center; flex-wrap: wrap;
            margin-bottom: 10px; }
  .logbar label { font-size: 12px; color: var(--dim); display: flex;
                  align-items: center; gap: 6px; cursor: pointer; }
  #log { font-family: var(--mono); font-size: 12px; max-height: 460px;
         overflow-y: auto; border-top: 1px solid var(--line); }
  .row { display: grid; grid-template-columns: 74px 62px 138px 1fr;
         gap: 10px; padding: 4px 2px; border-bottom: 1px solid #131f28; }
  @media (max-width: 700px) { .row { grid-template-columns: 62px 54px 1fr; }
                              .row .peer { display: none; } }
  .row:hover { background: var(--panel-2); }
  .row .t { color: var(--dim); }
  .row .peer { color: var(--dim); overflow: hidden; text-overflow: ellipsis;
               white-space: nowrap; }
  .row .s { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .tag { font-size: 10px; letter-spacing: .06em; text-transform: uppercase;
         padding: 1px 5px; border-radius: 3px; text-align: center; }
  .in  { background: rgba(88,166,255,.14);  color: var(--led-blue); }
  .out { background: rgba(74,222,128,.13);  color: var(--led-green); }
  .sys { background: rgba(245,185,66,.14);  color: var(--led-amber); }
  .row.err .s { color: var(--led-red); }
  .empty { color: var(--dim); padding: 22px 2px; }

  table.ep { width: 100%; border-collapse: collapse; font-family: var(--mono);
             font-size: 12px; }
  table.ep th { text-align: left; color: var(--dim); font-weight: 500;
                font-size: 10px; letter-spacing: .09em; text-transform: uppercase;
                padding-bottom: 6px; }
  table.ep td { padding: 4px 10px 4px 0; border-top: 1px solid var(--line);
                white-space: nowrap; }
  .idcell { max-width: 240px; overflow: hidden; text-overflow: ellipsis; }
  :focus-visible { outline: 2px solid var(--led-blue); outline-offset: 2px; }
</style>
</head>
<body>
<div class="wrap">

  <header>
    <div id="led" class="led led-off"></div>
    <div class="hstate">
      <h1 id="state">Waiting for the spa module</h1>
      <p id="statehint">Nothing has reached the relay yet.</p>
    </div>
    <div class="hmeta">
      <div><span>Spa</span><b id="spaid">&mdash;</b></div>
      <div><span>Address</span><b id="spaaddr">&mdash;</b></div>
      <div><span>Last ping</span><b id="ping">&mdash;</b></div>
      <div><span>In / Out</span><b id="counts">0 / 0</b></div>
      <div><span>Uptime</span><b id="uptime">0s</b></div>
    </div>
  </header>

  <div class="grid">
    <section class="panel">
      <h2>Water</h2>
      <div class="temp"><b id="water">--.-</b><i>&deg;C</i></div>
      <div class="tsub">
        <div><span>Set point</span> <b id="setpoint">--.-</b> &deg;C</div>
        <div><span>Heater</span> <b id="heater">--.-</b> &deg;C</div>
        <div><span>&deg;F</span> <b id="waterf">--.-</b></div>
      </div>
      <div class="bar"><div id="tempbar" style="width:0%"></div></div>
      <div class="step">
        <button id="tdown" type="button" disabled aria-label="Lower the set point by 0.5 degrees">&minus;</button>
        <button id="tup" type="button" disabled aria-label="Raise the set point by 0.5 degrees">+</button>
        <span class="lbl">Set point, 0.5 &deg;C steps</span>
      </div>
      <div class="msg" id="tmsg" role="status" aria-live="polite"></div>
    </section>

    <section class="panel">
      <h2>Accessories</h2>
      <div class="acc" id="acc"></div>
      <div class="tsub" style="margin-top:12px">
        <div><span>Pump timer</span> <b id="ptimer">&mdash;</b></div>
        <div><span>Light timer</span> <b id="ltimer">&mdash;</b></div>
      </div>
      <div class="msg" id="amsg" role="status" aria-live="polite"></div>
    </section>
  </div>

  <section class="panel logpanel">
    <h2>Endpoints</h2>
    <table class="ep">
      <thead><tr><th>Id</th><th>Kind</th><th>Address</th><th>Idle</th><th>Rx</th><th>Tx</th></tr></thead>
      <tbody id="eps"><tr><td colspan="6" class="empty">No endpoints yet.</td></tr></tbody>
    </table>
  </section>

  <section class="panel logpanel">
    <h2>Wire log</h2>
    <div class="logbar">
      <label><input type="checkbox" id="follow" checked> Follow</label>
      <label><input type="checkbox" id="hidepings"> Hide keepalives</label>
      <label><input type="checkbox" id="showhex"> Show hex</label>
      <span style="color:var(--dim);font-size:12px" id="logcount"></span>
    </div>
    <div id="log"><div class="empty">Waiting for traffic.</div></div>
  </section>
</div>

<script>
(function () {
  var lastSeq = 0, rows = [], MAX = 600;
  var logEl = document.getElementById('log');
  var elFollow = document.getElementById('follow');
  var elHide = document.getElementById('hidepings');
  var elHex = document.getElementById('showhex');

  function fmtTime(ts) {
    var d = new Date(ts * 1000);
    return d.toTimeString().slice(0, 8) + '.' +
           String(d.getMilliseconds()).padStart(3, '0');
  }
  function esc(s) {
    return String(s).replace(/[&<>]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c];
    });
  }
  function isPing(e) {
    return e.kind === 'PINGS' || e.verb === 'APING';
  }

  function renderLog() {
    var hide = elHide.checked, hex = elHex.checked;
    var vis = rows.filter(function (e) { return !(hide && isPing(e)); });
    document.getElementById('logcount').textContent =
      vis.length + ' shown / ' + rows.length + ' held';
    if (!vis.length) { logEl.innerHTML = '<div class="empty">Waiting for traffic.</div>'; return; }
    logEl.innerHTML = vis.map(function (e) {
      var cls = e.dir === 'in' ? 'in' : (e.dir === 'out' ? 'out' : 'sys');
      var err = e.note && /drop|unrecognised/.test(e.note) ? ' err' : '';
      var txt = e.summary + (e.note ? '  \u00b7 ' + e.note : '');
      if (hex) { txt += '\n    ' + e.hex; }
      return '<div class="row' + err + '">' +
             '<div class="t">' + fmtTime(e.ts) + '</div>' +
             '<div><span class="tag ' + cls + '">' + e.dir + '</span></div>' +
             '<div class="peer">' + esc(e.peer) + '</div>' +
             '<div class="s" style="white-space:' + (hex ? 'pre-wrap' : 'nowrap') + '">' +
             esc(txt) + '</div></div>';
    }).join('');
    if (elFollow.checked) { logEl.scrollTop = logEl.scrollHeight; }
  }
  elHide.onchange = elHex.onchange = renderLog;

  var LED = {
    blue:  { c: 'var(--led-blue)',  t: 'Module connected',
             h: 'The relay is answering keepalives. The module LED should be blue.' },
    amber: { c: 'var(--led-amber)', t: 'Module stalled',
             h: 'Seen before, but no keepalive recently. Check power and cabling.' },
    off:   { c: null, t: 'Waiting for the spa module',
             h: 'Nothing has reached the relay yet. Check DNS or the redirect rule.' }
  };

  function setLed(key) {
    var s = LED[key], el = document.getElementById('led');
    if (s.c) { el.className = 'led'; el.style.setProperty('--c', s.c); }
    else { el.className = 'led led-off'; el.style.removeProperty('--c'); }
    document.getElementById('state').textContent = s.t;
    document.getElementById('statehint').textContent = s.h;
  }

  function devCell(name, on, label, button) {
    var cls = 'dev ' + (on ? 'on' : 'off') + (busy[button] ? ' busy' : '');
    var inner = '<div class="n">' + name + '</div>' +
                '<div class="v">' + (busy[button] ? '...' : label) + '</div>';
    if (!button) { return '<div class="' + cls + '">' + inner + '</div>'; }
    return '<button type="button" class="' + cls + '" data-button="' + button + '"' +
           (canControl ? '' : ' disabled') + '>' + inner + '</button>';
  }

  // ---- control ----
  var canControl = false, busy = {}, lastState = null;

  function say(el, text, kind) {
    var m = document.getElementById(el);
    m.textContent = text;
    m.className = 'msg' + (kind ? ' ' + kind : '');
  }

  function post(body, msgEl, describe) {
    return fetch('api/command', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }).then(function (r) {
      return r.json().then(function (d) { return { ok: r.ok, d: d }; });
    }).then(function (res) {
      if (res.ok && res.d.ok) { say(msgEl, describe + ' sent', 'good'); }
      else { say(msgEl, res.d.error || 'command failed', 'bad'); }
    }).catch(function () {
      say(msgEl, 'could not reach the relay', 'bad');
    });
  }

  document.getElementById('acc').addEventListener('click', function (ev) {
    var btn = ev.target.closest('button[data-button]');
    if (!btn || !canControl) { return; }
    var name = btn.dataset.button;
    // The spa reports the new state via STATP, normally well inside a second.
    // Hold the tile until then rather than guessing at the result locally.
    busy[name] = true;
    if (lastState) { paint(lastState); }
    setTimeout(function () { busy[name] = false; }, 1500);
    post({ action: 'press', button: name }, 'amsg', name);
  });

  function nudge(delta) {
    if (!canControl) { return; }
    post({ action: 'setpoint', delta_c: delta }, 'tmsg',
         'set point ' + (delta > 0 ? '+' : '') + delta + ' °C');
  }
  document.getElementById('tup').onclick = function () { nudge(0.5); };
  document.getElementById('tdown').onclick = function () { nudge(-0.5); };

  function paint(d) {
    var r = d.relay, s = d.spa;
    lastState = d;
    // Controls need a client the spa has accepted, which means a complete
    // status block. Anything less and a press would just be dropped.
    // The server decides: a module can push live changes while refusing to
    // dump the whole block, and that is still a session we can drive.
    canControl = !!(s && s.controllable);
    document.getElementById('tup').disabled = !canControl;
    document.getElementById('tdown').disabled = !canControl;
    setLed(!r.spa_id ? 'off' : (r.spa_online ? 'blue' : 'amber'));
    document.getElementById('spaid').textContent = r.spa_id || '\u2014';
    document.getElementById('spaaddr').textContent = r.spa_addr || '\u2014';
    document.getElementById('ping').textContent =
      r.spa_last_ping_s === null ? '\u2014' : r.spa_last_ping_s + 's ago';
    document.getElementById('counts').textContent =
      r.packets_in + ' / ' + r.packets_out +
      (r.packets_dropped ? '  (' + r.packets_dropped + ' dropped)' : '');
    document.getElementById('uptime').textContent = Math.round(r.uptime_s) + 's';

    document.getElementById('eps').innerHTML = r.endpoints.length
      ? r.endpoints.map(function (e) {
          return '<tr><td class="idcell">' + esc(e.id) + '</td><td>' + e.kind +
                 '</td><td>' + e.addr + '</td><td>' + e.idle_s + 's</td><td>' +
                 e.rx + '</td><td>' + e.tx + '</td></tr>';
        }).join('')
      : '<tr><td colspan="6" class="empty">No endpoints yet.</td></tr>';

    var acc = document.getElementById('acc');
    if (s && s.known) {
      // A field we have not been told is shown as unknown, never as zero.
      var dash = '\u2014';
      function t(v) { return v === null || v === undefined ? dash : v.toFixed(1); }
      function n(v) { return v === null || v === undefined ? dash : v; }
      function state(v) {
        return v === null || v === undefined ? dash
             : (v === 0 ? 'OFF' : (v === 1 ? 'LOW' : 'HIGH'));
      }
      document.getElementById('water').textContent = t(s.water_c);
      document.getElementById('waterf').textContent = t(s.water_f);
      document.getElementById('setpoint').textContent = t(s.setpoint_c);
      document.getElementById('heater').textContent = t(s.heater_c);
      var pct = s.water_c === null || s.water_c === undefined ? 0
              : Math.max(0, Math.min(100, ((s.water_c - 10) / 30) * 100));
      document.getElementById('tempbar').style.width = pct + '%';
      acc.innerHTML =
        s.pumps.map(function (p) {
          return devCell('Pump ' + p.n, p.state > 0, state(p.state), 'pump' + p.n);
        }).join('') +
        devCell('Aux / O3', s.aux > 0,
                s.aux === null || s.aux === undefined ? dash : (s.aux > 0 ? 'ON' : 'OFF'),
                'aux') +
        devCell('Lights', s.light > 0,
                s.light === null || s.light === undefined ? dash : (s.light > 0 ? 'ON' : 'OFF'),
                'light');
      document.getElementById('ptimer').textContent =
        n(s.pump_timer_min) + ' / ' + n(s.pump_timeout_cfg) + ' min';
      document.getElementById('ltimer').textContent =
        n(s.light_timer_min) + ' / ' + n(s.light_timeout_cfg) + ' min';
    } else {
      acc.innerHTML = '<div class="empty">' +
        (s && s.enabled === false ? 'Status poller is switched off.'
                                  : 'No status block read yet.') + '</div>';
    }
  }

  function tick() {
    fetch('api/state').then(function (r) { return r.json(); }).then(function (d) {
      paint(d);
    }).catch(function () { setLed('off'); });

    fetch('api/events?since=' + lastSeq).then(function (r) { return r.json(); })
      .then(function (d) {
        if (d.events.length) {
          lastSeq = d.events[d.events.length - 1].seq;
          rows = rows.concat(d.events).slice(-MAX);
          renderLog();
        }
      }).catch(function () {});
  }
  tick();
  setInterval(tick, 1000);
})();
</script>
</body>
</html>
"""


class _Handler(BaseHTTPRequestHandler):
    server_version = "GeckoRelay/1.0"
    relay = None
    poller = None

    def log_message(self, fmt, *args):
        LOG.debug("%s %s", self.address_string(), fmt % args)

    def _send(self, code, body, ctype):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if u.path == "/api/state":
            return self._send(200, json.dumps({
                "relay": self.relay.status(),
                "spa": self.poller.as_dict() if self.poller else None,
            }), "application/json")
        if u.path == "/api/events":
            since = int(q.get("since", ["0"])[0])
            return self._send(200, json.dumps(
                {"events": self.relay.events.since(since)}), "application/json")
        if u.path == "/api/block":
            # Raw status block, for offline decoding and bug reports.
            blk = self.poller.status if self.poller else None
            return self._send(200, json.dumps({
                "hex": bytes(blk.block).hex() if blk else None,
                "complete": blk.complete if blk else False,
            }), "application/json")
        return self._send(404, "not found\n", "text/plain")

    def do_POST(self):
        """Drive the spa. The only endpoint here that writes anything."""
        u = urlparse(self.path)
        if u.path != "/api/command":
            return self._send(404, "not found\n", "text/plain")

        def fail(code, msg):
            return self._send(code, json.dumps({"ok": False, "error": msg}),
                              "application/json")

        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, OSError):
            return fail(400, "body must be JSON")

        if self.poller is None:
            return fail(503, "no status poller, so there is no client to send as")
        why = self.poller.ready()
        if why:
            return fail(503, why)

        action = body.get("action")
        try:
            if action == "press":
                button = body.get("button")
                if button not in poller_mod.BUTTONS:
                    return fail(400, f"unknown button {button!r}")
                self.poller.press(button)
                return self._send(200, json.dumps({"ok": True, "pressed": button}),
                                  "application/json")
            if action == "setpoint":
                if "celsius" in body:
                    sent = self.poller.set_setpoint(float(body["celsius"]))
                elif "delta_c" in body:
                    sent = self.poller.nudge_setpoint(float(body["delta_c"]))
                else:
                    return fail(400, "setpoint needs celsius or delta_c")
                return self._send(200, json.dumps({"ok": True, "setpoint_c": sent}),
                                  "application/json")
        except (TypeError, ValueError) as exc:
            return fail(400, str(exc))
        except OSError as exc:
            return fail(502, f"could not reach the spa: {exc}")
        return fail(400, f"unknown action {action!r}")


def start_web(relay, poller, host: str = "0.0.0.0", port: int = 8080):
    handler = type("Handler", (_Handler,), {"relay": relay, "poller": poller})
    httpd = ThreadingHTTPServer((host, port), handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True, name="web")
    t.start()
    LOG.info("dashboard on http://%s:%s/", host, port)
    return httpd
