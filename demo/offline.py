#!/usr/bin/env python3
"""Enter-paced playback of captured commands and traffic. Never executes recorded commands."""
import argparse
import json
import math
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from types import SimpleNamespace

from core import clean
from labels import cluster_mapping
from dashboard import replay_file
from presenter import Presenter, StopDemo, save_json, yaml_rows
from transcript import transcript_path

HERE = Path(__file__).resolve().parent
DEFAULT = HERE / "fallback/traffic.jsonl"


def load_session(recording):
    header, messages = replay_file(recording)
    path = transcript_path(recording)
    if not path.exists():
        raise ValueError("This recording has no command capture. Run ./demo/demo.sh rehearse to capture both panes.")
    with path.open(encoding="utf-8") as stream:
        transcript = json.loads(next(stream))
        events = [json.loads(line) for line in stream if line.strip()]
    if (transcript.get("schema") != "k8gb-commands-v1" or transcript.get("origin") != "live"
            or transcript.get("start") != header["start"]):
        raise ValueError("Command and traffic recordings are not from the same live run")
    if not events or events[-1].get("kind") != "complete":
        raise ValueError("Command recording is incomplete; use a successful rehearsal")
    previous, waiting, beats = header["start"], False, []
    for event in events:
        at = event.get("at")
        if not isinstance(at, (int, float)) or not math.isfinite(at) or at < previous:
            raise ValueError("Invalid command timeline")
        previous = at
        kind = event.get("kind")
        if kind == "beat":
            beats.append(event["number"])
            if not isinstance(event["title"], str) or not isinstance(event["note"], str):
                raise ValueError("Invalid recorded beat")
        elif kind == "command":
            if not isinstance(event.get("argv"), list) or not event["argv"] or not all(isinstance(a, str) for a in event["argv"]):
                raise ValueError("Invalid recorded command")
        elif kind == "line":
            if not isinstance(event.get("text"), str):
                raise ValueError("Invalid recorded output")
        elif kind == "yaml":
            if not isinstance(event.get("data"), dict):
                raise ValueError("Invalid recorded YAML")
        elif kind == "pause":
            if waiting:
                raise ValueError("Missing recorded resume")
            waiting = True
        elif kind == "resume":
            if not waiting:
                raise ValueError("Missing recorded pause")
            waiting = False
        elif kind != "complete":
            raise ValueError("Unknown command recording event")
    if waiting or beats != list(range(1, 8)):
        raise ValueError("Recording must contain all seven completed beats")
    outcomes = [m["data"].get("served_by") for m in messages if m["source"] == "request" and m["data"].get("success")]
    if not outcomes or outcomes[0] != "A" or "B" not in outcomes or outcomes[-1] != "A":
        raise ValueError("Traffic recording does not contain the successful A → B → A sequence")
    return header, messages, events


def play(recording, control, no_clear=False, no_notes=False):
    header, messages, events = load_session(recording)
    ui = Presenter({}, SimpleNamespace(auto=None), None)  # Formatting and input only.
    start = header["start"]
    end = max(m.get("received_at", m["data"]["at"]) for m in messages) - start
    origin, wall, beat = start, time.monotonic(), "WAITING FOR ENTER"

    def cue(at, until, label):
        if control:
            save_json(control, {"offset": min(end, max(0, at - start)),
                               "until": min(end, max(0, until - start)),
                               "at": time.time(), "beat": label})

    def next_pause(index):
        return next((e["at"] for e in events[index + 1:] if e["kind"] == "pause"), start + end)

    cue(start, next_pause(-1), beat)
    current = start
    try:
        for index, event in enumerate(events):
            kind, at = event["kind"], event["at"]
            if kind == "resume":
                # Human dwell at Enter is outside recorded time. Resume at the
                # captured action and preserve all subsequent observation timing.
                origin, wall = at, time.monotonic()
                cue(at, next_pause(index), beat)
                continue
            delay = at - origin - (time.monotonic() - wall)
            if delay > 0:
                time.sleep(delay)
            current = at
            if kind == "pause":
                cue(at, at, "PAUSED · " + beat)
                ui.pause("Enter to replay the next step · q to stop")
            elif kind == "beat":
                beat = "%s / %s" % (event["number"], clean(event["title"]).upper())
                if ui.color and not no_clear:
                    print("\033[2J\033[H", end="")
                ui.line("\n" + "─" * min(72, shutil.get_terminal_size().columns - 2), "teal")
                ui.line("  " + beat, "teal", True)
                ui.line("  RECORDED REPLAY · captured live output · commands are not executed", "amber")
                ui.line("  " + cluster_mapping(header["config"]), "teal")
                ui.line()
                if not no_notes:
                    for line in textwrap.wrap(clean(event["note"], 4000), max(30, min(76, shutil.get_terminal_size().columns - 6))):
                        ui.line("  " + line, "muted")
                    ui.line()
                cue(at, next_pause(index), beat)
            elif kind == "command":
                ui.show([clean(arg, 4000) for arg in event["argv"]])
            elif kind == "line":
                ui.line(clean(event["text"], 4000), event.get("color", ""), event.get("bold", False))
            elif kind == "yaml":
                for row in yaml_rows(event["data"]):
                    ui.line("".join(ui.styled(clean(text, 4000), color) for text, color in row))
            elif kind == "complete":
                cue(at, max(at, start + end), beat)
                if start + end > at:
                    time.sleep(start + end - at)
                ui.line("\n  Recorded session complete. No live resources were changed.", "amber", True)
        cue(start + end, start + end, "REPLAY COMPLETE")
    except (StopDemo, KeyboardInterrupt):
        cue(current, current, "REPLAY STOPPED")
        raise


