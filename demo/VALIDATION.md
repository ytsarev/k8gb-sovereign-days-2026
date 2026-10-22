# Demo validation

Checked on **22 October 2026**, including a real local k8gb failover and restoration.

| Check | Result |
| --- | --- |
| Python 3.9 syntax / Bash launcher | Passed |
| Automated suite | **49 tests passed**, including three real loopback HTTP tests run outside the sandbox |
| Provisioning | Rebuilt the three owned clusters with the minimal BIND9/k8gb/Ingress/Podinfo setup; verified parent DNS readiness and retried initial zone publication before launching |
| Real local failover | Repeated successfully after the null-endpoint fix: baseline A → zero ready application endpoints in A → DNS/resolver/HTTP on B → original replicas restored → DNS/resolver/HTTP on A |
| Restoration | Verified original replica count; no unfinished `beat-state.json` remained after the successful run |
| Recording | Real observation JSONL saved locally under ignored `demo/recordings/`; includes request failures and intermediate DNS/cache differences |
| Ownership and isolation | Unrelated name collisions rejected before mutation; missing owned clusters can be created on retry; setup uses a private kubeconfig and changes the default only with `--merge-kubeconfig` |
| Optional context import | Real `setup --merge-kubeconfig` verified DNS/HTTP and found the imported contexts already current. Controlled tests cover first setup, retained current context and namespace, refreshed demo entries, unrelated credentials and relative paths, private backups, repeated imports, invalid configs, symlinks, failed atomic replacement, and opt-in behavior after successful verification for setup/up/rehearse |
| Installation reuse | Completion marker tied to pinned source and cluster identities; failures do not mark installation complete; `--refresh` reapplies releases |
| Observer reuse | Requires matching checkout, configuration, and code; only the observer config is mounted, excluding Kubernetes credentials |
| Convergence | Requires fresh direct DNS and resolver records matching the target IPs, plus a successful HTTP response identifying the target cluster |
| DNS flags | Pinned CoreDNS CRD plugin v0.7.0 leaves AA unset; recordings retain the actual flag and server/port. Automated setup explicitly accepts this behavior while verifying direct server identity; manually prepared configurations default to strict AA checking |
| Interrupted/failed presenter | Controlled tests verify restoration after failure/interruption, retained recovery state on restoration failure, and no mutation when quitting before execution |
| Recovery identity | Controlled tests reject a replacement Deployment; each scale includes the observed replica precondition |
| Timeout | Failed convergence returns an error and attempts restoration; it is not reported as a successful transition |
| Preview | Clearly illustrative; completes without executing Docker/Kubernetes commands or creating live recovery state |
| tmux | Real `up --detach` launch verified with an existing tmux server: Enter executes a native YAML read; the right pane shows live DNS, endpoint readiness, and HTTP identity. Repeated launch reused the setup without restarting parent DNS; 25 requests to A and no DNS errors observed over 48 seconds |
| Completion display | Completed a separate tmux preview with status 0: final output remains visible with no exit banner. Verified nonzero exits retain their status; the setting is local to the demo window |
| Terminal output | Native `-o yaml`, highlighted selected fields, and compact EndpointSlices; presenter queries use `dig +short`, while parent DNS shows delegation records. Read-only live checks verified both clusters' answers, local targets, readiness output, and baseline convergence. Malformed YAML or invalid target addresses stop before mutation |
| Empty EndpointSlices | Regression tests reproduce Kubernetes returning `endpoints: null` after scale-to-zero. The presenter renders `endpoints: []`, and the dashboard reports zero ready endpoints; the complete controlled flow continues through failover and restoration |

Run the behavior suite:

```bash
demo/.venv/bin/python -m unittest discover -s demo -p 'test_*.py' -v
```

Provision, verify, and record an unattended live rehearsal:

```bash
./demo/demo.sh rehearse
```

This uses the pinned upstream checkout, k8gb v1.0.1, ingress-nginx chart 4.15.1, and matching Podinfo chart/image 6.15.0. Real setup checks exposed and corrected the Docker host's low inotify limit, k3d's separate kubeconfig documents, the upstream sample's Podinfo image/chart mismatch, premature DNS readiness, and the runtime command used during setup reuse. The plugin's unset AA flag is handled explicitly as described above.

The controlled presenter tests prove orchestration and recovery behavior. The live run separately verifies the actual Kubernetes → CoreDNS → Unbound → HTTP path. Timing depends on the local machine and is not a production recovery-time guarantee. Recordings stay local; the repository includes only illustrative layout assets, not captured live evidence.

Before presenting, run `./demo/demo.sh up`, rehearse with Enter, and review the right-hand pane and recording. The hostname uses an Ingress integration; the deck's HTTPRoute manifest illustrates a separate supported integration.

Whole-cluster outages, peer-DNS partitions, parent-DNS failure, stateful recovery, and existing-session continuity remain separate tests. This package demonstrates two example clusters; it does not imply a two-cluster limit in k8gb.
