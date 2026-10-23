#!/usr/bin/env python3
"""Locate main-thread stalls externally without modifying the measured binary."""

from __future__ import annotations

import argparse
import gzip
import json
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError as error:
        return f"UNAVAILABLE: {error}"


def write(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")


def classify(stack: str, state: str) -> str:
    if state.startswith("R"):
        return "runnable/preempted"
    if "sqlite" in stack.lower():
        return "SQLite wait"
    if ("drain_one_completion" in stack or "cpu_budget::CpuBudget" in stack) and (
        "nanosleep" in stack or "thread::sleep" in stack
    ):
        return "CPU admission wait"
    if "recv" in stack or "crossbeam" in stack:
        return "event receive wait"
    return "other/unknown"


def analyze(path: Path, pid: int, output: Path) -> None:
    pattern = re.compile(
        r"(\d+\.\d+):\s+sched:sched_switch:\s+.*prev_pid=(\d+).*"
        r"prev_state=(\S+).*next_pid=(\d+)"
    )
    categories: dict[str, dict[str, float]] = {}
    waits = []
    pending = None
    events = 0
    with gzip.open(path, "rt") as handle:
        block: list[str] = []
        for line in handle:
            if pattern.search(line):
                if block:
                    consume = "".join(block)
                    match = pattern.search(consume)
                    if match:
                        events += 1
                        timestamp, previous, state, following = match.groups()
                        now = float(timestamp)
                        if int(following) == pid and pending is not None:
                            start, previous_state, stack = pending
                            category = classify(stack, previous_state)
                            duration = now - start
                            totals = categories.setdefault(
                                category, {"count": 0, "seconds": 0, "max_seconds": 0}
                            )
                            totals["count"] += 1
                            totals["seconds"] += duration
                            totals["max_seconds"] = max(totals["max_seconds"], duration)
                            if duration >= 0.003:
                                waits.append(
                                    {
                                        "start_monotonic": start,
                                        "end_monotonic": now,
                                        "seconds": duration,
                                        "category": category,
                                        "state": previous_state,
                                        "stack": stack,
                                    }
                                )
                            pending = None
                        if int(previous) == pid:
                            pending = (now, state, consume)
                block = [line]
            else:
                block.append(line)
    # The final unmatched switch is excluded because the recording boundary can truncate it.
    write(
        output,
        {
            "main_pid": pid,
            "parsed_switches": events,
            "categories": categories,
            "longest_waits": sorted(waits, key=lambda value: -value["seconds"])[:80],
            "waits_over_3ms": [
                {key: value for key, value in wait.items() if key != "stack"} for wait in waits
            ],
        },
    )


def collect(args: argparse.Namespace) -> None:
    candidates = sorted(Path("/usr/lib/linux-tools").glob("**/perf"))
    perf = str(candidates[-1]) if candidates else shutil.which("perf")
    if not perf:
        raise RuntimeError("perf is required for blocking diagnostics")
    recordings = []
    pid = None
    prefix = str(args.output)
    with Path(prefix + ".samples.jsonl").open("w") as samples:
        while not args.stop.exists():
            if pid is None:
                try:
                    pid = json.loads((args.run_dir / "run/worker-pids.json").read_text()).get(
                        "trace"
                    )
                except (OSError, ValueError):
                    pass
            if pid:
                tasks = {}
                try:
                    entries = list(Path(f"/proc/{pid}/task").iterdir())
                except OSError:
                    entries = []
                for entry in entries:
                    name = read(entry / "comm")
                    if entry.name == str(pid) or name in ("sqlite-maintena", "probes-poll"):
                        tasks[entry.name] = {
                            "name": name,
                            "wchan": read(entry / "wchan"),
                            "syscall": read(entry / "syscall"),
                            "schedstat": read(entry / "schedstat"),
                        }
                samples.write(
                    json.dumps({"monotonic": time.monotonic(), "pid": pid, "tasks": tasks}) + "\n"
                )
                samples.flush()
                if not recordings and len(tasks) >= 3:
                    commands = {
                        "scheduler": [
                            perf,
                            "record",
                            "-a",
                            "-e",
                            "sched:sched_switch",
                            "--filter",
                            f"prev_pid == {pid} || next_pid == {pid}",
                            "--call-graph",
                            "dwarf,8192",
                        ],
                        "cpu": [
                            perf,
                            "record",
                            "-e",
                            "cpu-clock",
                            "-F",
                            "99",
                            "-t",
                            ",".join(tasks),
                            "--call-graph",
                            "dwarf,8192",
                        ],
                    }
                    write(Path(prefix + ".commands.json"), commands)
                    for name, command in commands.items():
                        path = Path(prefix + f".{name}.data")
                        log = Path(prefix + f".{name}.log").open("w")
                        process = subprocess.Popen(
                            [*command, "-o", str(path)], stdout=log, stderr=subprocess.STDOUT
                        )
                        recordings.append((name, path, log, process))
            time.sleep(0.2)
    for name, path, log, process in recordings:
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
        process.wait(timeout=30)
        log.close()
        if name == "scheduler":
            script = Path(prefix + ".scheduler.txt")
            with script.open("wb") as handle:
                subprocess.run([perf, "script", "--ns", "-i", str(path)], stdout=handle, check=True)
            compressed = Path(str(script) + ".gz")
            with script.open("rb") as source, gzip.open(compressed, "wb") as destination:
                shutil.copyfileobj(source, destination)
            script.unlink()
            analyze(compressed, pid, Path(prefix + ".waits.json"))
        else:
            with Path(prefix + ".cpu.txt").open("w") as handle:
                subprocess.run(
                    [
                        perf,
                        "report",
                        "--stdio",
                        "--no-children",
                        "--sort",
                        "comm,symbol",
                        "--percent-limit",
                        "0.5",
                        "-i",
                        str(path),
                    ],
                    stdout=handle,
                    check=True,
                )
    if not recordings:
        raise RuntimeError("Tracer threads were not found; no blocking profile recorded")


def run(args: argparse.Namespace) -> None:
    stop = args.output.with_suffix(".blocking.stop")
    collector = subprocess.Popen(
        [
            "sudo",
            "-n",
            "--",
            sys.executable,
            str(Path(__file__).resolve()),
            "collect",
            "--output",
            str(args.output.with_suffix(".blocking")),
            "--run-dir",
            str(args.output),
            "--stop",
            str(stop),
        ]
    )
    try:
        subprocess.run(
            [
                sys.executable,
                str(Path(__file__).with_name("agentsight-genai-diagnostics.py")),
                "measure",
                "--output",
                str(args.output),
                "--binary",
                str(args.binary),
            ],
            check=True,
        )
    finally:
        stop.touch()
        collector.wait(timeout=60)
    if collector.returncode:
        raise RuntimeError(f"Blocking diagnostics failed: exit {collector.returncode}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("run", "collect"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--stop", type=Path)
    args = parser.parse_args()
    if args.mode == "run":
        run(args)
    else:
        collect(args)


if __name__ == "__main__":
    main()
