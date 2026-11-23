"""Put the failing part of a test log into a GitHub Actions error annotation.

Annotations can be read through the public API without signing in, unlike the job logs.

    python .github/scripts/log_annotation.py LOGFILE "title"
"""
import sys
from pathlib import Path

path = Path(sys.argv[1])
title = sys.argv[2] if len(sys.argv) > 2 else "test failure"
lines = path.read_text(encoding="utf-8", errors="replace").splitlines() if path.exists() else ["(no log written)"]
marks = ("panicked", "failures:", "FAILED", "error[", "error:")
hits = [i for i, line in enumerate(lines) if any(m in line for m in marks)]
start = max(0, hits[0] - 5) if hits else max(0, len(lines) - 120)
text = "\n".join(lines[start:start + 150])[:60000]
text = text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
print(f"::error title={title}::{text}")
