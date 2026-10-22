# Recorded offline fallback

From the repository root:

```bash
./demo/demo.sh offline
```

This is a real local k8gb rehearsal captured on **22 October 2026 at 13:00:35 UTC**. It contains seven beats and 26 Enter pauses, with approximately 58 seconds of recorded observations. Time spent explaining at Enter prompts is added to that duration.

The capture shows application endpoint loss in Cluster A, authoritative DNS and resolver changes, HTTP responses from B, and restoration to A. It retains eight failed HTTP samples and intermediate disagreement between DNS answers. The original application replica count was restored and verified before the capture completed. The initial dashboard has observations from all eight sources before the first Enter prompt.

- `traffic.jsonl`: original live dashboard observations and configuration.
- `traffic.commands.jsonl`: matching commands, displayed output, selected YAML, notes, and pause/resume boundaries.

These files were copied unchanged from the same successful rehearsal. They contain demo addresses and resource names, with no kubeconfig, private keys, credentials, or host-specific filesystem paths. Both panes explicitly identify the output as **RECORDED REPLAY**. Playback never runs the displayed commands. The times are observations from this local machine, not a production recovery-time guarantee.

SHA-256 checksums:

```text
bab4f22bf2157f320c03e3d945c5666e925184c6cb0029024cd926919977d665  traffic.jsonl
7d7fe36b15b43961996319d5c04b354f5584479c48de4b4f506cf4829b5242e1  traffic.commands.jsonl
```

The checksums identify this bundled capture; they are not an independent attestation of its origin. To make a fresh paired capture, run `./demo/demo.sh rehearse --record demo/recordings/new-run.jsonl`, then `./demo/demo.sh offline demo/recordings/new-run.jsonl`.
