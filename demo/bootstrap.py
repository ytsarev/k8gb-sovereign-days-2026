#!/usr/bin/env python3
"""Provision the local playground, verify real traffic, and present or rehearse it."""
import argparse
import contextlib
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import queue
import shlex
import shutil
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
RUNTIME = HERE / ".runtime"
SOURCE = RUNTIME / "k8gb-source"
COMMIT = "330d33335981b9716bc8ca245660d90893eae272"
VERSION = "v1.0.1"
PODINFO_VERSION = "6.15.0"
CLUSTERS = (("edgedns", "edge-dns"), ("test-gslb1", "test-gslb1"), ("test-gslb2", "test-gslb2"))
OWNER = hashlib.sha256(str(HERE).encode()).hexdigest()[:16]
LABEL = "k8gb-sovereign-demo"


def step(text):
    print("\n>>> " + text, flush=True)


def run(argv, cwd=None, timeout=600):
    """Bound the entire process group, including Make/Helm children."""
    print("$ " + shlex.join([str(a) for a in argv]), flush=True)
    with (RUNTIME / "setup.log").open("a") as log:
        log.write("\n$ " + shlex.join([str(a) for a in argv]) + "\n")
        log.flush()
        p = subprocess.Popen(argv, cwd=cwd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(argv, timeout)
                try:
                    code = p.wait(timeout=min(30, remaining))
                    break
                except subprocess.TimeoutExpired:
                    print("  Still running; details: " + str(RUNTIME / "setup.log"), flush=True)
        except BaseException:
            if p.poll() is None:
                os.killpg(p.pid, signal.SIGTERM)
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
                p.wait()
            raise
        if code:
            raise RuntimeError("Command failed (%s). See %s" % (code, RUNTIME / "setup.log"))


def capture(argv, timeout=30):
    # Kubeconfig and Docker inspection can contain credentials: never log stdout.
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError("Command failed: " + shlex.join(argv) + "\n" + result.stderr[-1500:])
    return result.stdout


@contextlib.contextmanager
def lock(name):
    with (RUNTIME / name).open("a") as held:
        try:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another setup/presenter is running; finish it before starting setup")
        yield


def ensure_python():
    python = HERE / ".venv/bin/python"
    if not python.exists():
        step("Create the demo's Python environment")
        run([sys.executable, "-m", "venv", str(HERE / ".venv")])
    check = subprocess.run([str(python), "-c", "import yaml; assert yaml.__version__ == '6.0.3'"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if check.returncode:
        step("Install the pinned YAML dependency")
        run([str(python), "-m", "pip", "install", "-r", str(HERE / "requirements.txt")])
    if Path(sys.prefix).resolve() != (HERE / ".venv").resolve():
        os.execv(str(python), [str(python), "-u", str(Path(__file__).resolve()), *sys.argv[1:]])


def ensure_source():
    if not SOURCE.exists():
        step("Download the pinned k8gb playground")
        run(["git", "clone", "https://github.com/k8gb-io/k8gb.git", str(SOURCE)])
        run(["git", "checkout", "--detach", COMMIT], cwd=SOURCE)
    head = capture(["git", "-C", str(SOURCE), "rev-parse", "HEAD"]).strip()
    if head != COMMIT:
        raise ValueError("Upstream checkout is at a different commit; refusing to reset it: " + str(SOURCE))


def cluster_owned(info):
    if (info["Config"].get("Labels") or {}).get(LABEL) != OWNER:
        raise ValueError("A cluster with a demo name already exists outside this checkout; refusing to adopt it")
    return info["Id"]


def ensure_watch_limits(container):
    # k3d nodes share the Linux Docker host's inotify quota. Extra clusters can
    # exhaust the common default of 128 and prevent containerd's CRI from loading.
    for name, minimum in (("fs.inotify.max_user_instances", 8192), ("fs.inotify.max_user_watches", 1048576)):
        current = int(capture(["docker", "exec", container, "sysctl", "-n", name]).strip())
        if current < minimum:
            step("Increase Docker host %s: %s → %s" % (name, current, minimum))
            run(["docker", "exec", container, "sysctl", "-w", name + "=" + str(minimum)])


def wait_nodes(name, count):
    step("Wait for %s ready node(s) in %s" % (count, name))
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        try:
            nodes = json.loads(capture(["kubectl", "--context", "k3d-" + name, "--request-timeout=5s",
                                        "get", "nodes", "-o", "json"], timeout=8))["items"]
            if len(nodes) == count and all(any(c["type"] == "Ready" and c["status"] == "True"
                    for c in node.get("status", {}).get("conditions", [])) for node in nodes):
                return
        except (RuntimeError, subprocess.TimeoutExpired):
            pass
        time.sleep(2)
    raise RuntimeError("Kubernetes nodes did not become ready in " + name)


def ensure_clusters():
    names = {c["name"] for c in json.loads(capture(["k3d", "cluster", "list", "-o", "json"]))}
    ids = {}
    # Validate every collision before creating or starting anything.
    for name, _ in CLUSTERS:
        if name in names:
            info = json.loads(capture(["docker", "inspect", "k3d-" + name + "-server-0"]))[0]
            cluster_owned(info)
    for name, filename in CLUSTERS:
        if name not in names:
            step("Create " + name)
            run(["k3d", "cluster", "create", "-c", str(SOURCE / "k3d" / (filename + ".yaml")),
                 "--kubeconfig-update-default=false", "--kubeconfig-switch-context=false",
                 "--runtime-label", LABEL + "=" + OWNER + "@server:*",
                 "--timeout", "300s"], timeout=360)
        else:
            step("Reuse " + name)
            run(["k3d", "cluster", "start", name, "--wait", "--timeout", "180s"], timeout=210)
        info = json.loads(capture(["docker", "inspect", "k3d-" + name + "-server-0"]))[0]
        ids[name] = cluster_owned(info)
        if name == "edgedns":
            ensure_watch_limits("k3d-edgedns-server-0")
            try:
                capture(["docker", "exec", "k3d-edgedns-server-0", "crictl", "info"])
            except RuntimeError:
                # The API server can start even if containerd exhausted inotify
                # during the first boot. Restart this owned node after tuning.
                step("Restart the parent node's container runtime")
                run(["docker", "restart", "k3d-edgedns-server-0"], timeout=60)
    import yaml
    # k3d emits multiple YAML documents, not a merged Kubernetes config.
    configs = [yaml.safe_load(capture(["k3d", "kubeconfig", "get", name])) for name, _ in CLUSTERS]
    merged = {"apiVersion": "v1", "kind": "Config", "current-context": "k3d-test-gslb1"}
    for field in ("clusters", "contexts", "users"):
        merged[field] = [item for config in configs for item in config[field]]
    config = yaml.safe_dump(merged, sort_keys=False)
    path = RUNTIME / "kubeconfig"
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(config)
    path.chmod(0o600)
    os.environ["KUBECONFIG"] = str(path)
    for name, _ in CLUSTERS:
        wait_nodes(name, 1 if name == "edgedns" else 2)
    return ids


def deploy(ids, refresh=False):
    marker = RUNTIME / "deployed.json"
    expected = {"commit": COMMIT, "version": VERSION, "clusters": ids, "recipe": 3}
    if not refresh and marker.exists() and json.loads(marker.read_text()) == expected:
        step("Use the installed playground; readiness and traffic will be checked again")
        return
    step("Install BIND9, k8gb, ingress controllers, and demo applications")
    # Use the upstream manifests/chart without installing unrelated Istio,
    # Gateway API, migration, and multi-service examples from the full playground.
    values = RUNTIME / "values.yaml"
    values.write_text("{}\n")
    import yaml
    parent = yaml.safe_load((SOURCE / "deploy/edge/deployment.yaml").read_text())
    bind = next(c for c in parent["spec"]["template"]["spec"]["containers"] if c["name"] == "bind")
    bind["startupProbe"] = {"tcpSocket": {"port": 1053}, "periodSeconds": 2, "failureThreshold": 90}
    bind["readinessProbe"] = {"tcpSocket": {"port": 1053}, "periodSeconds": 2, "timeoutSeconds": 1}
    manifest = RUNTIME / "bind-deployment.yaml"
    manifest.write_text(yaml.safe_dump(parent))
    run(["kubectl", "--context", "k3d-edgedns", "apply",
         "-f", str(SOURCE / "deploy/edge/ddns-secret.yaml"), "-f", str(SOURCE / "deploy/edge/zone.yaml"),
         "-f", str(SOURCE / "deploy/edge/service.yaml"), "-f", str(manifest)])
    run(["kubectl", "--context", "k3d-edgedns", "rollout", "status", "deployment/edge", "--timeout=180s"], timeout=190)
    run(["helm", "repo", "add", "--force-update", "nginx-stable", "https://kubernetes.github.io/ingress-nginx"])
    run(["helm", "repo", "add", "--force-update", "podinfo", "https://stefanprodan.github.io/podinfo"])
    for number, tag in ((1, "eu"), (2, "us")):
        context = "k3d-test-gslb" + str(number)
        # This affects only the generated demo kubeconfig.
        run(["kubectl", "config", "use-context", context])
        run(["helm", "upgrade", "--install", "nginx-ingress", "nginx-stable/ingress-nginx", "-n", "k8gb",
             "--create-namespace", "--version", "4.15.1", "-f", str(SOURCE / "deploy/ingress/nginx-ingress-values.yaml"),
             "--wait", "--timeout", "5m"], timeout=330)
        run(["make", "deploy-k8gb-with-helm", "CLUSTER_ID=" + str(number), "VERSION=" + VERSION,
             "CHART=./chart/k8gb", "VALUES_YAML=" + str(values)], cwd=SOURCE, timeout=900)
        run(["kubectl", "--context", context, "apply", "-f", str(SOURCE / "deploy/gslb/namespace_ingress.yaml")])
        run(["helm", "upgrade", "--install", "frontend", "podinfo/podinfo", "-n", "test-gslb",
             "--version", PODINFO_VERSION, "-f", str(SOURCE / "deploy/test-apps/podinfo/podinfo-values.yaml"),
             "--set-string", "image.repository=ghcr.io/stefanprodan/podinfo", "--set-string", "image.tag=" + PODINFO_VERSION,
             "--set-string", "ui.message=Ingress NGINX: " + tag, "--wait", "--timeout", "5m"], timeout=330)
        run(["kubectl", "--context", context, "apply", "-f",
             str(SOURCE / "deploy/gslb/k8gb.absa.oss_v1beta1_gslb_cr_failover_ingress_ref.yaml")])
    marker.write_text(json.dumps(expected, indent=2) + "\n")


def verify_and_prepare(timeout):
    from core import load_config
    from prepare import prepare
    from presenter import Presenter
    cfg = load_config(HERE / "config.json")
    # The pinned coredns-crd-plugin v0.7.0 uses SetReply without setting AA.
    # Preserve that observed flag; validate direct server identity and records.
    cfg["require_authoritative_flag"] = False
    step("Wait for both applications")
    for cluster in cfg["clusters"].values():
        run(["kubectl", "--context", cluster["context"], "-n", cfg["namespace"],
             "rollout", "status", "deployment/" + cfg["deployment"], "--timeout=180s"], timeout=190)
    step("Prepare the DNS resolver and traffic observer")
    deadline = time.monotonic() + timeout
    while True:
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                prepare(cfg, check_only=True)
            break
        except (RuntimeError, ValueError, KeyError) as exc:
            if time.monotonic() >= deadline:
                raise RuntimeError("Application/Gslb readiness did not converge: " + str(exc)) from exc
            print("  Waiting for application/Gslb readiness: " + str(exc), flush=True)
            time.sleep(3)
    cfg = prepare(cfg, reuse=True)
    args = SimpleNamespace(preview=False, auto=0, no_clear=True, no_notes=False,
                           timeout=timeout, control_file=RUNTIME / "preview-control.json")
    step("Verify both authorities, cached DNS, and a real HTTP response from A")
    p = Presenter(cfg, args)
    p.baseline()
    p.steering()
    wait_targets(cfg, timeout)
    p.dns()
    return cfg


def wait_targets(cfg, timeout):
    from core import addresses, parse_dig
    from presenter import dig
    step("Wait for parent DNS and both clusters' local records")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            parent = capture(["docker", "exec", cfg["observer_container"], "dig", "@" + cfg["parent_address"],
                              "-p", str(cfg["parent_port"]), cfg["parent_zone"], "SOA", "+tcp", "+time=2",
                              "+tries=1", "+norecurse", "+noall", "+comments", "+answer"], timeout=8)
            parse_dig(parent, authoritative=True)
            targets = []
            for key in ("A", "B"):
                answer = capture(dig(cfg, key, "localtargets." + cfg["hostname"], short=False), timeout=8)
                targets.append(addresses({"ok": True, **parse_dig(answer, cfg.get("require_authoritative_flag", True))}))
            if all(targets) and targets[0] != targets[1]:
                return
        except (RuntimeError, ValueError, subprocess.TimeoutExpired):
            pass  # Gslb status can be ready before CoreDNS imports the new zone.
        time.sleep(3)
    raise RuntimeError("Parent DNS or the clusters' distinct, nonempty local target records did not become ready")


def recording_path(path):
    path = path or HERE / "recordings" / (time.strftime("%Y%m%d-%H%M%S") + "-%s.jsonl" % os.getpid())
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise ValueError("Recording exists; choose a new filename: " + str(path))
    return path


def rehearse(cfg, path, timeout):
    from dashboard import Live, Recording
    step("Run and record the complete A → B → A rehearsal")
    start = time.time()
    recorder = Recording(path, cfg, start)
    live = Live(cfg, start)
    process = None
    try:
        live.launch()
        process = subprocess.Popen([sys.executable, "-u", str(HERE / "presenter.py"), "run",
                                    "--auto", "0", "--no-clear", "--timeout", str(timeout)], start_new_session=True)
        while process.poll() is None or not live.messages.empty():
            try:
                message = live.messages.get(timeout=0.25)
            except queue.Empty:
                continue
            if "transport_error" in message:
                raise RuntimeError(message["transport_error"])
            recorder.append(message)
        if process.returncode:
            raise RuntimeError("Rehearsal failed; inspect the transcript and recording. Recovery state is retained if restoration failed.")
    finally:
        if process and process.poll() is None:
            # Give the presenter's finally block time to restore A.
            os.killpg(process.pid, signal.SIGINT)
            process.wait(timeout=timeout + 30)
        live.close()
        recorder.close()
    print("\nRehearsal passed. Recording: " + str(path))


def main():
    parser = argparse.ArgumentParser(prog="./demo/demo.sh", description=__doc__)
    parser.add_argument("command", choices=("up", "setup", "rehearse"))
    parser.add_argument("--detach", action="store_true", help="up: create tmux panes without attaching")
    parser.add_argument("--record", type=Path, help="New JSONL recording path; generated automatically by default")
    parser.add_argument("--refresh", action="store_true", help="Reapply the pinned Helm releases and demo manifests")
    parser.add_argument("--timeout", type=int, default=180, help="Seconds allowed for each DNS/HTTP transition")
    args = parser.parse_args()
    if not 1 <= args.timeout <= 600:
        parser.error("--timeout must be between 1 and 600")
    if args.detach and args.command != "up":
        parser.error("--detach is only used by up")
    if args.record and args.command == "setup":
        parser.error("Use up or rehearse to record traffic")
    RUNTIME.mkdir(exist_ok=True)
    print("Setup log: " + str(RUNTIME / "setup.log"), flush=True)
    missing = [t for t in ("docker", "k3d", "kubectl", "helm", "git", "make") if not shutil.which(t)]
    if args.command == "up" and not shutil.which("tmux"):
        missing.append("tmux")
    if missing:
        raise ValueError("Install these tools first: " + ", ".join(missing))
    capture(["docker", "info", "--format", "{{.ServerVersion}}"])
    # Install/re-exec before taking locks (locks must not span exec).
    ensure_python()
    path = recording_path(args.record) if args.command != "setup" else None
    with lock("setup.lock"):
        with lock("beats.lock"):
            if (RUNTIME / "beat-state.json").exists():
                raise ValueError("An unfinished demo needs recovery: ./demo/demo.sh reset")
            ensure_source()
            ids = ensure_clusters()
            deploy(ids, args.refresh)
            cfg = verify_and_prepare(args.timeout)
        if args.command == "rehearse":
            rehearse(cfg, path, args.timeout)
    print("\nLocal demo ready. Kubernetes access: " + str(RUNTIME / "kubeconfig"), flush=True)
    if args.command == "up":
        print("Recording: " + str(path), flush=True)
        argv = [sys.executable, "-u", str(HERE / "presenter.py"), "stage", "--record", str(path), "--timeout", str(args.timeout)]
        if args.detach:
            argv.append("--detach")
        os.execv(sys.executable, argv)
    elif args.command == "setup":
        print("Launch: ./demo/demo.sh up\nUnattended test: ./demo/demo.sh rehearse")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nSetup/rehearsal interrupted. Re-run setup to resume; use reset if recovery is pending.")
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        sys.exit("Setup: " + str(exc))
