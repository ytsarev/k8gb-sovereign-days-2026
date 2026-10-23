# k8gb — Sovereign Cloud Days 2026

**Who Controls the Failover? Sovereign Global Traffic Management with Kubernetes**  
Yury Tsarev · 24 October 2026 · Saarbrücken

Final presentation and terminal demo for Kubernetes traffic steering across independent clusters.

- [PowerPoint — 21 main slides + 5 hidden appendix slides](slides/k8gb_sovereign_cloud_days_2026.pptx)
- [PDF](slides/k8gb_sovereign_cloud_days_2026_preview.pdf)
- [CLI demo and setup](demo/README.md)
- [Demo runbook](demo/RUNBOOK.md) and [validation status](demo/VALIDATION.md)

The PowerPoint includes speaker notes. The PDF is a preview rendered from the saved presentation; native PowerPoint rendering remains unverified.

## Presentation pacing

The prepared talk targets **27 minutes**, leaving **8 minutes for questions** within 35 minutes.

| Segment | Slides | Time |
| --- | --- | --- |
| k8gb capabilities and architecture | 1–12 | 11 min 15 sec |
| Local CLI demonstration | 13 | 6 min |
| Operational hard parts | 14–17 | 7 min |
| Closing, community and production case study | 18–20 | 2 min 45 sec |
| Questions | 21 | 8 min |

Speaker notes include the elapsed-time target for every main slide. The demo now precedes the DNS timing, application readiness, partition, and observability discussion. Readiness and the failed-payment example share one slide; the node/zone/cluster progression also shares one slide. Observability and resilience testing are combined into a single change-and-check table: test the application, cluster, peer-health lookup, and recovery; compare both DNS authorities, the client cache, and a real request in each case.

The architecture sequence is reference diagram (slide 5), component roles (6), reconciliation loops (7), then the client request path (8). The reconciliation slide has a one-minute budget; the architecture, request-path, strategy, and integrations explanations each use one minute to preserve the overall schedule. Slide 11 covers native Kubernetes resource integrations and DNS provider choices, based on the [resource reference documentation](https://www.k8gb.io/master/resource_ref/) and [DNS integration guides](https://www.k8gb.io/master/tutorials/). The PowerPoint has five hidden appendix slides after Questions, numbered A1–A5 (physical slides 22–26): delegation, normal operation, failover, failback, and availability responsibilities. They are skipped during the normal slideshow and available for questions. The PDF includes these reference pages and bookmarks after the 21 main slides.

The closing section includes a community snapshot checked on 23 October 2026: CNCF Incubating status, seven public adopters, 46 contributors (excluding automated accounts), and 1,335 GitHub stars. The adopter grid uses original brand logos, and the GitHub figure includes a star icon. Sources are linked in the slide notes. The Millennium bcp production case study immediately follows the public adopters slide, before Questions. The final Questions slide links to the project, GitHub, CNCF Slack, community meetings, the public adopters list, and the speaker’s LinkedIn profile.

## Try the demo preview

```bash
./demo/demo.sh stage --preview
```

Use a terminal around 160 columns × 44 rows, Python 3.9+, and tmux. Press Enter in the left pane to advance through visible commands and short notes; the right pane follows traffic, DNS, and readiness. The preview needs no Kubernetes cluster or pip dependencies.

![Illustrative command and traffic panes](demo/stage-preview.svg)

The preview uses clearly labelled illustrative data. For the real local demo, start Docker and run:

```bash
./demo/demo.sh up
```

This provisions the clusters, local BIND9, k8gb, and observer; checks real DNS and HTTP traffic; then opens the Enter-driven presentation with recording. `./demo/demo.sh rehearse` runs and records the complete A → B → A sequence automatically. See [prerequisites and setup](demo/README.md#run-the-real-demo) and the [runbook](demo/RUNBOOK.md).

## Offline fallback with Enter

```bash
./demo/demo.sh offline
```

Plays a bundled real rehearsal: captured commands and highlighted YAML on the left, synchronized recorded traffic on the right. Press Enter to advance; the recorded clock pauses while you explain. Both panes say **RECORDED REPLAY**. Only Python 3.9+ and tmux are needed; no Docker, Kubernetes, kubeconfig, network, or pip packages. The displayed commands are never executed. See [recording provenance](demo/fallback/README.md).

## Project and contact

- [k8gb.io](https://www.k8gb.io/)
- [k8gb source](https://github.com/k8gb-io/k8gb)
- [#k8gb on CNCF Slack](https://cloud-native.slack.com/archives/C021P656HGB)
- [Community meeting calendar](https://zoom-lfx.platform.linuxfoundation.org/meetings/k8gb?view=month)
- [Public adopters](https://github.com/k8gb-io/k8gb/blob/master/ADOPTERS.md)
- [Yury Tsarev on LinkedIn](https://www.linkedin.com/in/yurytsarev/)
- [Millennium bcp case study](https://www.cncf.io/case-studies/millennium-bcp/)

