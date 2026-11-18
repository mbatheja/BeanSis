"""Atomically record the latest scheduled reference-data refresh state."""
import json
import os
import sys
import tempfile

path, operation, state, started, finished = sys.argv[1:]
try:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
except (OSError, ValueError):
    data = {}

previous = data.get(operation, {})
record = {
    "last_run_epoch": int(finished),
    "last_started_epoch": int(started),
    "last_status": state,
}
if state == "success":
    record["last_success_epoch"] = int(finished)
elif previous.get("last_success_epoch"):
    record["last_success_epoch"] = previous["last_success_epoch"]
data[operation] = record

fd, temporary = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".ingestion-status-")
with os.fdopen(fd, "w", encoding="utf-8") as f:
    json.dump(data, f)
    f.write("\n")
os.replace(temporary, path)
