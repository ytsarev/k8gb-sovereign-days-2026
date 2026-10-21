#!/usr/bin/env python3
"""A full-screen, read-only k8gb failover audience console. Python 3.9+."""
import argparse
from collections import Counter, deque
import concurrent.futures as futures
import curses
import html
import json
import math
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

from core import (addresses, clean, collect_deployment, collect_readiness,
                  load_config, now, utc, valid)

HERE = Path(__file__).resolve().parent
COLORS = {"bg": "111D2D", "panel": "1B2B40", "border": "35465F",
          "white": "F4F7FC", "muted": "A4B5CC", "blue": "7AA2FF",
          "teal": "62D0CB", "red": "FF738B", "green": "82D7A4", "amber": "F5CB7C"}
SOURCES = ("ready_A", "ready_B", "deployment_A", "deployment_B", "dns_A", "dns_B", "resolver", "request")


class State:
    def __init__(self, config, mode, start):
        self.config, self.mode, self.start = config, mode, start
        self.latest = {}
        self.events = deque(maxlen=150)
        self.requests = deque(maxlen=100)
        self.counts = Counter()
        self.transport_error = ""

    def apply(self, message):
        source, data = message["source"], message["data"]
        if source == "marker":
            self.events.append((data["at"], "amber", "MARK  " + clean(data["text"])))
            return
        if source not in SOURCES:
            raise ValueError("Unknown observation source: " + source)
        old = self.latest.get(source)
        if old and data["at"] <= old["at"]:
            return
        self.latest[source] = data
        if source == "request":
            outcome = (data.get("served_by") or "?") if data.get("ok") and data.get("success") else "x"
            self.requests.append((data["at"], outcome))
            self.counts[outcome] += 1
        def signature(obs):
            if not obs:
                return None
            if not obs.get("ok"):
                return (False, obs.get("error"))
            if source.startswith("ready"):
                return (True, obs["ready"], obs["total"])
            if source.startswith("deployment"):
                return (True, obs["desired"], obs["ready"])
            if source.startswith("dns") or source == "resolver":
                return (True, tuple(addresses(obs)))
            return (True, obs.get("status"), obs.get("served_by"), obs.get("success"))
        if signature(old) != signature(data):
            color = "muted"
            if not data.get("ok"):
                detail = clean(data.get("error", "observation failed"), 110)
                color = "red"
            elif source.startswith("ready"):
                detail = "%s/%s ready endpoints" % (data["ready"], data["total"])
                color = "green" if data["ready"] else "red"
            elif source.startswith("deployment"):
                detail = "%s desired / %s ready replicas" % (data["desired"], data["ready"])
            elif source.startswith("dns") or source == "resolver":
                detail = ", ".join(addresses(data)) or "NOERROR / no A records"
                color = "blue" if source == "dns_A" else "teal" if source == "dns_B" else "amber"
            else:
                detail = "HTTP %s / %s" % (data.get("status", "?"),
                    "served by " + data["served_by"] if data.get("served_by") else "identity unknown")
                color = "blue" if data.get("served_by") == "A" else "teal" if data.get("served_by") == "B" else "red"
            self.events.append((data["at"], color, source.replace("_", " ").upper() + "  " + detail))


