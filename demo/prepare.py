#!/usr/bin/env python3
"""Prepare the local observer; never create or mutate Kubernetes workloads."""
import argparse
import copy
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


def prepare(config, check_only=False):
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
    if cfg["observer_container"] in existing:
        raise ValueError("Observer already exists: %s. Reuse it, or explicitly remove that demo container before preparing again." % cfg["observer_container"])
    image = "k8gb-talk-observer:local"
    subprocess.run(["docker", "build", "-t", image, str(HERE)], check=True)
    cfg["prepared_at"] = utc(now())
    cfg["observer_image_id"] = command(["docker", "image", "inspect", "--format", "{{.Id}}", image]).strip()
    runtime = HERE / ".runtime"
    runtime.mkdir(exist_ok=True)
    (runtime / "config.json").write_text(json.dumps(cfg, indent=2) + "\n")
    subprocess.run(["docker", "run", "-d", "--name", cfg["observer_container"],
        "--label", "k8gb-talk-demo=observer", "--network", cfg["network"],
        "--log-opt", "max-size=10m", "--log-opt", "max-file=2",
        "--mount", "type=bind,src=%s,dst=/config,readonly" % runtime,
        image], check=True)
    print("Observer started; verify DNS and request observations in the dashboard.")
    print("python3 demo/dashboard.py --live --record demo/recordings/rehearsal.jsonl")
    (HERE / "recordings").mkdir(exist_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=HERE / "config.json")
    parser.add_argument("--check", action="store_true", help="Read-only prerequisite checks; no files/build/container")
    args = parser.parse_args()
    prepare(load_config(args.config), args.check)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
        sys.exit("Preparation: " + clean(exc))
