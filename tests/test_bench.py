"""Smoke test: the benchmark script runs end to end at a small size."""

import subprocess
import sys
from pathlib import Path

BENCH = Path(__file__).parent.parent / "scripts" / "bench.py"


def test_bench_runs_at_small_size(tmp_path):
    completed = subprocess.run(
        [
            sys.executable,
            str(BENCH),
            "--rows",
            "4000",
            "--files",
            "2",
            "--chunk-rows",
            "1000",
            "--workdir",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    assert "rows/s" in completed.stdout
    assert "peak rss" in completed.stdout
    # The pipeline really ran: outputs and a manifest exist.
    assert (tmp_path / "output" / "consolidated.parquet").is_file()
    assert (tmp_path / "runs").is_dir()