class Canvas:
    """The same character grid drives curses, text snapshots and SVG previews."""
    def __init__(self, width, height):
        self.width, self.height = width, height
        self.cells = [[(" ", "white", "bg", False) for _ in range(width)] for _ in range(height)]

    def text(self, x, y, value, fg="white", bg="bg", bold=False, width=None):
        if not 0 <= y < self.height:
            return
        value = clean(value, 3000)
        available = min(self.width - x, width if width is not None else self.width)
        if len(value) > available:
            value = value[:max(0, available - 1)] + "…"
        for offset, char in enumerate(value):
            if 0 <= x + offset < self.width:
                self.cells[y][x + offset] = (char, fg, bg, bold)

    def fill(self, x, y, w, h, bg):
        for row in range(y, y + h):
            self.text(x, row, " " * w, bg=bg)

    def box(self, x, y, w, h, title, accent="border"):
        self.fill(x, y, w, h, "panel")
        self.text(x, y, "╭" + "─" * (w - 2) + "╮", accent, "panel")
        self.text(x, y + h - 1, "╰" + "─" * (w - 2) + "╯", accent, "panel")
        for row in range(y + 1, y + h - 1):
            self.text(x, row, "│", accent, "panel")
            self.text(x + w - 1, row, "│", accent, "panel")
        self.text(x + 2, y, " " + title + " ", accent, "panel", True, w - 5)

    def plain(self):
        return "\n".join("".join(cell[0] for cell in row) for row in self.cells)

    def svg(self):
        # A rendering of the actual terminal character grid, not a UI mockup.
        cw, ch = 9, 19
        w, h = self.width * cw + 32, self.height * ch + 32
        out = ['<svg xmlns="http://www.w3.org/2000/svg" width="%s" height="%s" viewBox="0 0 %s %s">' % (w, h, w, h),
               '<rect width="%s" height="%s" fill="#%s"/>' % (w, h, COLORS["bg"]),
               '<g font-family="monospace" font-size="14">']
        for y, row in enumerate(self.cells):
            for x, (char, fg, bg, bold) in enumerate(row):
                px, py = 16 + x * cw, 16 + y * ch
                if bg != "bg":
                    out.append('<rect x="%s" y="%s" width="9" height="19" fill="#%s"/>' % (px, py, COLORS[bg]))
                if char != " ":
                    out.append('<text x="%s" y="%s" fill="#%s" font-weight="%s">%s</text>' %
                               (px, py + 14, COLORS[fg], "bold" if bold else "normal", html.escape(char)))
        return "\n".join(out + ["</g></svg>"])


def age_label(obs, clock):
    return "not observed" if not obs else "sample %.1fs ago" % max(0, clock - obs["at"])


def describe(obs, clock, cfg, formatter):
    if not obs:
        return "WAITING", "muted"
    if clock - obs["at"] > cfg["stale_after"]:
        return "STALE / no current observation", "amber"
    if obs["at"] - clock > 1:
        return "UNKNOWN / observer clock is ahead", "amber"
    if not obs.get("ok"):
        return "UNKNOWN / " + clean(obs.get("error", "read failed")), "red"
    return formatter(obs)


