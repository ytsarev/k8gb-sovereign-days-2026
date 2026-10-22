"""Recorded playback stays offline and preserves the Enter-controlled timeline."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import dashboard
import offline
import presenter
from test_presenter import CFG, Playground, options
from transcript import Transcript, transcript_path


class OfflineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "traffic.jsonl"
        self.control = Path(self.directory.name) / "control.json"
        self.events = []
        at = 100.0
        for number in range(1, 8):
            for event in ({"kind": "beat", "number": number, "title": "Beat %s" % number, "note": "Captured note"},
                          {"kind": "command", "argv": ["kubectl", "scale", "demo", "--replicas=0"]},
                          {"kind": "pause", "label": "Enter to run"}, {"kind": "resume"},
                          {"kind": "yaml", "data": {"spec": {"replicas": 0}}},
                          {"kind": "line", "text": "captured command output", "color": "green", "bold": False}):
                at += 0.1
                self.events.append({**event, "at": at})
        self.events.append({"kind": "complete", "at": at + 0.1})
        self.write_session()

    def write_session(self, start=100.0, commands_start=100.0, outcomes=("A", "B", "A")):
        with self.path.open("w") as f:
            f.write(json.dumps({"schema": "k8gb-cli-v1", "origin": "live", "start": start, "config": CFG}) + "\n")
            for i, target in enumerate(outcomes):
                at = start + i + 1
                f.write(json.dumps({"source": "request", "received_at": at,
                                    "data": {"at": at, "ok": True, "success": True, "status": 200, "served_by": target}}) + "\n")
        with transcript_path(self.path).open("w") as f:
            f.write(json.dumps({"schema": "k8gb-commands-v1", "origin": "live", "start": commands_start}) + "\n")
            for event in self.events:
                f.write(json.dumps(event) + "\n")

    def test_enter_playback_never_executes_commands_and_freezes_traffic_while_waiting(self):
        class Input:
            count = 0
            def readline(inner):
                inner.count += 1
                cue = json.loads(self.control.read_text())
                self.assertEqual(cue["offset"], cue["until"])
                offset, _ = dashboard.controlled_preview(self.control, cue["at"] + 600, 3)
                self.assertEqual(offset, cue["offset"])
                return "\n"
        input_stream = Input()
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch("sys.stdin", input_stream), \
                patch.object(offline.time, "sleep"), \
                patch.object(offline.subprocess, "run", side_effect=AssertionError("Offline called a command")), \
                patch.object(offline.subprocess, "Popen", side_effect=AssertionError("Offline started a process")), \
                patch.object(offline.subprocess, "check_output", side_effect=AssertionError("Offline called a command")):
            offline.play(self.path, self.control, no_clear=True)
        self.assertEqual(input_stream.count, 7)
        self.assertIn("RECORDED REPLAY", output.getvalue())
        self.assertIn("spec:\n  replicas: 0", output.getvalue())
        self.assertEqual(output.getvalue().count("captured command output"), 7)
        self.assertIn("Recorded session complete", output.getvalue())
        cue = json.loads(self.control.read_text())
        self.assertEqual(cue["offset"], 3)
        self.assertEqual(cue["beat"], "REPLAY COMPLETE")

    def test_quit_at_enter_leaves_recording_intact_and_stops_traffic(self):
        before = self.path.read_bytes()
        with contextlib.redirect_stdout(io.StringIO()), patch("sys.stdin", io.StringIO("q\n")), \
                patch.object(offline.time, "sleep"), self.assertRaises(presenter.StopDemo):
            offline.play(self.path, self.control)
        self.assertEqual(self.path.read_bytes(), before)
        cue = json.loads(self.control.read_text())
        self.assertEqual(cue["offset"], cue["until"])
        self.assertEqual(cue["beat"], "REPLAY STOPPED")

    def test_incomplete_or_mismatched_recording_is_rejected(self):
        self.write_session(commands_start=99)
        with self.assertRaisesRegex(ValueError, "same live run"):
            offline.load_session(self.path)
        self.events.pop()
        self.write_session()
        with self.assertRaisesRegex(ValueError, "incomplete"):
            offline.load_session(self.path)

    def test_traffic_requires_recorded_failover_and_restoration(self):
        self.write_session(outcomes=("A", "B"))
        with self.assertRaisesRegex(ValueError, "successful A"):
            offline.load_session(self.path)

    def test_old_traffic_only_capture_has_actionable_error(self):
        transcript_path(self.path).unlink()
        with self.assertRaisesRegex(ValueError, "rehearse"):
            offline.load_session(self.path)

    def test_controlled_replay_supports_long_runs_and_rejects_out_of_range_cues(self):
        self.control.write_text(json.dumps({"offset": 90, "until": 120, "at": 1000, "beat": "RESTORE"}))
        self.assertEqual(dashboard.controlled_preview(self.control, 1010, 150), (100, "RESTORE"))
        self.assertEqual(dashboard.controlled_preview(self.control, 2000, 150), (120, "RESTORE"))
        self.assertEqual(dashboard.controlled_preview(self.control, 1010, 100), (0, "WAITING FOR PRESENTER"))

    def test_presenter_captures_displayed_yaml_commands_pauses_and_success(self):
        # Use a new path: capture must never overwrite earlier evidence.
        record = Transcript(Path(self.directory.name) / "new.commands.jsonl", time.time())
        p = presenter.Presenter(CFG, options(self.directory.name), record)
        fake = Playground()
        with contextlib.redirect_stdout(io.StringIO()), patch.object(presenter, "STATE", Path(self.directory.name) / "state.json"), \
                patch.object(presenter.subprocess, "run", side_effect=fake.run):
            p.run()
        record.close()
        events = [json.loads(line) for line in (Path(self.directory.name) / "new.commands.jsonl").read_text().splitlines()][1:]
        self.assertEqual(events[-1]["kind"], "complete")
        self.assertEqual([e["number"] for e in events if e["kind"] == "beat"], list(range(1, 8)))
        self.assertEqual(sum(e["kind"] == "pause" for e in events), sum(e["kind"] == "resume" for e in events))
        commands = [e["argv"] for e in events if e["kind"] == "command"]
        self.assertTrue(any("scale" in argv and "--replicas=0" in argv for argv in commands))
        self.assertTrue(any("scale" in argv and "--replicas=3" in argv for argv in commands))
        yaml_events = [e["data"] for e in events if e["kind"] == "yaml"]
        self.assertTrue(any(data.get("spec", {}).get("replicas") == 3 for data in yaml_events))
        self.assertFalse(any("uid" in data.get("metadata", {}) for data in yaml_events))

    def test_capture_write_failure_still_restores_live_replicas(self):
        class FailingRecorder:
            def append(self, kind, **fields):
                if kind == "line" and "scaled" in fields.get("text", ""):
                    raise OSError("disk full")
        fake = Playground()
        p = presenter.Presenter(CFG, options(self.directory.name), FailingRecorder())
        state = Path(self.directory.name) / "state.json"
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), \
                patch.object(presenter, "STATE", state), patch.object(presenter.subprocess, "run", side_effect=fake.run):
            with self.assertRaisesRegex(RuntimeError, "recording is incomplete"):
                p.run()
        self.assertEqual(fake.replicas, 3)
        self.assertFalse(state.exists())


if __name__ == "__main__":
    unittest.main()
