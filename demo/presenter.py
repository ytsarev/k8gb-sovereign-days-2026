#!/usr/bin/env python3
"""Visible commands, short notes, and Enter-paced k8gb demonstration beats."""
import argparse
import copy
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import textwrap
import time

from core import addresses, clean, load_config, utc, valid

HERE = Path(__file__).resolve().parent
RUNTIME = HERE / ".runtime"
STATE = RUNTIME / "beat-state.json"
CONTROL = RUNTIME / "preview-control.json"


class StopDemo(Exception):
    pass


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n")
    temporary.replace(path)


def preview_config(cfg):
    cfg = copy.deepcopy(cfg)
    cfg["parent_address"] = "172.20.0.10"
    for key, address in (("A", "172.20.0.3"), ("B", "172.20.0.5")):
        cfg["clusters"][key]["dns_address"] = address
    return cfg


def kubectl(cfg, key, *args, request_timeout=5):
    return ["kubectl", "--context", cfg["clusters"][key]["context"],
            "--request-timeout=%ss" % request_timeout, "-n", cfg["namespace"], *args]


def dig(cfg, key, hostname=None, short=True):
    return ["docker", "exec", cfg["observer_container"], "dig",
            "@" + cfg["clusters"][key]["dns_address"], hostname or cfg["hostname"],
            "A", "+tcp", "+time=2", "+tries=1", "+norecurse",
            *(["+short"] if short else ["+noall", "+comments", "+answer"])]


def synthetic_dns(cfg, key, hostname=None, short=False):
    ip = cfg["clusters"][key]["dns_address"]
    if short:
        return ip + "\n"
    return (";; ->>HEADER<<- opcode: QUERY, status: NOERROR, id: 1\n"
            ";; flags: qr aa; QUERY: 1, ANSWER: 1, AUTHORITY: 0, ADDITIONAL: 0\n"
            ";; ANSWER SECTION:\n%s. 10 IN A %s\n" % (hostname or cfg["hostname"], ip))


def short_targets(output):
    # Empty/error answers cannot become a baseline. Only literal IPv4 targets
    # are accepted here; the observer separately validates full DNS responses.
    return sorted({str(ipaddress.IPv4Address(line.strip())) for line in output.splitlines() if line.strip()})


def deployment_view(data):
    return {"metadata": {k: data["metadata"][k] for k in ("name", "namespace") if k in data["metadata"]},
            "spec": {k: data["spec"][k] for k in ("replicas",) if k in data["spec"]},
            "status": {k: data.get("status", {})[k] for k in ("readyReplicas", "availableReplicas")
                       if k in data.get("status", {})}}


def gslb_view(data):
    return {"apiVersion": data["apiVersion"], "kind": data["kind"],
            "metadata": {k: data["metadata"][k] for k in ("name", "namespace")},
            "spec": data["spec"]}


def endpointslice_view(data):
    return {"items": [{"metadata": {"name": item["metadata"]["name"]},
                       "endpoints": [{"addresses": endpoint.get("addresses", []),
                                      "conditions": {k: v for k, v in endpoint.get("conditions", {}).items() if k == "ready"}}
                                     for endpoint in (item.get("endpoints") or [])]}
                      for item in data["items"]]}


def yaml_parser():
    try:
        import yaml
    except ImportError:
        raise RuntimeError("Live YAML reads need PyYAML. Run: python3 -m venv demo/.venv && "
                           "demo/.venv/bin/python -m pip install -r demo/requirements.txt; "
                           "then use ./demo/demo.sh") from None
    return yaml


def read_yaml(output):
    yaml = yaml_parser()
    try:
        data = yaml.safe_load(output)
    except yaml.YAMLError as exc:
        raise ValueError("Invalid kubectl YAML: " + clean(exc)) from exc
    if not isinstance(data, dict):
        raise ValueError("Expected a Kubernetes object in the YAML response")
    return data


def yaml_scalar(value):
    """Emit JSON-compatible YAML scalars, quoting ambiguous strings."""
    if isinstance(value, str):
        reserved = {"null", "true", "false", "yes", "no", "on", "off", "y", "n"}
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_./-]*", value) and value.lower() not in reserved:
            return value
    return json.dumps(value, ensure_ascii=True, allow_nan=False)