def stage(args):
    load_session(args.recording)  # Reject incomplete/mismatched captures before opening tmux.
    if not shutil.which("tmux"):
        raise ValueError("Install tmux, or use ./demo/demo.sh offline --no-tmux for the command transcript")
    size = shutil.get_terminal_size((160, 44))
    if size.columns < 124 or size.lines < 34:
        raise ValueError("Use a terminal at least 124 columns × 34 rows; recommended 160 × 44")
    session = "k8gb-offline-%s" % os.getpid()
    runtime = Path(tempfile.mkdtemp(prefix="k8gb-offline-"))
    control = runtime / "control.json"
    save_json(control, {"offset": 0, "until": 0, "at": time.time(), "beat": "WAITING FOR ENTER"})
    left = [sys.executable, "-u", str(HERE / "offline.py"), str(args.recording), "--no-tmux", "--control-file", str(control)]
    if args.no_clear:
        left.append("--no-clear")
    if args.no_notes:
        left.append("--no-notes")
    right = [sys.executable, "-u", str(HERE / "dashboard.py"), "--replay", str(args.recording), "--compact", "--control-file", str(control)]

    def tmux(*parts):
        return subprocess.check_output(["tmux", *parts], text=True).strip()

    created = False
    try:
        lp = tmux("new-session", "-d", "-P", "-F", "#{pane_id}", "-s", session, "-n", "replay",
                  "-x", str(size.columns), "-y", str(size.lines), "exec " + shlex.join(left))
        created = True
        tmux("set-window-option", "-t", session + ":replay", "remain-on-exit", "on")
        tmux("set-window-option", "-t", session + ":replay", "remain-on-exit-format", "#{?pane_dead_status,Replay exited with status #{pane_dead_status}.,}")
        rp = tmux("split-window", "-h", "-P", "-F", "#{pane_id}", "-p", "44", "-t", lp, "exec " + shlex.join(right))
        tmux("set-window-option", "-t", session + ":replay", "pane-border-status", "top")
        tmux("set-window-option", "-t", session + ":replay", "pane-border-format", " #{pane_title} ")
        tmux("select-pane", "-t", lp, "-T", "RECORDED COMMANDS · Enter to advance")
        tmux("select-pane", "-t", rp, "-T", "RECORDED TRAFFIC · follows Enter")
        tmux("select-pane", "-t", lp)
        if args.detach:
            print("tmux attach-session -t " + session)
        else:
            subprocess.run(["tmux", "switch-client" if os.environ.get("TMUX") else "attach-session", "-t", session], check=True)
    except BaseException:
        if created:
            subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True)
        shutil.rmtree(runtime)
        raise


def main():
    parser = argparse.ArgumentParser(prog="./demo/demo.sh offline", description=__doc__)
    parser.add_argument("recording", nargs="?", type=Path, default=DEFAULT,
                        help="Traffic JSONL with a matching .commands.jsonl; defaults to the bundled fallback")
    parser.add_argument("--no-tmux", action="store_true", help="Play the command transcript in this terminal")
    parser.add_argument("--no-clear", action="store_true", help="Keep earlier beats visible")
    parser.add_argument("--no-notes", action="store_true", help="Hide captured presenter notes")
    parser.add_argument("--detach", action="store_true", help="Create the two panes without attaching")
    parser.add_argument("--control-file", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    args.recording = args.recording.resolve()
    if args.no_tmux and args.detach:
        parser.error("--detach requires tmux")
    if args.no_tmux:
        play(args.recording, args.control_file, args.no_clear, args.no_notes)
    else:
        stage(args)


if __name__ == "__main__":
    try:
        main()
    except (StopDemo, KeyboardInterrupt):
        print("\nRecorded replay stopped. No live resources were changed.")
    except (OSError, ValueError, KeyError, StopIteration, RuntimeError, subprocess.SubprocessError) as exc:
        sys.exit("Offline replay: " + clean(exc, 1200))
