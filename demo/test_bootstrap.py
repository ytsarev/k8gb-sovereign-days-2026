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

import yaml

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

    def test_moved_checkout_preserves_saved_cluster_identity(self):
        original = "0123456789abcdef"
        (self.runtime / "owner-id").write_text(original + "\n")
        self.assertEqual(bootstrap.cluster_owned(self.cluster(original)), "owned-server")
        with self.assertRaisesRegex(ValueError, "refusing to adopt"):
            bootstrap.cluster_owned(self.cluster("another-project"))
        (self.runtime / "owner-id").write_text("invalid")
        with self.assertRaisesRegex(ValueError, "Invalid demo ownership"):
            bootstrap.cluster_owned(self.cluster(original))

    def test_observer_mount_accepts_compatibility_symlink(self):
        moved = self.runtime / "moved"
        moved.mkdir()
        old = self.runtime / "old"
        old.symlink_to(moved, target_is_directory=True)
        info = {"Config": {"Labels": {"k8gb-talk-demo": "observer", "k8gb-demo-fingerprint": "same"}},
                "Mounts": [{"Source": str(old / "config.json")}]}
        self.assertTrue(prepare.reusable_observer(info, "same", moved))
        with self.assertRaisesRegex(ValueError, "not owned"):
            prepare.reusable_observer(info, "same", self.runtime / "unrelated")

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

    def demo_kubeconfig(self):
        config = {"apiVersion": "v1", "kind": "Config", "current-context": "k3d-test-gslb2",
                  "clusters": [], "users": [], "contexts": []}
        for cluster, _ in bootstrap.CLUSTERS:
            name = "k3d-" + cluster
            config["clusters"].append({"name": name, "cluster": {"server": "https://127.0.0.1:12345"}})
            config["users"].append({"name": name, "user": {"token": "demo-test-token"}})
            config["contexts"].append({"name": name, "context": {"cluster": name, "user": name}})
        (self.runtime / "kubeconfig").write_text(yaml.safe_dump(config))

    def test_merge_creates_default_config_despite_private_kubeconfig_environment(self):
        self.demo_kubeconfig()
        home = self.runtime / "home"
        private = str(self.runtime / "kubeconfig")
        with patch.object(bootstrap.Path, "home", return_value=home), patch.dict(os.environ, {"KUBECONFIG": private}):
            bootstrap.merge_kubeconfig()
            self.assertEqual(os.environ["KUBECONFIG"], private)
        path = home / ".kube/config"
        config = yaml.safe_load(path.read_text())
        self.assertEqual(config["current-context"], "k3d-test-gslb1")
        self.assertEqual(len(config["contexts"]), 3)
        for entry in config["contexts"]:
            if entry["name"] != "k3d-edgedns":
                self.assertEqual(entry["context"]["namespace"], "test-gslb")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(list(path.parent.glob("config.before-k8gb-*")), [])

    def test_merge_refreshes_demo_preserves_other_settings_and_is_idempotent(self):
        self.demo_kubeconfig()
        home = self.runtime / "home"
        path = home / ".kube/config"
        path.parent.mkdir(parents=True)
        old = {"current-context": "work", "preferences": {"colors": True},
               "clusters": [{"name": "work", "cluster": {"server": "https://work.example", "certificate-authority": "ca.pem"}},
                            {"name": "k3d-test-gslb1", "cluster": {"server": "https://old.example"}}],
               "users": [{"name": "work", "user": {"exec": {"command": "auth-helper"}}}],
               "contexts": [{"name": "work", "context": {"cluster": "work", "user": "work", "namespace": "mine"}},
                            {"name": "k3d-test-gslb1", "context": {"cluster": "k3d-test-gslb1", "namespace": "custom"}}]}
        original = yaml.safe_dump(old).encode()
        path.write_bytes(original)
        with patch.object(bootstrap.Path, "home", return_value=home):
            bootstrap.merge_kubeconfig()
            merged = path.read_bytes()
            bootstrap.merge_kubeconfig()
        self.assertEqual(path.read_bytes(), merged)
        config = yaml.safe_load(merged)
        self.assertEqual(config["current-context"], "work")
        self.assertEqual(config["preferences"], old["preferences"])
        for field in ("clusters", "users", "contexts"):
            self.assertEqual(config[field][0], old[field][0])
        self.assertEqual(config["clusters"][1]["cluster"]["server"], "https://127.0.0.1:12345")
        self.assertEqual(config["contexts"][1]["context"]["namespace"], "custom")
        self.assertEqual(len(config["contexts"]), 4)
        backups = list(path.parent.glob("config.before-k8gb-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_merge_rejects_invalid_existing_config_without_overwriting_it(self):
        self.demo_kubeconfig()
        home = self.runtime / "home"
        path = home / ".kube/config"
        path.parent.mkdir(parents=True)
        for original in (b"- not-a-config\n", b"contexts: not-a-list\n"):
            with self.subTest(original=original):
                path.write_bytes(original)
                with patch.object(bootstrap.Path, "home", return_value=home), self.assertRaises(ValueError):
                    bootstrap.merge_kubeconfig()
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(list(path.parent.glob("config.before-k8gb-*")), [])

    def test_failed_atomic_replace_preserves_original_and_symlink(self):
        self.demo_kubeconfig()
        home = self.runtime / "home"
        path = home / ".kube/config"
        path.parent.mkdir(parents=True)
        target = path.with_name("shared-config")
        original = b"apiVersion: v1\nkind: Config\ncurrent-context: work\n"
        target.write_bytes(original)
        path.symlink_to(target)
        with patch.object(bootstrap.Path, "home", return_value=home), \
                patch.object(bootstrap.os, "replace", side_effect=OSError("replace failed")), \
                self.assertRaisesRegex(OSError, "replace failed"):
            bootstrap.merge_kubeconfig()
        self.assertTrue(path.is_symlink())
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(list(path.parent.glob(".k8gb-config-*")), [])
        with patch.object(bootstrap.Path, "home", return_value=home):
            bootstrap.merge_kubeconfig()
        self.assertTrue(path.is_symlink())
        self.assertEqual(len(yaml.safe_load(target.read_text())["contexts"]), 3)

    def test_merge_flag_is_opt_in_for_all_setup_commands_and_runs_after_verification(self):
        for command in ("setup", "up", "rehearse"):
            for enabled in (False, True):
                for success in (False, True):
                    with self.subTest(command=command, enabled=enabled, success=success), contextlib.ExitStack() as stack:
                        argv = ["bootstrap.py", command] + (["--merge-kubeconfig"] if enabled else [])
                        stack.enter_context(patch.object(bootstrap.sys, "argv", argv))
                        stack.enter_context(patch.object(bootstrap.shutil, "which", return_value="/bin/tool"))
                        for name in ("capture", "ensure_python", "recording_path", "ensure_source", "ensure_clusters", "deploy", "rehearse"):
                            stack.enter_context(patch.object(bootstrap, name))
                        stack.enter_context(patch.object(bootstrap.os, "execv"))
                        stack.enter_context(patch.object(bootstrap, "verify_and_prepare", side_effect=None if success else RuntimeError("not ready")))
                        merge = stack.enter_context(patch.object(bootstrap, "merge_kubeconfig"))
                        if success:
                            bootstrap.main()
                        else:
                            with self.assertRaisesRegex(RuntimeError, "not ready"):
                                bootstrap.main()
                        self.assertEqual(merge.call_count, int(enabled and success))

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