def yaml_rows(data, indent=0):
    """Render selected resource fields as YAML tokens; no terminal or pip dependency."""
    if isinstance(data, dict) and data:
        entries = [(yaml_scalar(str(key)) + ":", value) for key, value in data.items()]
    elif isinstance(data, list) and data:
        entries = [("-", value) for value in data]
    else:
        yield [(" " * indent, ""), (yaml_scalar(data), "green" if isinstance(data, str) else "amber")]
        return
    for label, value in entries:
        prefix = [(" " * indent, ""), (label, "teal")]
        if isinstance(value, (dict, list)) and value:
            yield prefix
            yield from yaml_rows(value, indent + 2)
        else:
            yield prefix + [(" ", ""), (yaml_scalar(value), "green" if isinstance(value, str) else "amber")]


def converged(latest, cfg, target, target_ips, since, clock):
    """Require actual fresh authority, resolver and HTTP observations after the action."""
    for source in ("dns_A", "dns_B", "resolver", "request"):
        obs = latest.get(source)
        if not valid(obs, clock, cfg["stale_after"]) or obs["at"] < since:
            return False
        if source == "request":
            if not obs.get("success") or obs.get("served_by") != target:
                return False
        elif addresses(obs) != sorted(target_ips):
            return False
        elif source.startswith("dns_"):
            if cfg.get("require_authoritative_flag", True):
                if not obs.get("authoritative"):
                    return False
            elif obs.get("server") != cfg["clusters"][source[-1]]["dns_address"] or obs.get("port") != 53:
                return False
    return True


