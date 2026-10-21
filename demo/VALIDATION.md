# CLI demo validation

Preview and logic checks were run on 20 October 2026. Live integration remains unverified.

| Check | Result |
| --- | --- |
| Python 3.9 syntax compilation | Passed |
| Automated suite | 19 tests: 16 passed, 3 loopback-network tests skipped because this environment denies socket binding |
| DNS parsing | AA flag, multiple A records, returned TTL, SERVFAIL/NXDOMAIN, empty answer and missing response covered |
| Readiness | True, false, unset, empty addresses, duplicate slices and API failure covered |
| HTTP handling | Supplied-response tests verify actual response identity extraction, unknown identity, original request hostname, HTTP errors and network timeout retention |
| Stale data / replay | Old healthy values stop displaying as current; duplicate samples ignored; live-origin metadata required for replay; existing recordings protected |
| Layout | Tested character grids at 90×25, 110×38, 132×44 and 170×52; undersized terminal prompts for resize |
| Interactive CLI | Started preview in a 132×44 PTY, observed updates, quit with `q`; terminal restored |
| Visual inspection | Inspected the PNG exported from the same character grid at the illustrative cache-lag phase |
| Live prerequisites | `prepare.py --check` stopped at Docker's permission-denied response; no image build or container creation attempted |
| Real loopback HTTP / Unbound / Docker / k8gb failover | **Not validated in this session** |
| Upstream checkout | `git status --short` empty after implementation; upstream files unchanged |

The three skipped tests use a real local HTTP server to verify response identity, unknown identity and HTTP 503 behavior. They remain in the suite and should run in a normal terminal outside this restricted session. The passing HTTP tests use supplied responses, not a network connection.

No captured live run is included. [preview.png](preview.png) is an **illustrative character-grid export**, not live evidence. Its colors represent the intended terminal palette; the actual TUI uses the terminal's nearest 256-color entries. Generate a text/SVG snapshot with `python3 demo/dashboard.py --preview --snapshot --at 18 --svg /tmp/k8gb-cli.svg`.

## Remaining integration rehearsal

1. Start the already documented upstream playground; check both application clusters and the parent BIND infrastructure cluster.
2. Run `python3 demo/prepare.py --check`, then `python3 demo/prepare.py`. Check the observer image builds and Unbound starts. Confirm the displayed hostname and geotags match the deployed resource.
3. Open `--live --record` in a new capture file. Verify both authoritative A answers, resolver records/TTL, and actual podinfo identity. Ensure reads remain fresh; no unknown or stale initial state.
4. Capture original replicas; run the existing failover action. Observe endpoint loss, authoritative publication and the actual request transition. Do not promise a particular delay or flush the cache.
5. Restore the captured replica count, verify A → B → A from the observations, then replay the recording. Verify the dashboard's response stream and timing agree with the capture.
6. Repeat from a fresh observer and from a warm-cache observer. Rehearse the talk timing. A whole-cluster outage and peer-DNS partition remain separate optional drills.

The DNS/HTTP collector observes only this two-cluster local demonstration and one backend Service. It is not a generic production monitor, a cluster-count limit in k8gb, or a proof of transaction correctness.
