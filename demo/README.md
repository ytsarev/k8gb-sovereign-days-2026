# k8gb terminal demo

A full-screen audience dashboard for the existing local **A → B → A** failover demo. It shows readiness, direct authoritative DNS answers, a real caching resolver, actual HTTP responses and an observation timeline. The dashboard is read-only; run the existing failover and restore commands in a second terminal.

## Try the visual preview

From the repository root:

```bash
python3 demo/dashboard.py --preview
```

Use a terminal around **132 columns × 44 rows**, ideally full screen. Minimum: 110 × 38. Python 3.9+ with curses is sufficient on macOS/Linux; no pip packages are needed. The palette matches the slides: blue A, teal B, red failures, amber caching. `q` quits; `space` pauses the preview/replay; `r` restarts it.

The 48-second preview uses **illustrative data**, prominently labelled. It demonstrates the layout and storytelling; its delays are not measured k8gb performance. It contacts no clusters and cannot be saved as a live recording. [Character-grid preview](preview.png).

## Prepare live observations

The upstream local playground must already be running with both applications ready. See [the runbook](RUNBOOK.md). This code does not provision the clusters or alter upstream files.

```bash
python3 demo/prepare.py --check
python3 demo/prepare.py
python3 demo/dashboard.py --live --record demo/recordings/rehearsal-01.jsonl
```

Preparation checks the configured contexts, workload readiness, failover strategy and primary geotag. It captures both original replica counts, builds a local observer image and creates **only** `k8gb-talk-observer` on the playground's Docker network. It publishes no host ports and mounts only its generated configuration. It will not replace an existing container. Building needs image/package downloads; build and pre-pull the playground dependencies before an offline presentation.

The Dockerfile installs Python, `dig`, and Unbound in Debian Bookworm. The resulting local image ID is captured in `demo/.runtime/config.json`. The base image and package repository can change; keep the built image for the rehearsed run.

Defaults in [config.json](config.json):

| Component | Default |
| --- | --- |
| Namespace / workload / Service | `test-gslb` / `frontend-podinfo` / `frontend-podinfo` |
| Gslb / hostname | `failover-ingress` / `failover.cloud.example.com` |
| Primary A / secondary B | `k3d-test-gslb1` (`eu`) / `k3d-test-gslb2` (`us`) |
| Docker network | `k3d-action-bridge-network` |
| Authoritative DNS | `k3d-test-gslb1-serverlb:53`, `k3d-test-gslb2-serverlb:53` |
| Parent BIND | `k3d-edgedns-server-0:30053` |

Preparation discovers their current Docker-network IPs. Parent BIND is a **third infrastructure cluster**, separate from the two application clusters. Its published host port is 1053; from the observer, use the server container's **NodePort 30053**. The reviewed edge cluster has no `serverlb` container.

## Present the experiment

Keep the dashboard on the audience screen. In a second terminal:

```bash
cd /path/to/k8gb # your upstream k8gb checkout
REPLICAS="$(kubectl --context k3d-test-gslb1 -n test-gslb get deployment frontend-podinfo -o jsonpath='{.spec.replicas}')"
printf 'Original primary replicas: %s\n' "$REPLICAS"
./hack/demo-failover.sh status --yaml
./hack/demo-failover.sh failover
```

Walk the audience through A's endpoint loss, both authoritative answers, the resolver's returned TTL, and the request history. `A` and `B` identify the response's geotag; `×` means a failed request; `?` means HTTP succeeded without a known cluster identity. The history is a stream of samples, not a throughput or availability benchmark.

Then restore the original state:

```bash
./hack/demo-failover.sh failback "$REPLICAS"
./hack/demo-failover.sh status
```

Watch actual HTTP responses return to A. `m` adds a presenter marker to the timeline and recording; it performs no cluster action. Replica changes appear independently when observed. If the operator shell is lost, the original counts are also in `demo/.runtime/config.json`; the runbook includes a direct restore command.

`q` closes only the dashboard. It **does not restore replicas or stop the observer**. After restoring and verifying the workload, remove the named observer when finished:

```bash
docker rm -f k8gb-talk-observer
```

This removes its cache and stops its request loop. Starting the dashboard again reuses an existing observer's warm cache. Do not restart/flush it during a measured transition.

## What is measured

- **Host:** explicit-context `kubectl` reads of Deployment and EndpointSlices, bounded by timeouts. Endpoint readiness follows the reviewed k8gb criterion: nonempty addresses with `ready=true` or an unset condition. The UI counts endpoints, not healthy-record IP entries or clusters. It is scoped to this demo's one backend Service, not a general implementation of all Gslb readiness modes.
- **Observer container:** direct TCP A queries to both CoreDNS authorities, requiring the AA flag; an active query to local Unbound; and a fresh HTTP connection to the unchanged hostname. All run in one persistent observer process outside A and B. Each observation has a timestamp; the nominal interval is two seconds and slow cycles take longer.
- **Resolver:** Unbound follows the local parent zone's delegation and caches actual responses. Displayed TTL is the value in the last DNS response; it is not an animated estimate. The extra DNS sample is active and can populate the cache. HTTP performs a separate system lookup through the same resolver, so its answer can change between the two observations.
- **Application identity:** parsed from podinfo's `message` (`Ingress NGINX: eu` / `us`), never inferred from DNS or manually chosen in the dashboard. HTTP uses normal hostname resolution, no proxy, no redirects, no manual IP override and a fresh connection per request. It does not demonstrate existing-session migration or business-transaction correctness.
- **Errors:** unsuccessful reads are unknown, not zero; old observations become stale after ten seconds. Failure responses stay in the request history. The timeline reports observation times, not exact internal controller transition times. Host and Docker VM clocks should be synchronized.

Unbound uses a private unsigned demo zone and a configured authoritative stub, rather than a public upstream resolver. Its `stub-prime: no` avoids replacing the configured parent address with the playground's `ns.example.com = 127.0.0.1` record. Child delegations are still followed. See the [official Unbound stub-zone documentation](https://unbound.docs.nlnetlabs.nl/en/latest/manpages/unbound.conf.html#stub-zone-options).

## Record and replay

```bash
python3 demo/dashboard.py --replay demo/recordings/rehearsal-01.jsonl
```

Recordings contain a live-origin header, configuration, original replica counts (when prepared), and the observations received while the dashboard was open. Replay is labelled **RECORDED REPLAY** and preserves receipt timing, responses, errors and observation timestamps. Recording paths must be new; old evidence is never overwritten. No real rehearsal recording is included yet. A header is provenance metadata, not a cryptographic authenticity guarantee.

For a text/SVG layout snapshot:

```bash
python3 demo/dashboard.py --preview --snapshot --at 18 --svg /tmp/k8gb-cli.svg
```

## Validation

```bash
python3 -m unittest discover -s demo -p 'test_*.py' -v
```

See [validation status](VALIDATION.md). The terminal preview was exercised; live integration has **not** been rehearsed. Run the complete real A → B → A sequence, retain the recording, and verify restoration before using live mode on stage.