class Presenter:
    def __init__(self, cfg, args, recorder=None):
        self.cfg, self.args = cfg, args
        self.recorder = recorder
        self.capture_error = None
        self.original = None
        self.uid = None
        self.pending = None
        self.targets = {}
        self.restored = False
        self.action_at = time.time()
        self.color = sys.stdout.isatty() and "NO_COLOR" not in os.environ

    def styled(self, text, color="", bold=False):
        codes = {"blue": "94", "teal": "96", "red": "91", "green": "92", "amber": "93", "muted": "2"}
        prefix = "\033[%sm" % ("1;" + codes.get(color, "0") if bold else codes.get(color, "0")) if self.color else ""
        return prefix + text + ("\033[0m" if self.color else "")

    def record(self, kind, **fields):
        if self.recorder:
            try:
                self.recorder.append(kind, **fields)
            except OSError as exc:
                # A full disk must never prevent the live finally block from
                # restoring replicas. The incomplete capture cannot be replayed.
                self.capture_error, self.recorder = exc, None
                print("Command capture failed; continuing through restoration: " + clean(exc), file=sys.stderr)

    def line(self, text="", color="", bold=False, capture=True):
        if capture:
            self.record("line", text=clean(text, 4000), color=color, bold=bold)
        print(self.styled(text, color, bold), flush=True)

    def pause(self, label="Enter to run · q to stop"):
        self.record("pause", label=label)
        if self.args.auto is not None:
            time.sleep(self.args.auto)
            self.record("resume")
            return
        self.line("  ↪ " + label, "muted", capture=False)
        answer = sys.stdin.readline()
        if not answer or answer.strip().lower() == "q":
            raise StopDemo()
        while answer.strip():
            self.line("  Press Enter, or q to stop.", "muted", capture=False)
            answer = sys.stdin.readline()
            if not answer or answer.strip().lower() == "q":
                raise StopDemo()
        self.record("resume")

    def beat(self, number, title, note):
        if number > 1:
            self.pause("Enter for beat %s: %s · q to stop" % (number, title))
        self.record("beat", number=number, title=title, note=note)
        if self.color and not self.args.no_clear:
            print("\033[2J\033[H", end="")
        self.line(capture=False)
        self.line("─" * min(72, shutil.get_terminal_size().columns - 2), "teal", capture=False)
        self.line("  %s / 7   %s" % (number, title.upper()), "teal", True, capture=False)
        if self.args.preview:
            self.line("  PREVIEW · illustrative output · no commands executed", "amber")
        self.line(capture=False)
        if not self.args.no_notes:
            for line in textwrap.wrap(note, max(30, min(76, shutil.get_terminal_size().columns - 6))):
                self.line("  " + line, "muted", capture=False)
            self.line(capture=False)

    def show(self, argv):
        self.record("command", argv=[str(arg) for arg in argv])
        width = max(38, shutil.get_terminal_size().columns - 5)
        # Wrap between argv tokens only: copying the command preserves its quoting.
        lines, line = [], "$"
        for arg in argv:
            token = shlex.quote(str(arg))
            if len(line) + len(token) + 1 > width and line != "$":
                lines.append(line + " \\")
                line = "   " + token
            else:
                line += " " + token
        lines.append(line)
        for line in lines:
            self.line(line, "blue", True, capture=False)

    def command(self, argv, fixture="", view=None, pause=True, timeout=15, display=True):
        if display:
            self.show(argv)
        if pause:
            self.pause()
        if self.args.preview:
            output = fixture if isinstance(fixture, str) else json.dumps(fixture)
        else:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
            output = result.stdout
            if result.returncode:
                raise RuntimeError(clean(result.stderr or output or "Command failed", 1200))
        if view:
            data = fixture if self.args.preview else read_yaml(output)
            selected = view(data)
            self.line("  Selected YAML fields:", "muted")
            self.record("yaml", data=selected)
            for row in yaml_rows(selected):
                self.line("".join(self.styled(text, color) for text, color in row), capture=False)
        else:
            # Preserve line breaks, remove terminal escapes from external output.
            for line in output.rstrip().splitlines():
                self.line(clean(line.expandtabs(4), 4000))
        self.line()
        return data if view else output

    def cue(self, offset, until, beat):
        if self.args.preview:
            save_json(self.args.control_file, {"offset": offset, "until": until,
                      "at": time.time(), "beat": beat})

    def read_deployment(self, key, pause=True):
        sample = {"metadata": {"name": self.cfg["deployment"], "namespace": self.cfg["namespace"], "uid": "preview-" + key},
                  "spec": {"replicas": 1}, "status": {"readyReplicas": 1}}
        return self.command(kubectl(self.cfg, key, "get", "deployment", self.cfg["deployment"],
                              "-o", "yaml"), sample, deployment_view, pause=pause)

    def baseline(self):
        self.beat(1, "Two clusters. Two locations.",
                  "A is the preferred location; B runs the same service. Local BIND9 delegates the application zone to their CoreDNS servers.")
        self.cue(0, 8, "1 / BASELINE")
        for key in ("A", "B"):
            dep = self.read_deployment(key)
            desired = dep["spec"].get("replicas", 1)
            ready = dep.get("status", {}).get("readyReplicas", 0)
            if desired < 1 or ready != desired:
                raise ValueError("Cluster %s must start with all desired replicas ready" % key)
            if key == "A":
                self.original, self.uid = desired, dep["metadata"]["uid"]
        self.line("  Original A replica count: %s (restored at the end)" % self.original, "green")

    def steering(self):
        self.beat(2, "Traffic steering is a Kubernetes resource",
                  "Both clusters use failover with A's geotag as primary. Kubernetes readiness determines which application targets remain eligible.")
        for key in ("A", "B"):
            sample = {"apiVersion": "k8gb.io/v1beta1", "kind": "Gslb",
                      "metadata": {"name": self.cfg["gslb"], "namespace": self.cfg["namespace"]},
                      "spec": {"resourceRef": {"apiVersion": "networking.k8s.io/v1", "kind": "Ingress",
                                              "name": self.cfg["gslb"]},
                               "strategy": {"type": "failover", "primaryGeoTag": self.cfg["clusters"]["A"]["geotag"]}},
                      "status": {"geoTag": self.cfg["clusters"][key]["geotag"], "hosts": self.cfg["hostname"]}}
            data = self.command(kubectl(self.cfg, key, "get", "gslbs.k8gb.io", self.cfg["gslb"],
                                    "-o", "yaml"), sample, gslb_view)
            strategy = data["spec"]["strategy"]
            if strategy.get("type") != "failover" or strategy.get("primaryGeoTag") != self.cfg["clusters"]["A"]["geotag"]:
                raise ValueError("Unexpected steering in cluster " + key)
            status = data.get("status", {})
            if status.get("geoTag") != self.cfg["clusters"][key]["geotag"] or self.cfg["hostname"] not in [h.strip() for h in status.get("hosts", "").split(",")]:
                raise ValueError("Gslb hostname/geotag does not match cluster " + key)

    def dns(self):
        self.beat(3, "Follow the name",
                  "The parent delegates to both CoreDNS servers. Before failure, both should return A's IPs for the same hostname. Then inspect each cluster's local targets.")
        parent = ["docker", "exec", self.cfg["observer_container"], "dig", "@" + self.cfg["parent_address"],
                  "-p", str(self.cfg["parent_port"]), self.cfg["hostname"], "A", "+tcp", "+time=2",
                  "+tries=1", "+norecurse", "+noall", "+authority", "+additional"]
        self.command(parent, "cloud.example.com. 30 IN NS ns-eu.cloud.example.com.\ncloud.example.com. 30 IN NS ns-us.cloud.example.com.")
        for key in ("A", "B"):
            self.read_dns(key, "A")
        for key in ("A", "B"):
            hostname = "localtargets." + self.cfg["hostname"]
            output = self.read_dns(key, key, hostname)
            self.targets[key] = short_targets(output)
            if not self.targets[key]:
                raise ValueError("No ready application IPs in cluster " + key)
        if self.targets["A"] == self.targets["B"]:
            raise ValueError("This demo needs distinct application targets for A and B")
        self.wait_for("A", self.action_at)

    def read_dns(self, key, preview_target, hostname=None):
        self.line("  Cluster %s · %s" % (key, "local application targets" if hostname else "DNS answer"), "muted")
        output = self.command(dig(self.cfg, key, hostname), synthetic_dns(self.cfg, preview_target, hostname, short=True))
        if not output.strip():
            self.line("  No A records returned.", "amber")
        return output

    def failover(self):
        self.beat(4, "Remove A's ready application targets",
                  "Scale only the demo application to zero. A's Kubernetes API and DNS stay running. Watch readiness, DNS publication, then the client.")
        current = self.read_deployment("A")
        if current["metadata"]["uid"] != self.uid or current["spec"].get("replicas", 1) != self.original:
            raise ValueError("A changed since baseline; restart from a healthy baseline")
        argv = kubectl(self.cfg, "A", "scale", "deployment/" + self.cfg["deployment"],
                       "--current-replicas=" + str(self.original), "--replicas=0")
        self.show(argv)
        self.pause("Enter to scale the demo application to zero · q to stop")
        self.pending = {"config": self.cfg, "uid": self.uid, "replicas": self.original, "started_at": time.time()}
        if not self.args.preview:
            save_json(STATE, self.pending)  # Persist before mutation, including an uncertain command result.
        self.action_at = time.time()
        self.command(argv, "deployment.apps/%s scaled" % self.cfg["deployment"], pause=False, display=False)
        self.cue(10, 30, "4 / FAILOVER")

    def observe(self):
        self.beat(5, "Follow the traffic to B",
                  "A new DNS answer and a recovered client are separate observations. Cached answers can briefly retain A; keep any failed requests visible.")
        self.command(kubectl(self.cfg, "A", "get", "endpointslices", "-l", "kubernetes.io/service-name=" + self.cfg["service"],
                            "-o", "yaml"), {"apiVersion": "v1", "kind": "List", "items": [
                                {"metadata": {"name": "frontend-podinfo-demo"}, "endpoints": None}]}, endpointslice_view)
        for key in ("A", "B"):
            self.read_dns(key, "B")
        self.wait_for("B", self.action_at)

    def wait_for(self, target, since):
        argv = ["docker", "logs", "--since", utc(since), "--tail", "200", self.cfg["observer_container"]]
        self.show(argv)
        self.line("  Polling these observations: both authorities + resolver + HTTP must agree on %s." % target, "muted")
        self.pause("Enter to verify the observed transition · q to stop")
        if self.args.preview:
            self.cue(26 if target == "B" else 46 if self.restored else 8,
                     30 if target == "B" else 48 if self.restored else 8, "5 / TRAFFIC ON B" if target == "B" else "TRAFFIC ON A")
            self.line("  PREVIEW: authorities → %s; resolver → %s; HTTP 200 from %s" % (target, target, target), "amber")
            return
        deadline = time.monotonic() + self.args.timeout
        latest = {}
        while time.monotonic() < deadline:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=8)
            if result.returncode:
                raise RuntimeError(clean(result.stderr or "Observer log read failed"))
            for line in result.stdout.splitlines():
                try:
                    msg = json.loads(line)
                    source, data = msg["source"], msg["data"]
                    if source not in latest or data["at"] > latest[source]["at"]:
                        latest[source] = data
                except (ValueError, KeyError, TypeError):
                    continue
            if converged(latest, self.cfg, target, self.targets[target], since, time.time()):
                self.line("  Verified: both authorities + resolver → %s; HTTP %s from %s." % (target, latest["request"].get("status", "?"), target), "green")
                return
            self.line("  Waiting for fresh observations on %s… %ss remaining" % (target, max(0, int(deadline - time.monotonic()))), "muted")
            time.sleep(2)
        raise RuntimeError("No verified transition to %s within %ss" % (target, self.args.timeout))

    def restore(self, emergency=False):
        if self.pending is None:
            return
        saved = self.pending
        cfg = saved["config"]
        # Always use the saved identity/context, including ./demo.sh reset after a restart.
        if not self.args.preview:
            data = self.command(kubectl(cfg, "A", "get", "deployment", cfg["deployment"], "-o", "yaml"),
                                view=deployment_view, pause=False)
            if data["metadata"]["uid"] != saved["uid"]:
                raise ValueError("Deployment was replaced; refusing to restore a different object")
            replicas = data["spec"].get("replicas", 1)
            if replicas not in (0, saved["replicas"]):
                raise ValueError("Replica count changed outside the demo; inspect it before restoring")
        else:
            replicas = 0
        self.action_at = time.time()
        self.command(kubectl(cfg, "A", "scale", "deployment/" + cfg["deployment"],
                            "--current-replicas=" + str(replicas), "--replicas=" + str(saved["replicas"])),
                     "deployment.apps/%s scaled" % cfg["deployment"], pause=not emergency)
        self.cue(32, 48, "6 / RESTORE")
        self.command(kubectl(cfg, "A", "rollout", "status", "deployment/" + cfg["deployment"],
                            "--timeout=%ss" % self.args.timeout, request_timeout=self.args.timeout + 5), "deployment successfully rolled out",
                     pause=False, timeout=self.args.timeout + 10)
        if not self.args.preview:
            STATE.unlink()
        self.restored = True
        self.pending = None

    def run(self):
        if STATE.exists() and not self.args.preview:
            raise ValueError("An unfinished run exists. Run ./demo/demo.sh reset before starting again.")
        try:
            self.baseline()
            self.steering()
            self.dns()
            self.failover()
            self.observe()
            self.beat(6, "Restore A", "Restore the original replica count. Readiness returns before we expect DNS and the client to prefer A again.")
            self.restore()
            self.beat(7, "Verify the return to A", "Check the actual application response as well as both DNS authorities. The hostname never changed.")
            for key in ("A", "B"):
                self.read_dns(key, "A")
            self.wait_for("A", self.action_at)
            self.cue(46, 48, "7 / RESTORED")
            self.line("  A → B → A complete. Original replicas restored.", "green", True)
            self.record("complete")
            if self.capture_error:
                raise RuntimeError("Command recording is incomplete: " + clean(self.capture_error))
        finally:
            if self.pending:
                self.line("  Stopping: restoring the demo application's original replicas.", "amber")
                try:
                    self.restore(emergency=True)
                except (Exception, KeyboardInterrupt) as exc:
                    self.line("  Restore incomplete: " + clean(exc), "red")
                    self.line("  Recovery state retained. Run ./demo/demo.sh reset.", "amber")
                    raise


