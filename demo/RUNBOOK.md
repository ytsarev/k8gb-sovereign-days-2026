# Local failover demo runbook

Allow around seven minutes. Start with both application clusters healthy and the observer already running. The presenter shows each command and a short note; press Enter to execute and advance.

## Prepare before the talk

The upstream k8gb playground was reviewed at commit [`330d33335981b9716bc8ca245660d90893eae272`](https://github.com/k8gb-io/k8gb/tree/330d33335981b9716bc8ca245660d90893eae272). In that separate checkout, its setup entry point is `make deploy-full-local-setup`. This demo package does not provision or delete those clusters.

The playground uses a referenced **Ingress**; the slide's declarative manifest illustrates **HTTPRoute**. Both use the failover strategy, but they are distinct route integrations.

1. Start the upstream playground with its two application clusters and local BIND9 parent. Keep both configured contexts available to `kubectl`.
2. From this repository, run `python3 demo/prepare.py --check`, then `python3 demo/prepare.py` to build/start the observer. Downloads are needed during preparation, not during the presentation.
3. Run `./demo/demo.sh status`. This prints its read-only checks; it does not inject a fault.
4. Open a full-screen terminal around 160 × 44 cells, with a large readable font.
5. Rehearse a complete live run and retain the recording. Live integration has not yet been validated in this workspace.

## On stage

```bash
./demo/demo.sh stage --record demo/recordings/talk-01.jsonl
```

Use a new filename for each capture. `--no-notes` hides the short speaker notes if desired. Every presenter Kubernetes command includes the explicit context; the user's current context is unchanged.

| Beat | Say / show |
| --- | --- |
| 1 · Baseline | “Two clusters represent independent locations. Both run the same application.” Inspect their desired and ready replicas. |
| 2 · Steering | “This is a standard Kubernetes custom resource. Both clusters prefer A's geotag.” Show the selected Gslb manifest fields. |
| 3 · DNS | “Local BIND9 delegates the application zone. Both cluster authorities answer, and the client has its own cache.” Inspect the delegation and local targets, then validate A. |
| 4 · Failure | “I remove the application's ready targets in A. Its DNS is still alive.” Read the displayed scale-to-zero command, then press Enter. |
| 5 · Observe | “Watch the three different moments: no ready endpoints, changed authoritative answers, then a successful request to B.” Preserve any failed samples and resolver lag. |
| 6 · Restore | “Now we restore exactly the replica count we started with.” Show the scale and rollout commands. |
| 7 · Verify | “Both authorities and the resolver return A, and the application identifies A again. The hostname stayed the same.” |

The left pane pauses before each command and before moving to the next beat. The right pane's `A`/`B` identity comes from the HTTP payload. Each history character represents one sampled request; it is not a throughput chart. `×` is a failure and `?` is a successful response with unknown identity.

The observer queries Unbound separately from its HTTP lookup. Its displayed TTL is the last returned TTL, not a simulated countdown. The convergence check requires both authoritative answers, the resolver, and the HTTP identity to match the expected target using fresh observations after the action.

## If a step fails

The presenter stops on failed commands or a convergence timeout and attempts to restore A automatically. It prints the restore commands. `q` plus Enter or Ctrl-C uses the same cleanup. A failed restoration keeps the recovery record.

After a killed terminal/process, use:

```bash
./demo/demo.sh reset
```

This restores only the saved Deployment identity and replica count, then waits for rollout. Verify both DNS authorities and HTTP before starting a new run. The saved data is in `demo/.runtime/beat-state.json`; do not replace it with a new baseline while recovery is pending.

If the system will not converge within the rehearsed time, restore it and use an actual captured replay:

```bash
python3 demo/dashboard.py --replay demo/recordings/rehearsal-01.jsonl --compact
```

No live recording is included in the repository. `--preview` is explicitly illustrative and is suitable for rehearsing the presentation, not as evidence of a measured failover.

## Finish

Normal completion restores the original replica count. The command pane stays visible; the traffic pane keeps running until `q`. Detach with `Ctrl-b d`, or close the demo session once both panes are finished. Remove the observer only after verifying restoration:

```bash
docker rm -f k8gb-talk-observer
```

Whole-cluster outages, peer-DNS partitions, parent-DNS failure, and stateful recovery remain separate tests. This walkthrough demonstrates application endpoint loss and local DNS/client recovery. See [observation details](README.md#what-is-measured) and [validation status](VALIDATION.md).
