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

## Prepare the live demo

The upstream local playground must already be running with both applications ready. See [the runbook](RUNBOOK.md).

```bash
python3 -m venv demo/.venv
demo/.venv/bin/python -m pip install -r demo/requirements.txt
python3 demo/prepare.py --check
python3 demo/prepare.py
./demo/demo.sh status
./demo/demo.sh stage --record demo/recordings/rehearsal-01.jsonl
```

The launcher automatically uses `demo/.venv` when present. PyYAML parses the native Kubernetes YAML in live mode; install it before presenting. The virtual environment is ignored by Git. Preview mode still works with the standard library alone.

Preparation discovers Docker-network IPs, checks both Gslb resources, builds a local observer image, and creates only `k8gb-talk-observer`. It publishes no host ports and will not replace an existing container. Build and pre-pull dependencies before an offline presentation. The observer runs Unbound, DNS probes, and HTTP requests continuously, independently of the command pane.

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

Each convergence check has a 90-second bound, configurable with `--timeout`. It requires fresh observations after the action: both authoritative A-record sets and the resolver match the target IPs, and a successful HTTP response identifies the expected cluster. A timeout fails the run and triggers restoration; it is never reported as success.

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

See [validation status](VALIDATION.md). The Enter-driven preview, tmux layout, and automated behavior checks were exercised; live integration has **not** been rehearsed. Run the complete real A → B → A sequence, retain the recording, and verify restoration before using live mode on stage.
