"""Ownership, resumability and isolated access for the live setup."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import bootstrap
import prepare
import presenter


class SetupTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.runtime = Path(directory.name)
        runtime_patch = patch.object(bootstrap, "RUNTIME", self.runtime)
        runtime_patch.start()
        self.addCleanup(runtime_patch.stop)
        env_patch = patch.dict(os.environ)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        output = contextlib.redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)

    def cluster(self, owner=None):
        return {"Id": "owned-server", "Config": {"Labels": {bootstrap.LABEL: owner or bootstrap.OWNER}},
                "State": {"Running": True}}

    def test_foreign_name_collision_stops_before_any_mutation(self):
        def capture(argv, **kwargs):
            if argv[:3] == ["k3d", "cluster", "list"]:
                return json.dumps([{"name": "test-gslb2"}])
            return json.dumps([self.cluster("another-project")])
        with patch.object(bootstrap, "capture", side_effect=capture), patch.object(bootstrap, "run") as run:
            with self.assertRaisesRegex(ValueError, "refusing to adopt"):
                bootstrap.ensure_clusters()
            run.assert_not_called()
        self.assertFalse((self.runtime / "kubeconfig").exists())

    def test_resume_creates_only_missing_cluster_and_writes_private_kubeconfig(self):
        def capture(argv, **kwargs):
            if argv[:3] == ["k3d", "cluster", "list"]:
                return json.dumps([{"name": "edgedns"}, {"name": "test-gslb1"}])
            if argv[:3] == ["k3d", "kubeconfig", "get"]:
                return json.dumps({field: [{"name": "k3d-" + argv[-1]}] for field in ("clusters", "contexts", "users")})
            if "sysctl" in argv:
                return "1048576"
            if argv[:3] == ["docker", "exec", "k3d-edgedns-server-0"]:
                self.assertEqual(argv[3:], ["crictl", "info"])
                return "{}"
            if argv[0] == "kubectl":
                count = 1 if "k3d-edgedns" in argv else 2
                return json.dumps({"items": [{"status": {"conditions": [{"type": "Ready", "status": "True"}]}}] * count})
            return json.dumps([self.cluster()])
        with patch.object(bootstrap, "capture", side_effect=capture), patch.object(bootstrap, "run") as run:
            ids = bootstrap.ensure_clusters()
        creates = [call.args[0] for call in run.call_args_list if call.args[0][2] == "create"]
        self.assertEqual(len(creates), 1)
        self.assertIn("test-gslb2.yaml", creates[0][4])
        self.assertIn("--kubeconfig-update-default=false", creates[0])
        self.assertEqual(set(ids), {"edgedns", "test-gslb1", "test-gslb2"})
        self.assertFalse(any(call.args[0][:2] == ["docker", "restart"] for call in run.call_args_list))
        path = self.runtime / "kubeconfig"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(os.environ["KUBECONFIG"], str(path))
        import yaml
        config = yaml.safe_load(path.read_text())
        self.assertEqual([entry["name"] for entry in config["contexts"]],
                         ["k3d-edgedns", "k3d-test-gslb1", "k3d-test-gslb2"])

    def test_deployment_marker_is_written_only_after_success_and_invalidates_on_recreation(self):
        source = self.runtime / "source"
        deployment = source / "deploy/edge/deployment.yaml"
        deployment.parent.mkdir(parents=True)
        deployment.write_text('spec:\n  template:\n    spec:\n      containers:\n      - name: bind\n')
        source_patch = patch.object(bootstrap, "SOURCE", source)
        source_patch.start()
        self.addCleanup(source_patch.stop)
        ids = {"edgedns": "original"}
        with patch.object(bootstrap, "run", side_effect=RuntimeError("install failed")):
            with self.assertRaises(RuntimeError):
                bootstrap.deploy(ids)
        self.assertFalse((self.runtime / "deployed.json").exists())
        with patch.object(bootstrap, "run") as run:
            bootstrap.deploy(ids)
            first = run.call_count
            bootstrap.deploy(ids)
            self.assertEqual(run.call_count, first)
            bootstrap.deploy({"edgedns": "replacement"})
            self.assertEqual(run.call_count, first * 2)
            bootstrap.deploy({"edgedns": "replacement"}, refresh=True)
            self.assertEqual(run.call_count, first * 3)

    def test_watch_limits_are_only_raised_when_needed(self):
        with patch.object(bootstrap, "capture", side_effect=["128", "2097152"]), patch.object(bootstrap, "run") as run:
            bootstrap.ensure_watch_limits("owned-node")
        run.assert_called_once_with(["docker", "exec", "owned-node", "sysctl", "-w", "fs.inotify.max_user_instances=8192"])

    def test_new_zone_nxdomain_is_retried_until_both_local_targets_exist(self):
        cfg = presenter.preview_config(json.loads((Path(__file__).parent / "config.json").read_text()))
        empty = ";; ->>HEADER<<- opcode: QUERY, status: NXDOMAIN, id: 1\n;; flags: qr;\n"
        answer = presenter.synthetic_dns(cfg, "A")
        with patch.object(bootstrap, "capture", side_effect=[answer, empty, answer, answer, presenter.synthetic_dns(cfg, "B")]) as read, \
                patch.object(bootstrap.time, "sleep") as sleep:
            bootstrap.wait_targets(cfg, 30)
        self.assertEqual(read.call_count, 5)
        sleep.assert_called_once_with(3)

    def test_second_setup_cannot_acquire_live_presenter_lock(self):
        with bootstrap.lock("beats.lock"):
            with self.assertRaisesRegex(ValueError, "Another setup/presenter"):
                with bootstrap.lock("beats.lock"):
                    self.fail("A concurrent setup must not change the playground")

    def test_recording_never_overwrites_existing_evidence(self):
        path = self.runtime / "observations.jsonl"
        path.write_text("evidence")
        with self.assertRaises(ValueError):
            bootstrap.recording_path(path)
        self.assertEqual(path.read_text(), "evidence")

    def test_observer_reuse_requires_matching_checkout_and_configuration(self):
        info = {"Config": {"Labels": {"k8gb-talk-demo": "observer", "k8gb-demo-fingerprint": "same"}},
                "Mounts": [{"Source": str(self.runtime / "config.json")}]}
        self.assertTrue(prepare.reusable_observer(info, "same", self.runtime))
        self.assertFalse(prepare.reusable_observer(info, "changed", self.runtime))
        foreign = copy.deepcopy(info)
        foreign["Mounts"][0]["Source"] = "/another-checkout/config.json"
        with self.assertRaises(ValueError):
            prepare.reusable_observer(foreign, "same", self.runtime)
        # Old whole-directory mounts would expose the new kubeconfig. Never reuse.
        info["Mounts"][0]["Source"] = str(self.runtime)
        with self.assertRaises(ValueError):
            prepare.reusable_observer(info, "same", self.runtime)

    def test_tmux_panes_receive_the_selected_kubeconfig_even_with_an_existing_server(self):
        config = self.runtime / "config.json"
        config.write_text((Path(__file__).parent / "config.json").read_text())
        args = SimpleNamespace(config=config, preview=False, record=None, control_file=self.runtime / "control.json",
                               timeout=180, no_notes=False, no_clear=False, detach=True)
        selected = str(self.runtime / "kubeconfig with spaces")
        def tmux(argv, **kwargs):
            return "%8" if argv[1] == "new-session" else "%9" if argv[1] == "split-window" else ""
        with patch.dict(os.environ, {"KUBECONFIG": selected}), patch.object(presenter.shutil, "which", return_value="tmux"), \
                patch.object(presenter.shutil, "get_terminal_size", return_value=os.terminal_size((160, 44))), \
                patch.object(presenter.subprocess, "check_output", side_effect=tmux) as execute:
            presenter.stage(args)
        import shlex
        panes = [c.args[0][-1] for c in execute.call_args_list if c.args[0][1] in ("new-session", "split-window")]
        self.assertEqual(len(panes), 2)
        for command in panes:
            self.assertEqual(shlex.split(command)[:3], ["exec", "env", "KUBECONFIG=" + selected])


if __name__ == "__main__":
    unittest.main()