def stage(args):
    if not shutil.which("tmux"):
        raise ValueError("tmux is not installed. Use two terminals: ./demo/demo.sh and ./demo/demo.sh traffic (add --preview to both for rehearsal).")
    size = shutil.get_terminal_size((160, 44))
    if size.columns < 124 or size.lines < 34:
        raise ValueError("Use a terminal at least 124 columns × 34 rows; recommended 160 × 44.")
    session = "k8gb-sovereign-%s" % os.getpid()
    config = args.config or HERE / ("config.json" if args.preview else ".runtime/config.json")
    if not config.is_file():
        raise ValueError("Prepare the playground first: python3 demo/prepare.py (or use --preview).")
    load_config(config)
    if args.record and args.record.exists():
        raise ValueError("Recording already exists; choose a new --record filename.")
    common = (["--preview"] if args.preview else []) + (["--config", str(args.config)] if args.config else [])
    control = args.control_file.resolve()
    if args.preview:
        save_json(control, {"offset": 0, "until": 0, "at": time.time(), "beat": "WAITING FOR ENTER"})
    common += ["--control-file", str(control)]
    left = [sys.executable, "-u", str(HERE / "presenter.py"), "run", *common, "--timeout", str(args.timeout)]
    if args.no_notes:
        left.append("--no-notes")
    if args.no_clear:
        left.append("--no-clear")
    right = [sys.executable, "-u", str(HERE / "presenter.py"), "traffic", *common]
    if args.record:
        right.extend(["--record", str(args.record.resolve())])
    # An existing tmux server can retain a different environment. Pass the
    # selected kubeconfig explicitly to both new panes, without changing it globally.
    if os.environ.get("KUBECONFIG"):
        prefix = ["env", "KUBECONFIG=" + os.environ["KUBECONFIG"]]
        left, right = prefix + left, prefix + right
    def tmux(*parts):
        return subprocess.check_output(["tmux", *parts], text=True).strip()
    created = False
    try:
        left_pane = tmux("new-session", "-d", "-P", "-F", "#{pane_id}", "-s", session, "-n", "demo", "-x", str(size.columns), "-y", str(size.lines),
                         "-c", str(HERE.parent), "exec " + shlex.join(left))
        created = True
        tmux("set-window-option", "-t", session + ":demo", "remain-on-exit", "on")
        tmux("set-window-option", "-t", session + ":demo", "remain-on-exit-format",
             "#{?pane_dead_status,Command exited with status #{pane_dead_status}.,}")
        right_pane = tmux("split-window", "-h", "-P", "-F", "#{pane_id}", "-p", "44", "-t", left_pane,
                          "-c", str(HERE.parent), "exec " + shlex.join(right))
        tmux("set-window-option", "-t", session + ":demo", "pane-border-status", "top")
        tmux("set-window-option", "-t", session + ":demo", "pane-border-format", " #{pane_title} ")
        tmux("select-pane", "-t", left_pane, "-T", "COMMANDS · Enter to advance")
        tmux("select-pane", "-t", right_pane, "-T", "TRAFFIC · live observations" if not args.preview else "TRAFFIC · illustrative preview")
        tmux("select-pane", "-t", left_pane)
        if args.detach:
            print("tmux attach-session -t " + session)
        else:
            subprocess.run(["tmux", "switch-client" if os.environ.get("TMUX") else "attach-session", "-t", session], check=True)
    except BaseException:
        if created:
            subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True)
        raise


