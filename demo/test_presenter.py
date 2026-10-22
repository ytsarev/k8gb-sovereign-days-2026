"""Presenter behavior and narrow traffic layout; no real clusters are changed."""
import contextlib
import copy
import io
import json
from pathlib import Path
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import yaml

import core
import dashboard
import presenter


CFG = presenter.preview_config(core.load_config(Path(__file__).parent / "config.json"))


def options(directory, preview=False):
    return SimpleNamespace(preview=preview, auto=0, no_clear=True, no_notes=False,
                           timeout=1, control_file=Path(directory) / "control.json")


class Playground:
    """Responses at command boundaries, including native YAML and dig parsing."""
    def __init__(self):
        self.replicas = 3
        self.uid = "deployment-a"
        self.commands = []
        self.fail_observe = False

    def run(self, argv, **kwargs):
        self.commands.append(argv)
        key = "B" if CFG["clusters"]["B"]["context"] in argv else "A"
        output, rc = "", 0
        if argv[0] == "kubectl":
            if "scale" in argv:
                self.assert_current(argv)
                self.replicas = int(next(a.split("=", 1)[1] for a in argv if a.startswith("--replicas=")))
                output = "deployment scaled"
            elif "rollout" in argv:
                output = "deployment successfully rolled out"
            elif "deployment" in argv:
                replicas = self.replicas if key == "A" else 2
                output = json.dumps({"metadata": {"name": CFG["deployment"], "uid": self.uid if key == "A" else "deployment-b"},
                                     "spec": {"replicas": replicas}, "status": {"readyReplicas": replicas}})
            elif "gslbs.k8gb.io" in argv:
                output = json.dumps({"apiVersion": "k8gb.io/v1beta1", "kind": "Gslb",
                                     "metadata": {"name": CFG["gslb"], "namespace": CFG["namespace"]},
                                     "spec": {"strategy": {"type": "failover", "primaryGeoTag": CFG["clusters"]["A"]["geotag"]}},
                                     "status": {"geoTag": CFG["clusters"][key]["geotag"], "hosts": CFG["hostname"]}})
            elif "endpointslices" in argv:
                output, rc = json.dumps({"items": []}), int(self.fail_observe)
            else:
                raise AssertionError("Unexpected command: " + repr(argv))
        elif argv[:2] == ["docker", "exec"]:
            key = "B" if "@" + CFG["clusters"]["B"]["dns_address"] in argv else "A"
            output = presenter.synthetic_dns(CFG, key, short="+short" in argv)
        elif argv[:2] == ["docker", "logs"]:
            key = "A" if self.replicas else "B"
            output = "\n".join(json.dumps({"source": k, "data": v}) for k, v in observations(key).items())
        else:
            raise AssertionError("Unexpected command: " + repr(argv))
        if argv[0] == "kubectl" and "get" in argv:
            if argv[-2:] != ["-o", "yaml"]:
                raise AssertionError("Presenter resource reads must request native YAML")
            output = yaml.safe_dump(json.loads(output), sort_keys=True)
        return subprocess.CompletedProcess(argv, rc, output, "injected failure" if rc else "")

    def assert_current(self, argv):
        expected = next(a.split("=", 1)[1] for a in argv if a.startswith("--current-replicas="))
        if int(expected) != self.replicas:
            raise AssertionError("Scale did not preserve the observed replica precondition")


def observations(key, clock=None):
    clock = time.time() if clock is None else clock
    result = {}
    for source in ("dns_A", "dns_B", "resolver"):
        result[source] = {"ok": True, "at": clock, "authoritative": source != "resolver",
                          "records": [{"ip": CFG["clusters"][key]["dns_address"], "ttl": 10}]}
    result["request"] = {"ok": True, "at": clock, "success": True, "status": 200, "served_by": key}
    return result


class PresenterTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state_path = Path(self.directory.name) / "state.json"
        self.state_patch = patch.object(presenter, "STATE", self.state_path)
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)
        self.output = io.StringIO()
        self.stdout = contextlib.redirect_stdout(self.output)
        self.stdout.__enter__()
        self.addCleanup(self.stdout.__exit__, None, None, None)

    def test_complete_live_flow_restores_exact_replica_count(self):
        fake = Playground()
        p = presenter.Presenter(CFG, options(self.directory.name))
        with patch.object(presenter.subprocess, "run", side_effect=fake.run):
            p.run()
        scales = [a for a in fake.commands if "scale" in a]
        self.assertEqual(len(scales), 2)
        self.assertIn("--replicas=0", scales[0])
        self.assertIn("--replicas=3", scales[1])
        self.assertEqual(fake.replicas, 3)
        self.assertFalse(self.state_path.exists())
        self.assertIn("A → B → A complete", self.output.getvalue())
        self.assertIn("replicas: 3", self.output.getvalue())
        self.assertIn("readyReplicas: 3", self.output.getvalue())
        self.assertNotIn("JSON response", self.output.getvalue())
        self.assertNotIn("desired:", self.output.getvalue())
        for argv in fake.commands:
            if argv[0] == "kubectl":
                self.assertIn("--context", argv)
                self.assertTrue(any(a.startswith("--request-timeout=") for a in argv))

    def test_invalid_yaml_stops_before_any_mutation(self):
        for output in ("metadata: [", "- not a Kubernetes object"):
            with self.subTest(output=output):
                p = presenter.Presenter(CFG, options(self.directory.name))
                result = subprocess.CompletedProcess([], 0, output, "")
                with patch.object(presenter.subprocess, "run", return_value=result) as execute:
                    with self.assertRaises(ValueError):
                        p.run()
                self.assertEqual(execute.call_count, 1)
                self.assertEqual(execute.call_args.args[0][-2:], ["-o", "yaml"])
                self.assertFalse(self.state_path.exists())

    def test_failure_after_scaling_restores_before_exit(self):
        fake = Playground()
        fake.fail_observe = True
        p = presenter.Presenter(CFG, options(self.directory.name))
        with patch.object(presenter.subprocess, "run", side_effect=fake.run):
            with self.assertRaisesRegex(RuntimeError, "injected failure"):
                p.run()
        self.assertEqual(fake.replicas, 3)
        self.assertFalse(self.state_path.exists())
        self.assertNotIn("A → B → A complete", self.output.getvalue())

    def test_interrupt_after_mutation_restores_without_another_enter(self):
        fake = Playground()
        p = presenter.Presenter(CFG, options(self.directory.name))
        with patch.object(presenter.subprocess, "run", side_effect=fake.run), patch.object(p, "observe", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                p.run()
        self.assertEqual(fake.replicas, 3)
        self.assertFalse(self.state_path.exists())

    def test_quitting_at_scale_prompt_never_mutates(self):
        fake = Playground()
        p = presenter.Presenter(CFG, options(self.directory.name))
        p.original, p.uid, p.args.auto = 3, fake.uid, None
        with patch.object(presenter.subprocess, "run", side_effect=fake.run), patch("sys.stdin", io.StringIO("\nq\n")):
            with self.assertRaises(presenter.StopDemo):
                p.failover()
        self.assertFalse(any("scale" in a for a in fake.commands))
        self.assertFalse(self.state_path.exists())

    def test_eof_never_executes_a_displayed_command(self):
        p = presenter.Presenter(CFG, options(self.directory.name))
        p.args.auto = None
        with patch.object(presenter.subprocess, "run") as execute, patch("sys.stdin", io.StringIO("")):
            with self.assertRaises(presenter.StopDemo):
                p.command(["kubectl", "get", "pods"])
        execute.assert_not_called()
        self.assertIn("$ kubectl get pods", self.output.getvalue())

    def test_replaced_workload_keeps_recovery_record_without_scaling(self):
        fake = Playground()
        fake.uid = "new-deployment"
        saved = {"config": CFG, "uid": "old-deployment", "replicas": 3}
        presenter.save_json(self.state_path, saved)
        p = presenter.Presenter(CFG, options(self.directory.name))
        p.pending = saved
        with patch.object(presenter.subprocess, "run", side_effect=fake.run):
            with self.assertRaisesRegex(ValueError, "replaced"):
                p.restore(emergency=True)
        self.assertTrue(self.state_path.exists())
        self.assertFalse(any("scale" in a for a in fake.commands))

    def test_timeout_is_failure_and_does_not_claim_recovery(self):
        p = presenter.Presenter(CFG, options(self.directory.name))
        p.targets = {"B": [CFG["clusters"]["B"]["dns_address"]]}
        with patch.object(presenter.time, "monotonic", side_effect=[0, 0, 0, 2]), patch.object(presenter.time, "sleep"), patch.object(presenter.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")):
            with self.assertRaisesRegex(RuntimeError, "No verified transition"):
                p.wait_for("B", time.time())
        self.assertNotIn("Verified:", self.output.getvalue())

    def test_preview_never_executes_commands_or_creates_live_recovery_state(self):
        p = presenter.Presenter(CFG, options(self.directory.name, preview=True))
        with patch.object(presenter.subprocess, "run") as execute:
            p.run()
        execute.assert_not_called()
        self.assertFalse(self.state_path.exists())
        cue = json.loads(p.args.control_file.read_text())
        self.assertEqual(cue["offset"], 46)
        self.assertEqual(cue["beat"], "7 / RESTORED")


class ConvergenceTests(unittest.TestCase):
    def test_short_targets_reject_dns_errors_and_non_address_output(self):
        self.assertEqual(presenter.short_targets("172.20.0.5\n172.20.0.3\n172.20.0.5\n"),
                         ["172.20.0.3", "172.20.0.5"])
        self.assertEqual(presenter.short_targets("\n"), [])
        for answer in (";; connection timed out", "alias.example.com.", "NXDOMAIN", "999.1.2.3"):
            with self.subTest(answer=answer), self.assertRaises(ValueError):
                presenter.short_targets(answer)

    def test_pinned_plugin_without_aa_requires_the_expected_direct_server(self):
        cfg = copy.deepcopy(CFG)
        cfg["require_authoritative_flag"] = False
        data = observations("B", 100)
        ips = [cfg["clusters"]["B"]["dns_address"]]
        for key in ("A", "B"):
            data["dns_" + key].update(authoritative=False, server=cfg["clusters"][key]["dns_address"], port=53)
        self.assertTrue(presenter.converged(data, cfg, "B", ips, 99, 100))
        self.assertFalse(presenter.converged(data, CFG, "B", ips, 99, 100))
        data["dns_A"]["server"] = "127.0.0.1"
        self.assertFalse(presenter.converged(data, cfg, "B", ips, 99, 100))

    def test_requires_fresh_matching_authorities_cache_and_real_http(self):
        data = observations("B", 100)
        ips = [CFG["clusters"]["B"]["dns_address"]]
        self.assertTrue(presenter.converged(data, CFG, "B", ips, 99, 100))
        self.assertFalse(presenter.converged(data, CFG, "B", ips, 101, 100))
        self.assertFalse(presenter.converged(data, CFG, "B", ips, 99, 120))
        for source, changes in (("dns_A", {"authoritative": False}), ("resolver", {"records": []}),
                                ("request", {"served_by": "A"}), ("request", {"success": False}),
                                ("request", {"served_by": None}), ("dns_B", {"ok": False})):
            altered = copy.deepcopy(data)
            altered[source].update(changes)
            self.assertFalse(presenter.converged(altered, CFG, "B", ips, 99, 100))


class CompactTests(unittest.TestCase):
    def frame(self, offset, extra=0):
        start, messages = dashboard.preview_messages(CFG)
        state = dashboard.State(CFG, "ILLUSTRATIVE PREVIEW", start)
        for msg in messages:
            if msg["data"]["at"] <= start + offset:
                state.apply(msg)
        return dashboard.render_compact(state, 64, 40, start + offset + extra).plain()

    def test_cache_lag_failures_and_restoration_remain_visible_in_narrow_pane(self):
        text = self.frame(18)
        self.assertIn("REQUEST FAILED", text)
        self.assertIn("Cache differs from authorities", text)
        self.assertIn("failed 5", text)
        self.assertIn("CLUSTER B", self.frame(26))
        self.assertIn("CLUSTER A", self.frame(46))
        self.assertIn("STALE", self.frame(46, 20))
        self.assertNotIn("DNS answers agree", self.frame(46, 20))

    def test_preview_holds_until_enter_then_advances_only_within_the_beat(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cue.json"
            self.assertEqual(dashboard.controlled_preview(path, 100)[0], 0)
            presenter.save_json(path, {"offset": 10, "until": 30, "at": 100, "beat": "FAILOVER"})
            self.assertEqual(dashboard.controlled_preview(path, 101), (11, "FAILOVER"))
            self.assertEqual(dashboard.controlled_preview(path, 500), (30, "FAILOVER"))
            presenter.save_json(path, {"offset": 0, "until": 0, "at": 500, "beat": "RESET"})
            self.assertEqual(dashboard.controlled_preview(path, 600), (0, "RESET"))


if __name__ == "__main__":
    unittest.main()
