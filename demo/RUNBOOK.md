# Local failover demo runbook

Allow around seven minutes. Start with both application clusters healthy and the observer already running. The presenter shows each command and a short note; press Enter to execute and advance.

## Prepare before the talk

The launcher provisions the upstream playground pinned at commit [`330d33335981b9716bc8ca245660d90893eae272`](https://github.com/k8gb-io/k8gb/tree/330d33335981b9716bc8ca245660d90893eae272), using k8gb v1.0.1. It keeps the checkout, isolated kubeconfig, installation state, and logs under ignored `demo/.runtime/`.

The playground uses a referenced **Ingress**; the slide's declarative manifest illustrates **HTTPRoute**. Both use the failover strategy, but they are distinct route integrations.

1. Start Docker and install any missing CLI prerequisites listed in [the README](README.md#run-the-real-demo).
2. Run `./demo/demo.sh setup`. This creates all three clusters, installs the playground and observer, and verifies real DNS and HTTP traffic. Add `--merge-kubeconfig` to make the contexts available in `~/.kube/config` for another terminal. Rerunning setup reuses resources owned by this checkout.
3. Run `./demo/demo.sh rehearse`. This automatically executes the full A → B → A sequence, restores the original replicas, and saves a new JSONL recording. A failed transition returns a nonzero status.
4. Open a full-screen terminal around 160 × 44 cells, with a large readable font.
5. Rehearse once using Enter and the traffic pane, and retain the recording. Review [validation status](VALIDATION.md) for what has been exercised.

## On stage

```bash
./demo/demo.sh up
```

The recording filename is generated automatically. To choose one, use `up --record demo/recordings/talk-01.jsonl`. Once setup is complete, `stage --record PATH --no-notes` launches directly with notes hidden. Every presenter Kubernetes command includes the explicit context; setup and both panes use the isolated demo kubeconfig.

| Beat | Say / show |
| --- | --- |
| 1 · Baseline | “Two clusters represent independent locations. Both run the same application.” Inspect their desired and ready replicas. |
| 2 · Steering | “This is a standard Kubernetes custom resource. Both clusters prefer A's geotag.” Show the selected Gslb manifest fields. |
| 3 · DNS | “Local BIND9 delegates the application zone. Query either CoreDNS server for the same hostname: both return A's application IPs.” Show those two answers first, then the distinct local targets in A and B, and validate the resolver and HTTP baseline. |
| 4 · Failure | “I remove the application's ready targets in A. Its DNS is still alive.” Read the displayed scale-to-zero command, then press Enter. |
| 5 · Observe | “Watch the three different moments: no ready endpoints, changed authoritative answers, then a successful request to B.” Preserve any failed samples and resolver lag. |
| 6 · Restore | “Now we restore exactly the replica count we started with.” Show the scale and rollout commands. |
| 7 · Verify | “Both authorities and the resolver return A, and the application identifies A again. The hostname stayed the same.” |

The left pane pauses before each command and before moving to the next beat. The right pane's `A`/`B` identity comes from the HTTP payload. Each history character represents one sampled request; it is not a throughput chart. `×` is a failure and `?` is a successful response with unknown identity.

The observer queries Unbound separately from its HTTP lookup. Its displayed TTL is the last returned TTL, not a simulated countdown. The convergence check requires both authoritative answers, the resolver, and the HTTP identity to match the expected target using fresh observations after the action.

## If a step fails

For provisioning failures, inspect `demo/.runtime/setup.log`, correct the reported cause, and rerun `setup`. Missing clusters are created; completed installations are reused. `setup --refresh` reapplies installations. Setup refuses to run while a live presenter holds the lock or an unfinished recovery record exists.

The presenter stops on failed commands or a convergence timeout and attempts to restore A automatically. It prints the restore commands. `q` plus Enter or Ctrl-C uses the same cleanup. A failed restoration keeps the recovery record.

After a killed terminal/process, use:

```bash
./demo/demo.sh reset
```

This restores only the saved Deployment identity and replica count, then waits for rollout. Verify both DNS authorities and HTTP before starting a new run. The saved data is in `demo/.runtime/beat-state.json`; do not replace it with a new baseline while recovery is pending.

If the system will not converge within the rehearsed time, restore it and use an actual captured replay:

```bash
./demo/demo.sh replay demo/recordings/rehearsal-01.jsonl --compact
```

No live recording is included in the repository. `--preview` is explicitly illustrative and is suitable for rehearsing the presentation, not as evidence of a measured failover.

## Finish

Normal completion restores the original replica count. The command pane stays visible; the traffic pane keeps running until `q`. Detach with `Ctrl-b d`, or close the demo session once both panes are finished. Remove the observer only after verifying restoration:

```bash
docker rm -f k8gb-talk-observer
```

Whole-cluster outages, peer-DNS partitions, parent-DNS failure, and stateful recovery remain separate tests. This walkthrough demonstrates application endpoint loss and local DNS/client recovery. See [observation details](README.md#what-is-measured) and [validation status](VALIDATION.md).
