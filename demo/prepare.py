#!/usr/bin/env python3
"""Prepare the local observer; never create or mutate Kubernetes workloads."""
import argparse
import copy
import hashlib
import ipaddress
import json
from pathlib import Path
import subprocess
import sys

from core import command, kube, load_config, now, utc, endpoint_counts, clean
from resolver import unbound_config

HERE = Path(__file__).resolve().parent


def container_address(name, network):
    result = json.loads(command(["docker", "inspect", name]))[0]
    if not result["State"]["Running"]:
        raise ValueError("Container is not running: " + name)
    address = result["NetworkSettings"]["Networks"][network]["IPAddress"]
    return str(ipaddress.ip_address(address))


def observer_fingerprint(cfg):
    digest = hashlib.sha256(json.dumps(cfg, sort_keys=True).encode())
    for name in ("Dockerfile", "core.py", "observer.py", "resolver.py"):
        digest.update((HERE / name).read_bytes())
    return digest.hexdigest()


def reusable_observer(info, fingerprint, runtime):
    labels = info["Config"].get("Labels") or {}
    if labels.get("k8gb-talk-demo") != "observer" or not any(
            m.get("Source") and Path(m["Source"]).resolve() == (runtime / "config.json").resolve()
            for m in info.get("Mounts", [])):
        raise ValueError("Existing observer is not owned by this checkout; refusing to replace it")
    return labels.get("k8gb-demo-fingerprint") == fingerprint


def prepare(config, check_only=False, reuse=False):
    cfg = copy.deepcopy(config)
    # Fail before building or writing anything when the local playground is absent.
    command(["docker", "info", "--format", "{{.ServerVersion}}"])
    cfg["parent_address"] = container_address(cfg["parent_container"], cfg["network"])
    cfg["original_replicas"] = {}
    for key in ("A", "B"):
        cluster = cfg["clusters"][key]
        cluster["dns_address"] = container_address(cluster["container"], cfg["network"])
        dep = kube(cfg, key, ["get", "deployment", cfg["deployment"]])
        cfg["original_replicas"][key] = dep["spec"].get("replicas", 1)
        eps = endpoint_counts(kube(cfg, key, ["get", "endpointslices", "-l", "kubernetes.io/service-name=" + cfg["service"]]))
        if not eps["ready"] or not cfg["original_replicas"][key]:
            raise ValueError("Cluster %s is not ready at the baseline; restore it before preparation" % key)
        gslb = kube(cfg, key, ["get", "gslbs.k8gb.io", cfg["gslb"]])
        strategy = gslb["spec"]["strategy"]
        if strategy["type"] != "failover" or strategy["primaryGeoTag"] != cfg["clusters"]["A"]["geotag"]:
            raise ValueError("Cluster %s has an unexpected Gslb strategy or primaryGeoTag" % key)
        status = gslb.get("status", {})
        if status.get("geoTag") != cluster["geotag"]:
            raise ValueError("Cluster %s has an unexpected or missing status.geoTag" % key)
        if cfg["hostname"] not in [host.strip() for host in status.get("hosts", "").split(",")]:
            raise ValueError("Cluster %s Gslb does not report the configured hostname" % key)
        print("Cluster %s: %s ready endpoints, %s original replicas" % (key, eps["ready"], cfg["original_replicas"][key]))
    # Validate address/port config before creating files or containers.
    unbound_config(cfg)
    if check_only:
        print("Read-only prerequisite checks passed. DNS and requests still require a running observer.")
        return
    existing = command(["docker", "ps", "-a", "--format", "{{.Names}}"]).splitlines()
    runtime = HERE / ".runtime"
    runtime.mkdir(exist_ok=True)
    fingerprint = observer_fingerprint(cfg)
    if cfg["observer_container"] in existing:
        if not reuse:
            raise ValueError("Observer already exists; use --reuse to validate and reuse it")
        info = json.loads(command(["docker", "inspect", cfg["observer_container"]]))[0]
        if reusable_observer(info, fingerprint, runtime) and (runtime / "config.json").is_file():
            if not info["State"]["Running"]:
                command(["docker", "start", cfg["observer_container"]], timeout=30)
            print("Reusing the prepared observer and its resolver cache.")
            return load_config(runtime / "config.json")
        print("Replacing this checkout's observer because its configuration or code changed.")
        command(["docker", "rm", "-f", cfg["observer_container"]], timeout=30)
    image = "k8gb-talk-observer:local"
    subprocess.run(["docker", "build", "-t", image, str(HERE)], check=True, timeout=600)
    cfg["prepared_at"] = utc(now())
    cfg["observer_image_id"] = command(["docker", "image", "inspect", "--format", "{{.Id}}", image]).strip()
    (runtime / "config.json").write_text(json.dumps(cfg, indent=2) + "\n")
    subprocess.run(["docker", "run", "-d", "--name", cfg["observer_container"],
        "--label", "k8gb-talk-demo=observer", "--network", cfg["network"],
        "--label", "k8gb-demo-fingerprint=" + fingerprint,
        "--log-opt", "max-size=10m", "--log-opt", "max-file=2",
        "--mount", "type=bind,src=%s,dst=/config/config.json,readonly" % (runtime / "config.json"),
        image], check=True)
    print("Observer started; verify DNS and request observations in the dashboard.")
    print("python3 demo/dashboard.py --live --record demo/recordings/rehearsal.jsonl")
    (HERE / "recordings").mkdir(exist_ok=True)
    return cfg


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=HERE / "config.json")
    parser.add_argument("--check", action="store_true", help="Read-only prerequisite checks; no files/build/container")
    parser.add_argument("--reuse", action="store_true", help="Reuse a matching observer owned by this checkout")
    args = parser.parse_args()
    prepare(load_config(args.config), args.check, args.reuse)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
        sys.exit("Preparation: " + clean(exc))