def render(state, width, height, clock, paused=False, finished=False):
    c = Canvas(width, height)
    if width < 110 or height < 38:
        c.text(2, 2, "k8gb / TRAFFIC STEERING", "teal", bold=True)
        c.text(2, 4, state.mode, "amber")
        c.text(2, 6, "Enlarge terminal to at least 110 columns × 38 rows.")
        c.text(2, 8, "Recommended: 132 × 44.  q = quit. Collection continues.", "muted")
        return c
    cfg = state.config
    elapsed = max(0, clock - state.start)
    c.text(2, 0, "k8gb  /  WHO CONTROLS THE FAILOVER?", "white", bold=True)
    mode = state.mode + (" · PAUSED" if paused else " · ENDED" if finished else "")
    c.text(width - len(mode) - 2, 0, mode, "green" if state.mode == "LIVE" else "amber", bold=True)
    c.text(2, 2, cfg["hostname"], "white", bold=True)
    c.text(width - 36, 2, "T+%02d:%02d  ·  polling ≥ %.1fs" % (elapsed // 60, elapsed % 60, cfg["interval"]), "muted")
    c.text(2, 4, "READY ENDPOINTS  →  AUTHORITATIVE DNS  →  CLIENT RESOLVER  →  REAL HTTP REQUEST", "muted")
    obs = state.latest
    recent = lambda key: valid(obs.get(key), clock, cfg["stale_after"])
    if state.transport_error:
        headline, color = "OBSERVER DISCONNECTED  /  " + state.transport_error, "red"
    elif all(recent(k) for k in ("dns_A", "dns_B", "resolver")):
        a, b, cache = (addresses(obs[k]) for k in ("dns_A", "dns_B", "resolver"))
        if a != b:
            headline, color = "Authorities disagree. Compare the two answers before drawing conclusions.", "amber"
        elif a != cache:
            headline, color = "Both authorities agree. The client resolver returns a different answer.", "amber"
        elif recent("request") and obs["request"].get("success") and obs["request"].get("served_by"):
            headline, color = "Request served by CLUSTER %s  /  both authorities and resolver agree on A records." % obs["request"]["served_by"], "green"
        else:
            headline, color = "DNS answers agree. Check the actual application response below.", "amber"
    else:
        headline, color = "Waiting for fresh observations from both authorities and the client resolver.", "muted"
    c.text(2, 5, headline, color, bold=True, width=width - 4)
    gap, left, half = 3, 2, (width - 7) // 2
    right = left + half + gap
    for key, x, accent, role in (("A", left, "blue", "PRIMARY"), ("B", right, "teal", "SECONDARY")):
        c.box(x, 7, half, 11, "CLUSTER %s  /  %s  /  %s" % (key, cfg["clusters"][key]["geotag"], role), accent)
        def put(y, value, fg="white", bold=False):
            c.text(x + 2, y, value, fg, "panel", bold, half - 4)
        ready = obs.get("ready_" + key)
        label, fg = describe(ready, clock, cfg, lambda o: (
            "%s/%s READY ENDPOINTS" % (o["ready"], o["total"]), "green" if o["ready"] else "red"))
        put(9, label, fg, True)
        dep = obs.get("deployment_" + key)
        label, fg = describe(dep, clock, cfg, lambda o: ("Deployment: %s desired / %s ready" % (o["desired"], o["ready"]), "muted"))
        put(10, label, fg)
        put(11, "EndpointSlice · " + age_label(ready, clock), "muted")
        put(13, "CoreDNS %s  /  authoritative A answer" % key, accent, True)
        dns = obs.get("dns_" + key)
        label, fg = describe(dns, clock, cfg, lambda o: (", ".join(addresses(o)) or "NOERROR / no A records", accent))
        put(14, label, fg, True)
        if valid(dns, clock, cfg["stale_after"]) and dns.get("records"):
            put(15, "TTL at query: %ss  ·  AA verified" % min(r["ttl"] for r in dns["records"]), "muted")
        put(16, age_label(dns, clock), "muted")
    c.box(left, 19, half, 8, "CLIENT RESOLVER  /  UNBOUND", "amber")
    resolver = obs.get("resolver")
    label, fg = describe(resolver, clock, cfg, lambda o: (", ".join(addresses(o)) or "NOERROR / no A records", "white"))
    c.text(left + 2, 21, label, fg, "panel", True, half - 4)
    ttl = min((r["ttl"] for r in (resolver or {}).get("records", [])), default=None)
    c.text(left + 2, 22, "TTL returned: %ss  ·  %s" % (ttl, age_label(resolver, clock)) if recent("resolver") and ttl is not None else age_label(resolver, clock), "muted", "panel", width=half - 4)
    c.text(left + 2, 24, "Real cache · parent-zone delegation", "amber", "panel", width=half - 4)
    c.text(left + 2, 25, "Active DNS sample; HTTP does its own lookup.", "muted", "panel", width=half - 4)
    c.box(right, 19, half, 8, "APPLICATION  /  FRESH CONNECTIONS", "teal")
    request = obs.get("request")
    label, fg = describe(request, clock, cfg, lambda o: (
        "HTTP %s  ·  %s  ·  %sms" % (o.get("status", "?"), "CLUSTER " + o["served_by"] if o.get("served_by") else "IDENTITY UNKNOWN", o.get("latency_ms", "?")),
        "blue" if o.get("served_by") == "A" else "teal" if o.get("served_by") == "B" else "amber"))
    if request and not request.get("ok") and clock - request["at"] <= cfg["stale_after"]:
        label, fg = "FAILED / " + clean(request.get("error", "request failed")), "red"
    c.text(right + 2, 21, label, fg, "panel", True, half - 4)
    c.text(right + 2, 22, clean((request or {}).get("message", age_label(request, clock))), "muted", "panel", width=half - 4)
    for index, (_, outcome) in enumerate(list(state.requests)[-(half - 4):]):
        color = {"A": "blue", "B": "teal", "x": "red", "?": "amber"}[outcome]
        c.text(right + 2 + index, 24, "×" if outcome == "x" else outcome, color, "panel", True)
    c.text(right + 2, 25, "A %s  B %s  failed %s  unknown %s · newest →" % (state.counts["A"], state.counts["B"], state.counts["x"], state.counts["?"]), "muted", "panel", width=half - 4)
    timeline_height = height - 32
    c.box(2, 28, width - 4, timeline_height, "OBSERVATION TIMELINE  /  UTC", "muted")
    rows = timeline_height - 2
    events = sorted(state.events, key=lambda event: event[0])[-rows:]
    for index, (ts, fg, detail) in enumerate(events):
        c.text(4, 29 + index, utc(ts)[11:23] + "  " + detail, fg, "panel", width=width - 8)
    disclaimer = "ILLUSTRATIVE DATA · no clusters contacted" if state.mode == "ILLUSTRATIVE PREVIEW" else "Captured observations · original timing" if state.mode == "RECORDED REPLAY" else "READ-ONLY · change replicas in a second terminal · q leaves workloads unchanged"
    c.text(2, height - 3, disclaimer, "amber" if state.mode != "LIVE" else "muted", width=width - 4)
    c.text(2, height - 1, "q quit   m mark observation" if state.mode == "LIVE" else "q quit   space pause   r restart", "muted")
    c.text(width - 45, height - 1, "SOVEREIGN CLOUD DAYS 2026  /  k8gb", "muted")
    return c


def preview_messages(config):
    """Synthetic scenario, explicitly excluded from the live recording format."""
    start = 1792497600.0
    messages = []
    for second in range(0, 50, 2):
        ts = start + second
        ready_a = not 10 <= second < 32
        authority_ip = "172.20.0.5" if 14 <= second < 36 else "172.20.0.3"
        cache_ip = "172.20.0.5" if 22 <= second < 42 else "172.20.0.3"
        for key in ("A", "B"):
            ready = 1 if key == "B" or ready_a else 0
            messages.append({"source": "ready_" + key, "data": {"at": ts, "ok": True, "ready": ready, "total": ready}})
            messages.append({"source": "deployment_" + key, "data": {"at": ts, "ok": True, "desired": ready, "ready": ready}})
            messages.append({"source": "dns_" + key, "data": {"at": ts, "ok": True, "authoritative": True,
                "records": [{"ip": authority_ip, "ttl": 10, "name": config["hostname"]}]}})
        messages.append({"source": "resolver", "data": {"at": ts, "ok": True,
            "records": [{"ip": cache_ip, "ttl": 10 - ((second - 2) % 10), "name": config["hostname"]}]}})
        failure = 10 <= second < 22
        served = "A" if cache_ip.endswith(".3") else "B"
        data = {"at": ts, "ok": False, "error": "Connection refused / cached primary address"} if failure else {
            "at": ts, "ok": True, "success": True, "status": 200, "served_by": served,
            "latency_ms": 8 + second % 7, "message": "Ingress NGINX: " + config["clusters"][served]["geotag"]}
        messages.append({"source": "request", "data": data})
        if second in (10, 32):
            messages.append({"source": "marker", "data": {"at": ts,
                "text": "Illustrative action: " + ("scale A to zero" if second == 10 else "restore A to one replica")}})
    return start, messages


def render_compact(state, width, height, clock, paused=False, finished=False, cue=""):
    """A narrow audience pane: real request identity first, DNS and readiness below."""
    c = Canvas(width, height)
    if width < 48 or height < 32:
        c.text(1, 1, "k8gb / TRAFFIC", "teal", bold=True)
        c.text(1, 3, "Enlarge this pane to 48 × 32.", "amber")
        c.text(1, 5, "q quits; collection continues.", "muted")
        return c
    cfg, obs = state.config, state.latest
    w = width - 2
    fresh = lambda key: valid(obs.get(key), clock, cfg["stale_after"])
    c.text(2, 0, "k8gb / TRAFFIC", "teal", bold=True)
    mode = "LIVE" if state.mode == "LIVE" else "PREVIEW / ILLUSTRATIVE" if state.mode == "ILLUSTRATIVE PREVIEW" else "RECORDED REPLAY"
    c.text(2, 1, mode + (" · PAUSED" if paused else ""), "green" if mode == "LIVE" else "amber", bold=True)
    c.text(2, 3, cfg["hostname"], bold=True, width=width - 4)
    c.text(2, 4, cue or "Fresh connections · actual response identity", "muted", width=width - 4)

    c.box(1, 6, w, 8, "HTTP / SERVED BY", "teal")
    req = obs.get("request")
    label, fg = describe(req, clock, cfg, lambda o: (
        "CLUSTER " + o["served_by"] if o.get("success") and o.get("served_by") else "IDENTITY UNKNOWN" if o.get("success") else "REQUEST FAILED",
        "blue" if o.get("success") and o.get("served_by") == "A" else "teal" if o.get("success") and o.get("served_by") == "B" else "red"))
    if req and not req.get("ok") and 0 <= clock - req["at"] <= cfg["stale_after"]:
        label, fg = "REQUEST FAILED", "red"
    if fresh("request") and req.get("success") and req.get("served_by"):
        label = "CLIENT ━━━━━▶ " + label
    c.text(3, 8, label, fg, "panel", True, width - 6)
    detail = "HTTP %s  ·  %sms" % (req.get("status", "?"), req.get("latency_ms", "?")) if fresh("request") else age_label(req, clock)
    c.text(3, 9, detail, "muted", "panel", width=width - 6)
    for i, (_, outcome) in enumerate(list(state.requests)[-(width - 6):]):
        c.text(3 + i, 11, "×" if outcome == "x" else outcome,
               {"A": "blue", "B": "teal", "x": "red", "?": "amber"}[outcome], "panel", True)
    c.text(3, 12, "A %s   B %s   failed %s   ? %s  →" % tuple(state.counts[k] for k in ("A", "B", "x", "?")), "muted", "panel", width=width - 6)

    c.box(1, 15, w, 7, "DNS / AUTHORITY → RESOLVER", "amber")
    for row, key, title, accent in ((17, "dns_A", "A DNS", "blue"), (18, "dns_B", "B DNS", "teal"), (19, "resolver", "Cache", "amber")):
        data = obs.get(key)
        label, fg = describe(data, clock, cfg, lambda o: (", ".join(addresses(o)) or "no A records", accent))
        ttl = min((r["ttl"] for r in (data or {}).get("records", [])), default=None)
        if fresh(key) and ttl is not None:
            label += " · %ss" % ttl
        c.text(3, row, title + "  " + label, fg, "panel", width=width - 6)
    status, fg = "Waiting for fresh DNS observations", "muted"
    if all(fresh(k) for k in ("dns_A", "dns_B", "resolver")):
        a, b, resolver = (addresses(obs[k]) for k in ("dns_A", "dns_B", "resolver"))
        status, fg = ("Authorities differ", "red") if a != b else ("Cache differs from authorities", "amber") if a != resolver else ("DNS answers agree", "green")
    c.text(3, 20, status, fg, "panel", width=width - 6)

    for row, key, accent in ((23, "A", "blue"), (24, "B", "teal")):
        label, fg = describe(obs.get("ready_" + key), clock, cfg,
                             lambda o: ("%s/%s ready endpoints" % (o["ready"], o["total"]), accent if o["ready"] else "red"))
        c.text(2, row, "CLUSTER " + key + "  " + label, fg, width=width - 4)

    if state.transport_error:
        c.text(2, 26, "OBSERVER DISCONNECTED", "red", bold=True)
    else:
        c.text(2, 26, "LATEST CHANGES / UTC", "muted", bold=True)
    events = sorted(state.events, key=lambda event: event[0])[-max(1, height - 30):]
    for i, (ts, fg, detail) in enumerate(events):
        c.text(2, 27 + i, utc(ts)[11:19] + " " + detail, fg, width=width - 4)
    c.text(2, height - 2, "One character = one HTTP sample; newest →", "muted", width=width - 4)
    c.text(2, height - 1, "q quit · read-only" if state.mode == "LIVE" else "q quit · illustrative data" if state.mode == "ILLUSTRATIVE PREVIEW" else "q quit · space pause · r restart", "muted", width=width - 4)
    return c


def controlled_preview(path, clock):
    """Follow presenter cues; missing/partial files keep preview at the baseline."""
    try:
        cue = json.loads(path.read_text())
        values = [float(cue[k]) for k in ("offset", "until", "at")]
        offset, until, at = values
        if not all(math.isfinite(v) for v in values) or not 0 <= offset <= until <= 48:
            raise ValueError("Invalid preview cue")
        return min(until, offset + max(0, clock - at)), clean(cue.get("beat", ""))
    except (OSError, ValueError, KeyError, TypeError):
        return 0, "WAITING FOR PRESENTER"


class Recording:
    def __init__(self, path, config, start):
        # Exclusive creation protects the evidence from an earlier rehearsal.
        self.file = open(path, "x", encoding="utf-8")
        self.file.write(json.dumps({"schema": "k8gb-cli-v1", "origin": "live", "start": start, "config": config}) + "\n")
        self.file.flush()

    def append(self, message):
        self.file.write(json.dumps(message) + "\n")
        self.file.flush()

    def close(self):
        self.file.close()


def replay_file(path):
    with open(path, encoding="utf-8") as f:
        header = json.loads(next(f))
        if header.get("schema") != "k8gb-cli-v1" or header.get("origin") != "live":
            raise ValueError("Replay requires a live observation recording; fixtures are not recorded evidence")
        messages = [json.loads(line) for line in f if line.strip()]
    if not messages:
        raise ValueError("Recording has no observations")
    for msg in messages:
        if msg["source"] not in SOURCES + ("marker",) or not isinstance(msg["data"]["at"], (float, int)):
            raise ValueError("Malformed observation in recording")
    # Use original host receipt time where available. Remote clock skew should
    # not dictate replay pacing. The observation's own timestamp stays visible.
    messages.sort(key=lambda m: m.get("received_at", m["data"]["at"]))
    return header, messages


class Live:
    def __init__(self, config, start):
        self.config, self.start = config, start
        self.messages = queue.Queue()
        self.stop = threading.Event()
        self.process = None
        self.threads = []

    def launch(self):
        for fn in (self.kubernetes, self.observer):
            thread = threading.Thread(target=fn, daemon=True)
            thread.start()
            self.threads.append(thread)

    def put(self, source, data):
        self.messages.put({"source": source, "data": data, "received_at": now()})

    def kubernetes(self):
        with futures.ThreadPoolExecutor(max_workers=4) as pool:
            while not self.stop.is_set():
                started = time.monotonic()
                jobs = {pool.submit(fn, self.config, key): source + "_" + key
                        for key in ("A", "B") for fn, source in (
                            (collect_readiness, "ready"), (collect_deployment, "deployment"))}
                for job in futures.as_completed(jobs):
                    self.put(jobs[job], job.result())
                self.stop.wait(max(0, self.config["interval"] - (time.monotonic() - started)))

    def observer(self):
        try:
            self.process = subprocess.Popen(["docker", "logs", "--follow", "--since", utc(self.start),
                self.config["observer_container"]], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            errors = deque(maxlen=3)
            for line in self.process.stdout:
                if self.stop.is_set():
                    break
                try:
                    msg = json.loads(line)
                    if msg["source"] not in ("dns_A", "dns_B", "resolver", "request"):
                        raise ValueError("unexpected observer source")
                    self.put(msg["source"], msg["data"])
                except (ValueError, KeyError, TypeError):
                    errors.append(clean(line))
            if not self.stop.is_set():
                self.messages.put({"transport_error": "Observer log stream ended. " + " ".join(errors)})
        except Exception as exc:
            self.messages.put({"transport_error": clean(exc)})

    def close(self):
        self.stop.set()
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()


def paint(screen, canvas):
    screen.erase()
    for y, row in enumerate(canvas.cells):
        # Write runs to avoid terminal flicker and thousands of curses calls.
        x = 0
        while x < len(row):
            _, fg, bg, bold = row[x]
            end = x + 1
            while end < len(row) and row[end][1:] == row[x][1:]:
                end += 1
            attr = curses.color_pair(paint.pairs.get((fg, bg), 0)) | (curses.A_BOLD if bold else 0)
            try:
                screen.addstr(y, x, "".join(cell[0] for cell in row[x:end]), attr)
            except curses.error:
                pass  # Bottom-right cell and a concurrent resize may raise.
            x = end
    screen.refresh()


def setup_colors():
    paint.pairs = {}
    if not curses.has_colors():
        return
    curses.start_color()
    fallback = {"bg": 0, "panel": 0, "border": 4, "white": 7, "muted": 7,
                "blue": 4, "teal": 6, "red": 1, "green": 2, "amber": 3}
    palette = {}
    for name, value in COLORS.items():
        rgb = tuple(int(value[n:n + 2], 16) for n in (0, 2, 4))
        # Use existing color indices; do not modify the user's terminal palette.
        if curses.COLORS >= 256:
            palette[name] = 16 + 36 * round(rgb[0] / 255 * 5) + 6 * round(rgb[1] / 255 * 5) + round(rgb[2] / 255 * 5)
        else:
            palette[name] = fallback[name]
    for fg in COLORS:
        for bg in ("bg", "panel"):
            index = len(paint.pairs) + 1
            if index < curses.COLOR_PAIRS:
                curses.init_pair(index, palette[fg], palette[bg])
                paint.pairs[(fg, bg)] = index


def interact(screen, state, messages, live, recording, compact=False, control_file=None):
    setup_colors()
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    screen.timeout(100)
    cursor, offset, last_tick, paused = 0, 0, time.monotonic(), False
    end = max((m.get("received_at", m["data"]["at"]) - state.start for m in messages), default=0)
    while True:
        tick = time.monotonic()
        cue = ""
        previous = offset
        if not paused:
            offset += tick - last_tick
        if control_file:
            offset, cue = controlled_preview(control_file, now())
            if offset < previous:
                state = State(state.config, state.mode, state.start)
                cursor = 0
        last_tick = tick
        clock = now() if live else state.start + min(offset, end)
        if live:
            while True:
                try:
                    msg = live.messages.get_nowait()
                except queue.Empty:
                    break
                if "transport_error" in msg:
                    state.transport_error = msg["transport_error"]
                    continue
                state.apply(msg)
                if recording:
                    recording.append(msg)
        else:
            while cursor < len(messages) and messages[cursor].get("received_at", messages[cursor]["data"]["at"]) <= clock:
                state.apply(messages[cursor])
                cursor += 1
        height, width = screen.getmaxyx()
        canvas = render_compact(state, width, height, clock, paused, not live and offset >= end, cue) if compact else render(state, width, height, clock, paused, not live and offset >= end)
        paint(screen, canvas)
        key = screen.getch()
        if key in (ord("q"), 27):
            return
        if key == ord(" ") and not live and not control_file:
            paused = not paused
        if key == ord("r") and not live and not control_file:
            state = State(state.config, state.mode, state.start)
            cursor, offset, paused = 0, 0, False
        if key == ord("m") and live:
            msg = {"source": "marker", "data": {"at": now(), "text": "Presenter marker (no action performed)"}, "received_at": now()}
            state.apply(msg)
            if recording:
                recording.append(msg)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preview", action="store_true", help="Illustrative A → B → A; no Docker or Kubernetes needed")
    mode.add_argument("--live", action="store_true", help="Read existing clusters and the prepared observer")
    mode.add_argument("--replay", type=Path, help="Replay an actual --record JSONL capture")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--record", type=Path, help="New JSONL path; live mode only")
    parser.add_argument("--snapshot", action="store_true", help="Print one frame (preview/replay only)")
    parser.add_argument("--at", type=float, default=18, help="Seconds into the snapshot")
    parser.add_argument("--width", type=int, default=132)
    parser.add_argument("--height", type=int, default=44)
    parser.add_argument("--svg", type=Path, help="Also export snapshot character grid as SVG")
    parser.add_argument("--compact", action="store_true", help="Narrow traffic view for the right tmux pane")
    parser.add_argument("--control-file", type=Path, help="Follow Enter-driven preview cues from the presenter")
    args = parser.parse_args()
    if args.record and not args.live:
        parser.error("--record requires --live; illustrative data cannot be recorded as evidence")
    if args.snapshot and args.live:
        parser.error("Use the live full-screen view; snapshots support preview/replay")
    if args.svg and not args.snapshot:
        parser.error("--svg requires --snapshot")
    if args.control_file and (not args.preview or args.snapshot):
        parser.error("--control-file requires interactive --preview")
    if args.replay:
        header, messages = replay_file(args.replay)
        cfg, start, label = header["config"], header["start"], "RECORDED REPLAY"
    else:
        path = args.config or HERE / (".runtime/config.json" if args.live else "config.json")
        if args.live and not path.exists():
            raise ValueError("Live configuration missing. Prepare the local playground, then run python3 demo/prepare.py")
        cfg = load_config(path)
        if args.preview:
            start, messages = preview_messages(cfg)
            label = "ILLUSTRATIVE PREVIEW"
        else:
            start, messages, label = now(), [], "LIVE"
    state = State(cfg, label, start)
    if args.snapshot:
        clock = start + max(0, args.at)
        for message in messages:
            if message.get("received_at", message["data"]["at"]) <= clock:
                state.apply(message)
        renderer = render_compact if args.compact else render
        canvas = renderer(state, max(40, args.width), max(15, args.height), clock)
        print(canvas.plain())
        if args.svg:
            args.svg.write_text(canvas.svg(), encoding="utf-8")
        return
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        parser.error("Full-screen mode needs a terminal; use --preview --snapshot for a text preview")
    live = Live(cfg, start) if args.live else None
    recording = None
    try:
        if args.record:
            recording = Recording(args.record, cfg, start)
        if live:
            live.launch()
        curses.wrapper(interact, state, messages, live, recording, args.compact, args.control_file)
    finally:
        if live:
            live.close()
        if recording:
            recording.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        sys.exit("Dashboard: " + clean(exc))
