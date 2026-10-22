# k8gb terminal demo

An **Enter-driven command walkthrough** with a traffic pane on the right. The presentation follows the beat-and-note rhythm of the [DevOpsDays demo](https://github.com/ytsarev/devopsdays-2026-demo/blob/main/demo.sh). Every presenter command is printed with its actual context and arguments before it runs.

## Rehearse the presentation

```bash
./demo/demo.sh stage --preview
```

Use a terminal around **160 columns × 44 rows** (minimum 124 × 34). The launcher creates a new tmux session, selects the command pane, and leaves existing sessions and global tmux settings alone. Python 3.9+ and tmux are required; preview mode needs no pip packages. On macOS, install tmux with `brew install tmux`; on Debian/Ubuntu, `sudo apt-get install tmux`.

- **Enter** runs the displayed command or advances to the next beat.
- Short notes appear above the commands; `--no-notes` hides them.
- **q + Enter** or **Ctrl-C** stops the presenter. If it has scaled A down, it attempts to restore the captured replica count and waits for rollout.
- The right pane is read-only. Its **q** key closes only that view.
- `Ctrl-b`, then an arrow key switches panes; `Ctrl-b d` detaches and leaves the session running.

Preview mode never executes Docker or Kubernetes commands. Both panes label the data as **illustrative**. Its timeline follows the Enter-driven beats: baseline, failure, cached-answer lag, traffic on B, and restoration. The sample delays are not measurements. Preview waits jump to the illustrative outcome when you press Enter to verify it.

![Illustrative two-pane presentation](stage-preview.svg)

Without tmux, use two terminals:

```bash
# Left: visible commands and notes
./demo/demo.sh --preview

# Right: compact traffic view, following the same preview beats
./demo/demo.sh traffic --preview
```

For a transcript, `./demo/demo.sh --preview --auto 0 --no-clear` runs the entire illustrative walkthrough without pauses. `--auto SECONDS` also works in live standalone mode for an intentional unattended rehearsal.

## The seven beats

| Beat | Commands and observation |
| --- | --- |
| 1 · Two clusters, two locations | Inspect both Deployments; capture A's original replica count. |
| 2 · Declarative traffic steering | Inspect both Gslb resources and validate their hostname, geotags, and failover strategy. |
| 3 · Follow the name | Query local BIND9, inspect each cluster's local targets, and verify the baseline against both authorities, the resolver, and HTTP. |
| 4 · Remove A's ready targets | Show and execute `kubectl scale … --replicas=0` against A's demo Deployment. |
| 5 · Follow traffic to B | Inspect EndpointSlices and both authoritative answers; wait for fresh DNS, resolver, and HTTP observations on B. |
| 6 · Restore A | Restore the captured replica count with `kubectl scale`, then wait for rollout. |
| 7 · Verify the return | Query both authorities and verify that the actual HTTP response returns to A. |

Presenter resource reads execute **`kubectl get … -o yaml`**. Selected fields from that YAML are syntax-highlighted: teal keys, green strings, and amber numbers/booleans. Field names and nesting stay unchanged, including `spec.replicas` and `status.readyReplicas`. The same YAML response supplies the validation and recovery checks; there is no second JSON fetch. Set `NO_COLOR=1` for plain YAML; redirected transcripts omit colors automatically. Repeated observation checks print the `docker logs` command and explain what is being polled. The traffic pane independently performs read-only Kubernetes sampling in the background.

The fault affects the **application's ready endpoints**, not the whole cluster. A's DNS stays available, making the distinction between workload readiness, authoritative publication, and client recovery visible.

## Run the real demo

Start Docker, then run this from the repository root:

```bash
./demo/demo.sh up
```

This installs the local Python dependency, downloads a pinned k8gb checkout, creates the parent BIND9 cluster and two application clusters, installs the playground, prepares the observer, and verifies both authoritative DNS answers, the resolver, and an actual HTTP response from A. It then opens the Enter-driven tmux presentation and records live observations to a new file under `demo/recordings/`.

Prerequisites: **Python 3.9+, Docker, k3d, kubectl, Helm, Git, Make, and tmux**. The launcher reports missing tools; it does not install global packages. Initial setup needs internet access and enough Docker resources for three additional Kubernetes clusters. Setup progress is shown in the terminal; detailed output goes to `demo/.runtime/setup.log`.

```bash
./demo/demo.sh setup       # Provision and verify, without opening tmux
./demo/demo.sh rehearse    # Provision if needed; run and record A → B → A automatically
./demo/demo.sh up --detach # Prepare both panes and print the tmux attach command
```

Setup resumes after failures and reuses clusters created by this checkout. A name collision with an unrelated cluster stops setup. Repeated runs reuse matching Helm installations and the observer's cache; `--refresh` reapplies the pinned releases and manifests. Use `--record PATH` with `up` or `rehearse` to choose a new recording filename. `--timeout SECONDS` sets the transition wait (180 seconds by default for these commands).

All Kubernetes access uses `demo/.runtime/kubeconfig` with private file permissions. Setup does not modify your default kubeconfig. The launcher selects this file when `KUBECONFIG` is unset, and passes it explicitly into both tmux panes. To inspect this playground manually, use `export KUBECONFIG="$PWD/demo/.runtime/kubeconfig"`.

k3d nodes share the Docker host's Linux file-watcher quota. Setup raises `fs.inotify.max_user_instances` to at least 8192 and `fs.inotify.max_user_watches` to at least 1048576 when necessary. On macOS these settings apply inside the Docker VM. Existing higher limits are preserved.

The observer runs Unbound, DNS probes, and HTTP requests continuously, independently of the command pane. Only its configuration file is mounted into the container; the kubeconfig stays on the host. Prepare and rehearse before going offline. `.runtime/`, `.venv/`, and recordings are ignored by Git.

For an already prepared playground, `./demo/demo.sh stage --record PATH` opens the presentation directly without rerunning setup. Manual observer preparation remains available through `prepare.py --check` and `prepare.py --reuse`; supply the appropriate `KUBECONFIG` when using Python entry points directly.

The presenter checks the baseline again and captures the replica count immediately relevant to that run. An unfinished mutation is saved in `demo/.runtime/beat-state.json` before scaling. A second presenter cannot run concurrently. A new live run refuses to overwrite an unfinished recovery record.

| Component | Default |
| --- | --- |
| Namespace / workload / Service | `test-gslb` / `frontend-podinfo` / `frontend-podinfo` |
| Gslb / hostname | `failover-ingress` / `failover.cloud.example.com` |
| Primary A / secondary B | `k3d-test-gslb1` (`eu`) / `k3d-test-gslb2` (`us`) |
| Docker network | `k3d-action-bridge-network` |
| Authoritative DNS | `k3d-test-gslb1-serverlb:53`, `k3d-test-gslb2-serverlb:53` |
| Parent BIND9 | `k3d-edgedns-server-0:30053` |

Parent BIND9 runs in a **third infrastructure cluster**, separate from the two application clusters. Its host port is 1053; the observer uses the server container's NodePort 30053. Settings are in [config.json](config.json); live mode uses the prepared `.runtime/config.json`. Pass `--config PATH` to use another prepared file.

Each convergence check is bounded: 180 seconds for `up` and `rehearse`, or 90 seconds when launching `run` or `stage` directly. Override either with `--timeout`. Checks require fresh observations after the action: both authoritative A-record sets and the resolver match the target IPs, and a successful HTTP response identifies the expected cluster. A timeout fails the run and triggers restoration; it is never reported as success.

## Restore and finish

Normal completion restores A. If the terminal or tmux session is lost, recover with:

```bash
./demo/demo.sh reset
```

Reset uses the saved context, namespace, Deployment UID, and original replica count. It refuses to alter a replacement Deployment or an unexpected replica count. If restoration fails, it retains the record for another attempt. `reset` waits for rollout; repeat DNS and HTTP verification before presenting again. Killing the process forcibly cannot run its cleanup handler.

After completion, the command pane remains visible and the traffic pane keeps sampling. Quit the traffic pane, then close that demo's tmux session. Detaching alone does not stop either pane. Once restoration is verified, stop the observer explicitly:

```bash
docker rm -f k8gb-talk-observer
```

Removing the observer discards its resolver cache. Do not flush or restart it during a measured transition.

## Full-screen dashboard

The original detailed view remains available:

```bash
python3 demo/dashboard.py --preview
python3 demo/dashboard.py --live --record demo/recordings/full-screen.jsonl
```

It needs 110 × 38 cells, preferably 132 × 44. In preview/replay, `space` pauses and `r` restarts; `m` adds an observation marker in live mode. In the Enter-controlled compact preview, pacing belongs to the presenter pane.

## What is measured

- **Host:** explicit-context `kubectl` reads of Deployment and EndpointSlices, bounded by timeouts. Endpoint readiness follows the reviewed k8gb criterion: nonempty addresses with `ready=true` or an unset condition. The UI counts endpoints, not healthy-record IP entries or clusters. It is scoped to this demo's one backend Service, not a general implementation of all Gslb readiness modes.
- **Observer container:** direct, nonrecursive TCP A queries to both configured CoreDNS servers; an active query to local Unbound; and a fresh HTTP connection to the unchanged hostname. All run in one persistent observer process outside A and B. Each observation has a timestamp; the nominal interval is two seconds and slow cycles take longer.
- **Resolver:** Unbound follows the local parent zone's delegation and caches actual responses. Displayed TTL is the value in the last DNS response; it is not an animated estimate. The extra DNS sample is active and can populate the cache. HTTP performs a separate system lookup through the same resolver, so its answer can change between the two observations.
- **Application identity:** parsed from podinfo's `message` (`Ingress NGINX: eu` / `us`), never inferred from DNS or manually chosen in the dashboard. HTTP uses normal hostname resolution, no proxy, no redirects, no manual IP override and a fresh connection per request. It does not demonstrate existing-session migration or business-transaction correctness.
- **Errors:** unsuccessful reads are unknown, not zero; old observations become stale after ten seconds. Failure responses stay in the request history. The timeline reports observation times, not exact internal controller transition times. Host and Docker VM clocks should be synchronized.

Unbound uses a private unsigned demo zone and a configured authoritative stub, rather than a public upstream resolver. Its `stub-prime: no` avoids replacing the configured parent address with the playground's `ns.example.com = 127.0.0.1` record. Child delegations are still followed. See the [official Unbound stub-zone documentation](https://unbound.docs.nlnetlabs.nl/en/latest/manpages/unbound.conf.html#stub-zone-options).

The pinned [CoreDNS CRD plugin v0.7.0](https://github.com/k8gb-io/coredns-crd-plugin/blob/v0.7.0/service/gateway/gateway.go) leaves the AA flag unset when constructing replies. Automated setup records `require_authoritative_flag: false` explicitly in the prepared configuration. Observations preserve the actual flag and queried server; convergence checks require each configured server and port, the expected IP sets, fresh samples, and the correct HTTP identity. The full dashboard displays `AA unset`. Other manually prepared configurations retain strict AA checking by default.

## Record and replay

```bash
python3 demo/dashboard.py --replay demo/recordings/rehearsal-01.jsonl
```

Recordings contain a live-origin header, configuration, original replica counts (when prepared), and the observations received during the live view or unattended rehearsal. Replay is labelled **RECORDED REPLAY** and preserves receipt timing, responses, errors and observation timestamps. Recording paths must be new; old evidence is never overwritten. Machine-specific recordings stay local. A header is provenance metadata, not a cryptographic authenticity guarantee.

For a text/SVG layout snapshot:

```bash
python3 demo/dashboard.py --preview --snapshot --at 18 --svg /tmp/k8gb-cli.svg
```

## Validation

```bash
demo/.venv/bin/python -m unittest discover -s demo -p 'test_*.py' -v
```

See [validation status](VALIDATION.md). The real local A → B → A sequence has passed with recorded DNS, resolver, and HTTP observations. Rehearse on the presentation machine before the talk; use the retained recording for replay if needed.
