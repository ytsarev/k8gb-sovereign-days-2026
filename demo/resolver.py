#!/usr/bin/env python3
"""Container entry point: real caching resolver + persistent HTTP/DNS observer."""
import argparse
import ipaddress
import os
from pathlib import Path
import socket
import subprocess
import time

from core import load_config


def unbound_config(cfg):
    parent = str(ipaddress.ip_address(cfg["parent_address"]))
    port = int(cfg["parent_port"])
    if not 1 <= port <= 65535:
        raise ValueError("Invalid parent DNS port")
    return '''server:
  interface: 127.0.0.1
  port: 53
  access-control: 127.0.0.0/8 allow
  do-ip6: no
  username: "unbound"
  chroot: ""
  directory: "/tmp"
  pidfile: "/tmp/k8gb-unbound.pid"
  use-syslog: no
  logfile: ""
  verbosity: 0
  # Private demo zone is unsigned. This instance is only the local demo client.
  module-config: "iterator"
  prefetch: no
  serve-expired: no
  cache-min-ttl: 0
stub-zone:
  name: "%s"
  stub-addr: %s@%s
  # The playground parent advertises ns.example.com = 127.0.0.1.
  # Keep the configured parent address; still follow child-zone delegations.
  stub-prime: no
  stub-first: no
''' % (cfg["parent_zone"], parent, port)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    cfg = load_config(args.config)
    path = Path("/tmp/k8gb-unbound.conf")
    path.write_text(unbound_config(cfg))
    subprocess.run(["unbound-checkconf", str(path)], check=True, stdout=subprocess.DEVNULL)
    resolver = subprocess.Popen(["unbound", "-d", "-c", str(path)])
    deadline = time.monotonic() + 5
    while True:
        if resolver.poll() is not None:
            raise RuntimeError("Unbound exited before listening")
        try:
            with socket.create_connection(("127.0.0.1", 53), timeout=0.2):
                break
        except OSError:
            if time.monotonic() > deadline:
                raise RuntimeError("Unbound did not start")
            time.sleep(0.1)
    # This changes only this disposable observer container's resolver. The host
    # and both application clusters keep their existing DNS configuration.
    Path("/etc/resolv.conf").write_text("nameserver 127.0.0.1\noptions timeout:1 attempts:1\n")
    os.execv("/usr/bin/python3", ["python3", "-u", "/opt/demo/observer.py", "--config", args.config])


if __name__ == "__main__":
    main()
