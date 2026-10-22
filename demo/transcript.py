"""Structured capture of the presenter's displayed output, not executable commands."""
import json
from pathlib import Path
import time


def transcript_path(recording):
    return Path(recording).with_suffix(".commands.jsonl")


class Transcript:
    def __init__(self, path, start):
        self.file = open(path, "x", encoding="utf-8")
        self.file.write(json.dumps({"schema": "k8gb-commands-v1", "origin": "live", "start": start}) + "\n")
        self.file.flush()

    def append(self, kind, **fields):
        self.file.write(json.dumps({"kind": kind, "at": time.time(), **fields}) + "\n")
        self.file.flush()

    def close(self):
        self.file.close()