def main():
    parser = argparse.ArgumentParser(prog="./demo/demo.sh", description=__doc__,
        epilog="Automated setup: ./demo/demo.sh up (prepare + tmux), setup (prepare only), or rehearse (recorded A → B → A). Use up --help for setup options. Enter-driven offline fallback: ./demo/demo.sh offline. Replay a captured dashboard: ./demo/demo.sh replay <recording.jsonl> [--compact].")
    parser.add_argument("command", nargs="?", default="run", choices=("run", "stage", "traffic", "status", "reset"))
    parser.add_argument("--preview", action="store_true", help="Enter-paced illustrative rehearsal; never execute Kubernetes/Docker commands")
    parser.add_argument("--config", type=Path, help="Prepared live configuration, or base config for preview")
    parser.add_argument("--no-notes", action="store_true", help="Hide the short presenter notes")
    parser.add_argument("--no-clear", action="store_true", help="Keep all beats in the terminal transcript")
    parser.add_argument("--auto", type=float, help="Seconds between actions, for unattended rehearsal")
    parser.add_argument("--timeout", type=int, default=90, help="Bound on each transition/rollout wait")
    parser.add_argument("--control-file", type=Path, default=CONTROL, help=argparse.SUPPRESS)
    parser.add_argument("--record", type=Path, help="Record live traffic observations (stage/traffic only)")
    parser.add_argument("--detach", action="store_true", help="Create tmux panes without attaching")
    parser.add_argument("--transcript", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--capture-start", type=float, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.config:
        args.config = args.config.resolve()
    if args.auto is not None and (not 0 <= args.auto <= 60):
        parser.error("--auto must be between 0 and 60 seconds")
    if args.timeout < 1 or args.timeout > 600:
        parser.error("--timeout must be between 1 and 600 seconds")
    if args.record and (args.preview or args.command not in ("stage", "traffic")):
        parser.error("--record needs live stage or traffic mode")
    if args.transcript and (args.preview or args.command != "run" or args.capture_start is None):
        parser.error("Command capture requires live run mode and --capture-start")
    if args.command == "stage" and args.auto is not None:
        parser.error("Use --auto with run; stage is Enter-driven")
    if not args.preview and args.command in ("run", "stage", "status", "reset"):
        yaml_parser()  # Fail before starting a live presentation or changing workloads.
    if args.command == "traffic":
        argv = [sys.executable, str(HERE / "dashboard.py"), "--compact", "--preview" if args.preview else "--live"]
        if args.preview:
            argv.extend(["--control-file", str(args.control_file)])
        if args.config:
            argv.extend(["--config", str(args.config)])
        if args.record:
            args.record.parent.mkdir(parents=True, exist_ok=True)
            argv.extend(["--record", str(args.record)])
        os.execv(sys.executable, argv)
    if args.command == "stage":
        stage(args)
        return
    path = args.config or HERE / ("config.json" if args.preview else ".runtime/config.json")
    if args.command == "reset":
        if args.preview:
            save_json(args.control_file, {"offset": 0, "until": 0, "at": time.time(), "beat": "RESET"})
            return
        if not STATE.exists():
            print("No unfinished demo mutation to restore.")
            return
    elif not path.exists():
        raise ValueError("Prepare the playground first: python3 demo/prepare.py (or use --preview).")
    RUNTIME.mkdir(exist_ok=True)
    # Preview and live runs cannot overwrite each other's recovery record.
    lock = RUNTIME / ("preview.lock" if args.preview else "beats.lock")
    with lock.open("a") as held:
        try:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another presenter is running; stop it before starting/resetting this run")
        saved = json.loads(STATE.read_text()) if args.command == "reset" else None
        cfg = saved["config"] if saved else load_config(path)
        from transcript import Transcript
        recorder = Transcript(args.transcript, args.capture_start) if args.transcript else None
        try:
            presenter = Presenter(preview_config(cfg) if args.preview else cfg, args, recorder)
            if args.command == "reset":
                presenter.pending = saved
                presenter.restore()
            elif args.command == "status":
                presenter.args.auto = 0
                presenter.baseline()
                presenter.steering()
            else:
                presenter.run()
        finally:
            if recorder:
                recorder.close()


if __name__ == "__main__":
    def stop_on_signal(*_):
        raise StopDemo()
    signal.signal(signal.SIGTERM, stop_on_signal)
    try:
        main()
    except (StopDemo, KeyboardInterrupt):
        print("\nDemo stopped.")
        sys.exit(130)
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
        sys.exit("Demo: " + clean(exc, 1200))
