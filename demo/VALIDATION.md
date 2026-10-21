# Demo validation

The Enter-driven refactor was checked on **22 October 2026**. Live integration with k8gb remains unverified: this workspace has no configured Kubernetes contexts.

| Check | Result |
| --- | --- |
| Python 3.9 syntax / Bash launcher | Passed |
| Automated suite | **30 tests passed, no skips**, including three real loopback HTTP tests |
| Complete presenter flow | Controlled command responses exercised baseline → scale to zero → DNS/HTTP verification → restore; original replica count of three preserved |
| Interrupted/failed presenter | Failure or interruption after scaling triggers restoration; quitting/EOF before execution performs no mutation |
| Recovery identity | Replacement Deployment rejected; recovery record retained; replica precondition included in each scale command |
| Convergence | Requires fresh observations after the action, authoritative answers and resolver matching expected IPs, and successful HTTP identifying the expected cluster; stale/divergent/failed observations rejected |
| Timeout | Raises a failure rather than claiming a successful transition |
| Offline preview | Entire walkthrough completes without executing any Docker/Kubernetes commands or creating live recovery state |
| tmux presentation | Exercised all seven beats with Enter in a 160 × 44 session; verified cached-answer lag/failures, traffic on B, then restoration to A in the right pane; both panes exited cleanly |
| Compact view | Cache lag, failed samples, cluster identity, stale observations, and presenter-controlled preview pacing covered |
| Terminal colors | Captured actual ANSI output in a separate tmux preview; dark background and panel use xterm indices 234 and 236, replacing the incorrect teal/gray fills without changing the terminal palette |
| Existing observation logic | EndpointSlice handling, DNS AA/TTL/error parsing, HTTP identity, unknown identity, HTTP failures, stale data, duplicate samples, and recording/replay protections remain covered |
| Live BIND9 / Unbound / Docker / k8gb failover | **Not rehearsed in this session** |

Run the suite from the repository root:

```bash
python3 -m unittest discover -s demo -p 'test_*.py' -v
```

The tmux test was an **illustrative preview**. The presenter integration tests supplied controlled Kubernetes/Docker responses; they prove orchestration and recovery behavior, not an actual controller failover. The HTTP tests used a real loopback server.

[stage-preview.svg](stage-preview.svg) is a character-grid illustration of the tested presentation layout. [preview.png](preview.png) is the earlier full-screen illustrative layout. Neither is evidence of measured k8gb performance. No captured live rehearsal is included.

## Remaining live rehearsal

1. Start the upstream local playground with both application clusters and the parent BIND9 infrastructure cluster.
2. Run `python3 demo/prepare.py --check`, then `python3 demo/prepare.py`. Verify the observer builds and Unbound starts; check the configured hostname and geotags.
3. Run `./demo/demo.sh stage --record demo/recordings/rehearsal-01.jsonl` in a terminal around 160 × 44 cells.
4. Advance through all seven beats. Verify the baseline, EndpointSlice loss in A, authoritative publication of B, resolver behavior, and actual HTTP response from B. Preserve failed samples; do not flush the cache.
5. Restore the original replica count and verify the return to A. Check that no `beat-state.json` recovery record remains.
6. Replay the JSONL capture; compare its observations to what was seen live. Rehearse once from a fresh observer and once with a warm cache.
7. Verify interruption/recovery against the actual demo workload before relying on it on stage.

Whole-cluster outages, peer-DNS partitions, parent-DNS failure, and stateful recovery are separate tests. This package demonstrates two example clusters; it does not imply a two-cluster limit in k8gb.
