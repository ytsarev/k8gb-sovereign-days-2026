# Local failover demo runbook

This demo uses the upstream k8gb local playground, reviewed at commit [`330d33335981b9716bc8ca245660d90893eae272`](https://github.com/k8gb-io/k8gb/tree/330d33335981b9716bc8ca245660d90893eae272). Live integration has not yet been rehearsed.

The [full-screen CLI dashboard](README.md) shows readiness, both DNS authorities, a client caching resolver, real request outcomes and a timeline. Run `python3 demo/dashboard.py --preview` from this repository root for the explicitly illustrative layout. Once the existing local playground is ready, run `python3 demo/prepare.py`, then `python3 demo/dashboard.py --live --record demo/recordings/rehearsal-01.jsonl`. Keep the commands below in a second terminal. Allow approximately seven minutes for the demo. [Validation](VALIDATION.md) separates tested preview logic from the live rehearsal still required.

The checkout must already have a working local playground. The source's setup entry point is `make deploy-full-local-setup`; prepare and rehearse that environment before presenting. The existing demo uses a referenced **Ingress**. The declarative-manifest slide shows an **HTTPRoute** example; both use the same failover strategy, but they are different route integrations.

| Setting | Upstream default |
| --- | --- |
| Primary context / geotag | `k3d-test-gslb1` / `eu` |
| Secondary context / geotag | `k3d-test-gslb2` / `us` |
| Namespace | `test-gslb` |
| Deployment | `frontend-podinfo` |
| Canonical Gslb / referenced Ingress | `failover-ingress` |
| Hostname | `failover.cloud.example.com` |
| Primary CoreDNS, exposed locally | `127.0.0.1:5053`, UDP/TCP |
| Secondary CoreDNS, exposed locally | `127.0.0.1:5054`, UDP/TCP |

Start in your upstream k8gb checkout. Capture the original replica count before changing it:

```bash
cd /path/to/k8gb # your upstream k8gb checkout
git rev-parse HEAD
./hack/demo-failover.sh status --yaml
REPLICAS="$(kubectl --context k3d-test-gslb1 -n test-gslb get deployment frontend-podinfo -o jsonpath='{.spec.replicas}')"
printf 'Original primary replicas: %s\n' "$REPLICAS"
```

Proceed only with both applications ready, the expected failover strategy/primary geotag, and a positive original replica count. Do not treat a pre-existing failed deployment as the initial state of this demo. Keep the value of `REPLICAS` visible in a second terminal for restoration.

Query both authorities before and after every state change. These queries have bounded DNS waits:

```bash
dig @127.0.0.1 -p 5053 failover.cloud.example.com A +tcp +time=2 +tries=1 +noall +answer
dig @127.0.0.1 -p 5054 failover.cloud.example.com A +tcp +time=2 +tries=1 +noall +answer
```

For diagnostics, the current internal health name is `localtargets.failover.cloud.example.com`. The implementation retains legacy dash-name compatibility; use the dot-name for the current checkout.

```bash
dig @127.0.0.1 -p 5053 localtargets.failover.cloud.example.com A +tcp +time=2 +tries=1 +noall +answer
dig @127.0.0.1 -p 5054 localtargets.failover.cloud.example.com A +tcp +time=2 +tries=1 +noall +answer
```

The upstream application probe creates a temporary BusyBox Pod, resolves through the selected cluster's CoreDNS, and prints the application region. Pre-pull its image before the talk. It tests that specific path, not a public recursive resolver:

```bash
./hack/demo-failover.sh probe
```

Remove primary ready targets, inspect both authorities again, and observe the secondary application response:

```bash
./hack/demo-failover.sh failover
./hack/demo-failover.sh probe
```

Restore the original replica count explicitly, then repeat the DNS and application checks:

```bash
./hack/demo-failover.sh failback "$REPLICAS"
./hack/demo-failover.sh probe
./hack/demo-failover.sh status
```

| Phase | Expected authoritative answers after reconciliation | Expected application probe |
| --- | --- | --- |
| Initial healthy primary | Both contain primary application IPs | `eu` |
| Primary scaled to zero | Both contain secondary application IPs | `us` |
| Original replicas restored and ready | Both contain primary application IPs | `eu` |

The upstream script can continue after a transition wait fails (`wait_for_message ... || true`). Its exit status is therefore **not** a sufficient success criterion. Inspect the answers and application identity. If there is no convergence within the rehearsed budget, restore the original state and use a prepared recording; do not extend the live demo indefinitely. A backup recording is not included in this package.

If the presentation shell is interrupted, restoration remains explicit:

```bash
kubectl --context k3d-test-gslb1 -n test-gslb scale deployment/frontend-podinfo --replicas "$REPLICAS"
kubectl --context k3d-test-gslb1 -n test-gslb rollout status deployment/frontend-podinfo --timeout=180s
```

Budget approximately seven minutes for the transition, exercise and explanation, plus one minute for debrief. The existing commands demonstrate loss of ready application endpoints. The new observer adds actual local recursive caching and a continuous HTTP stream when it is prepared and running. A whole-cluster outage, peer-DNS partition, parent-DNS failure and stateful application recovery require separate drills. Live integration of the new observer has not yet been validated.
