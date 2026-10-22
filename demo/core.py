"""Read-only collectors shared by the host TUI and its local observer.

Python 3.9+, standard library only. All commands are argv lists, never shell code.
"""
import datetime as dt
import json
import re
import subprocess
import time
import urllib.error
import urllib.request


def now():
    return time.time()


def utc(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat(timespec="milliseconds")


def clean(value, limit=240):
    # Application responses and tool errors must not inject terminal controls.
    return "".join(c for c in str(value) if c.isprintable())[:limit]


def command(argv, timeout=6):
    p = subprocess.run(argv, text=True, capture_output=True, timeout=timeout)
    if p.returncode:
        raise RuntimeError(clean(p.stderr or p.stdout or "command failed"))
    return p.stdout


def observed(fn):
    started = now()
    try:
        return {"ok": True, **fn(), "at": now(), "started_at": started}
    except Exception as exc:
        return {"ok": False, "error": clean(exc), "at": now(), "started_at": started}


def endpoint_counts(data):
    """Match the reviewed k8gb ready=true OR nil, nonempty-address criterion.

    Deduplicate endpoints across overlapping slices. Counts are endpoints, not IPs
    or clusters; multiple addresses on one endpoint still count once.
    """
    endpoints = {}
    for item in data.get("items", []):
        for ep in item.get("endpoints", []):
            addresses = ep.get("addresses") or []
            if not addresses:
                continue
            key = tuple(sorted(addresses))
            ready = ep.get("conditions", {}).get("ready") is not False
            endpoints[key] = endpoints.get(key, False) or ready
    return {"ready": sum(endpoints.values()), "total": len(endpoints)}


def kube(config, cluster, args):
    return json.loads(command([
        "kubectl", "--context", config["clusters"][cluster]["context"],
        "--request-timeout=4s", "-n", config["namespace"], *args, "-o", "json"
    ]))


def collect_readiness(config, cluster):
    return observed(lambda: endpoint_counts(kube(config, cluster, [
        "get", "endpointslices.discovery.k8s.io", "-l",
        "kubernetes.io/service-name=" + config["service"]
    ])))


def collect_deployment(config, cluster):
    def read():
        dep = kube(config, cluster, ["get", "deployment", config["deployment"]])
        return {"desired": dep["spec"].get("replicas", 1),
                "ready": dep.get("status", {}).get("readyReplicas", 0)}
    return observed(read)


def parse_dig(output, authoritative=False):
    status = re.search(r"status: ([A-Z]+)", output)
    flags = re.search(r"flags: ([^;]*);", output)
    if not status or not flags:
        raise ValueError("DNS response missing header: " + clean(output))
    rcode = status.group(1)
    if rcode != "NOERROR":
        raise ValueError("DNS " + rcode)
    aa = "aa" in flags.group(1).split()
    if authoritative and not aa:
        raise ValueError("DNS response lacks authoritative (AA) flag")
    records = []
    in_answer = False
    for line in output.splitlines():
        if line.startswith(";; ANSWER SECTION:"):
            in_answer = True
            continue
        if line.startswith(";;"):
            in_answer = False
        if in_answer and line and not line.startswith(";"):
            fields = line.split()
            if len(fields) >= 5 and fields[2:4] == ["IN", "A"]:
                records.append({"name": fields[0].rstrip("."), "ttl": int(fields[1]), "ip": fields[4]})
    return {"records": records, "authoritative": aa, "rcode": rcode}


def query_dns(hostname, address, port=53, authoritative=False, require_aa=True):
    def query():
        response = parse_dig(command([
            "dig", "@" + address, "-p", str(port), hostname, "A", "+tcp",
            "+time=2", "+tries=1", "+noall", "+comments", "+answer",
            "+norecurse" if authoritative else "+recurse"
        ], timeout=4), authoritative and require_aa)
        return {**response, "server": address, "port": port}
    return observed(query)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_application(hostname, clusters, port=80):
    """A fresh HTTP connection using system DNS; never select an IP from UI state."""
    started = now()
    def request():
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        url = "http://" + hostname + (":" + str(port) if port != 80 else "") + "/"
        req = urllib.request.Request(url, headers={"Connection": "close", "User-Agent": "k8gb-talk-observer/1"})
        try:
            with opener.open(req, timeout=3) as response:
                body = response.read(16385)
                status = response.status
        except urllib.error.HTTPError as exc:
            exc.close()
            return {"status": exc.code, "served_by": None, "message": "HTTP " + str(exc.code),
                    "success": False, "latency_ms": round((now() - started) * 1000)}
        identity = None
        message = "Response did not identify a configured cluster"
        try:
            payload = json.loads(body)
            message = clean(payload.get("message", ""))
            tag = message.rsplit(":", 1)[-1].strip()
            identity = next((k for k, v in clusters.items() if tag == v["geotag"]), None)
        except (ValueError, AttributeError):
            pass
        return {"status": status, "served_by": identity, "message": message,
                "success": 200 <= status < 300, "latency_ms": round((now() - started) * 1000)}
    return observed(request)


def addresses(obs):
    return sorted(set(r["ip"] for r in obs.get("records", []))) if obs.get("ok") else []


def valid(obs, clock, stale_after=10):
    return bool(obs and obs.get("ok") and -1 <= clock - obs["at"] <= stale_after)


def load_config(path):
    with open(path) as f:
        config = json.load(f)
    # Only the known local playground is assumed; all locations remain explicit.
    for field in ("hostname", "parent_zone"):
        value = config[field]
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", value):
            raise ValueError("Invalid " + field)
    if set(config["clusters"]) != {"A", "B"}:
        raise ValueError("This audience layout expects demonstration clusters A and B")
    if config["interval"] < 0.5 or config["stale_after"] < config["interval"] * 2:
        raise ValueError("Use interval >= 0.5 and stale_after >= 2 * interval")
    return config
