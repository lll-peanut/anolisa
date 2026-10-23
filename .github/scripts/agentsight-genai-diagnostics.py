#!/usr/bin/env python3
"""Collect external evidence for the fork-only AgentSight GenAI experiment."""

from __future__ import annotations

import argparse
import json
import os
import re
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
CGROUP = Path("/sys/fs/cgroup")


def read(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError as error:
        return f"UNAVAILABLE: {error}"


def write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def cgroups(pid: int) -> dict[str, dict[str, str]]:
    paths = {CGROUP}
    for line in read(Path(f"/proc/{pid}/cgroup")).splitlines():
        if line.startswith("0::"):
            candidate = CGROUP / line[3:].lstrip("/")
            if candidate.is_relative_to(CGROUP) and ".." not in candidate.parts:
                paths.add(candidate)
                paths.update(
                    parent for parent in candidate.parents if parent.is_relative_to(CGROUP)
                )
    return {
        str(path): {
            name: read(path / name)
            for name in (
                "cpu.max",
                "cpu.stat",
                "cpu.pressure",
                "memory.events",
                "cpuset.cpus.effective",
            )
        }
        for path in sorted(paths)
    }


def environment(output: Path) -> None:
    commands = (
        ["uname", "-a"],
        ["lscpu"],
        ["free", "-m"],
        ["df", "-h"],
        ["rustc", "--version"],
        [sys.executable, "-c", "import sys, ssl; print(sys.version); print(ssl.OPENSSL_VERSION)"],
        ["git", "rev-parse", "HEAD"],
    )
    data = {"commands": {}, "cgroups": cgroups(os.getpid())}
    for command in commands:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        data["commands"][" ".join(command)] = {
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    for name in ("/proc/cpuinfo", "/proc/self/status", "/proc/self/cgroup", "/proc/meminfo"):
        data[name] = read(Path(name))
    write(output, data)
    print(json.dumps(data, indent=2), flush=True)


def sha(output: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="agentsight-sha-probe-") as temporary:
        project = Path(temporary)
        (project / "src").mkdir()
        (project / "Cargo.toml").write_text(
            '[package]\nname = "agentsight-sha-probe"\nversion = "0.0.0"\nedition = "2021"\n'
            '[dependencies]\nsha2 = "=0.10.9"\n'
            '[features]\nforce-soft = ["sha2/force-soft"]\n[workspace]\n'
        )
        shutil.copyfile(
            Path(__file__).with_name("agentsight-sha-probe.rs"), project / "src/main.rs"
        )
        env = dict(os.environ, CARGO_TARGET_DIR=str(project / "target"))
        results = {}
        for name, features in (("native", []), ("soft", ["--features", "force-soft"])):
            subprocess.run(
                ["cargo", "build", "--release", *features], cwd=project, env=env, check=True
            )
            trials = []
            for _ in range(3):
                before = resource.getrusage(resource.RUSAGE_CHILDREN)
                started = time.monotonic()
                result = subprocess.run(
                    [str(project / "target/release/agentsight-sha-probe")],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                after = resource.getrusage(resource.RUSAGE_CHILDREN)
                trial = json.loads(result.stdout)
                trial["wall_seconds"] = time.monotonic() - started
                trial["cpu_seconds"] = (
                    after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime
                )
                trials.append(trial)
            results[name] = trials
        write(output, results)
        print(json.dumps(results, indent=2), flush=True)


def process(pid: int, threads: bool = False) -> dict[str, Any]:
    path = Path(f"/proc/{pid}")
    text = read(path / "stat")
    fields = text[text.rfind(")") + 2 :].split()
    if len(fields) < 22 or text.startswith("UNAVAILABLE:"):
        return {}
    result = {
        "pid": pid,
        "utime": int(fields[11]),
        "stime": int(fields[12]),
        "minflt": int(fields[7]),
        "majflt": int(fields[9]),
        "starttime": fields[19],
        "cmdline": read(path / "cmdline").replace("\x00", " ")[:700],
    }
    if threads:
        result["threads"] = {}
        try:
            tasks = list((path / "task").iterdir())
        except OSError:
            tasks = []
        for task in tasks:
            schedstat = read(task / "schedstat").split()
            if len(schedstat) == 3 and all(value.isdigit() for value in schedstat):
                result["threads"][task.name] = {
                    "name": read(task / "comm").strip(),
                    "runtime_ns": int(schedstat[0]),
                    "runqueue_ns": int(schedstat[1]),
                    "timeslices": int(schedstat[2]),
                }
    return result


def watch(args: argparse.Namespace) -> None:
    profiler = None
    profile_log = None
    perf = None
    last_pid = None
    if args.profile:
        candidates = sorted(Path("/usr/lib/linux-tools").glob("**/perf"))
        perf = str(candidates[-1]) if candidates else shutil.which("perf")
    with args.output.open("w") as handle:
        while not args.stop.exists():
            state_path = args.run_dir / "run/worker-pids.json"
            try:
                state = json.loads(state_path.read_text())
            except (OSError, ValueError):
                state = {}
            pid = state.get("trace")
            sample: dict[str, Any] = {
                "monotonic": time.monotonic(),
                "wall_time": time.time(),
                "system_cpu": read(Path("/proc/stat")).splitlines()[0],
                "cpu_pressure": read(Path("/proc/pressure/cpu")),
                "memory_pressure": read(Path("/proc/pressure/memory")),
                "loadavg": read(Path("/proc/loadavg")),
                "cgroups": cgroups(pid or os.getpid()),
            }
            if pid:
                sample["tracer"] = process(pid, threads=True)
                if args.profile and perf and last_pid != pid:
                    last_pid = pid
                    profile_log = args.output.with_suffix(".perf.log").open("w")
                    profiler = subprocess.Popen(
                        [
                            perf,
                            "record",
                            "-e",
                            "cpu-clock",
                            "-F",
                            "99",
                            "--call-graph",
                            "dwarf,8192",
                            "-p",
                            str(pid),
                            "-o",
                            str(args.output.with_suffix(".perf.data")),
                        ],
                        stdout=profile_log,
                        stderr=subprocess.STDOUT,
                    )
            sample["load_processes"] = []
            for candidate in Path("/proc").iterdir():
                if not candidate.name.isdigit():
                    continue
                cmdline = read(candidate / "cmdline")
                if any(
                    name in cmdline
                    for name in ("/load/agents.py", "/mock_llm_server.py", "/validate_results.py")
                ):
                    value = process(int(candidate.name))
                    if value:
                        sample["load_processes"].append(value)
            handle.write(json.dumps(sample) + "\n")
            handle.flush()
            time.sleep(1)
    if profiler is not None:
        if profiler.poll() is None:
            profiler.send_signal(signal.SIGINT)
        profiler.wait(timeout=30)
        profile_log.close()
        with args.output.with_suffix(".perf.txt").open("w") as report:
            subprocess.run(
                [
                    perf,
                    "report",
                    "--stdio",
                    "--no-children",
                    "--percent-limit",
                    "0.5",
                    "-i",
                    str(args.output.with_suffix(".perf.data")),
                ],
                stdout=report,
                stderr=subprocess.STDOUT,
                check=False,
            )


def measure(args: argparse.Namespace) -> None:
    output = args.output.resolve()
    monitor = output.with_suffix(".monitor.jsonl")
    stop = output.with_suffix(".stop")
    watcher = subprocess.Popen(
        [
            "sudo",
            "-n",
            "--",
            sys.executable,
            str(Path(__file__).resolve()),
            "watch",
            "--output",
            str(monitor),
            "--run-dir",
            str(output),
            "--stop",
            str(stop),
            *(["--profile"] if args.profile else []),
        ]
    )
    command = [
        sys.executable,
        "-B",
        str(ROOT / "src/agentsight/scripts/benchmark/single_run/resource_run.py"),
        "--candidate-bin",
        str(args.binary.resolve()),
        "--agents",
        "32",
        "--connection-capacity",
        "128",
        "--payload-kb",
        "1024",
        "--tokens-per-second",
        "100",
        "--tokens-per-response",
        "1000",
        "--duration",
        str(args.duration),
        "--ring-buffer-mb",
        "64",
        "--cpu-budget-percent",
        str(args.cpu_budget),
        "--output-dir",
        str(output),
        *(["--config", str(args.config.resolve())] if args.config else []),
    ]
    try:
        result = subprocess.run(command, cwd=ROOT / "src/agentsight", check=False)
        write(
            output.with_suffix(".exit.json"), {"returncode": result.returncode, "command": command}
        )
    finally:
        stop.touch()
        try:
            watcher.wait(timeout=45)
        except subprocess.TimeoutExpired:
            subprocess.run(["sudo", "-n", "kill", "-INT", str(watcher.pid)], check=False)
    # Capture failures are experiment results, so subsequent comparisons must still run.
    if not (output / "resource-summary.json").exists():
        raise RuntimeError(f"No resource summary produced: {output}")


def summarize(output: Path) -> None:
    print("AgentSight fork experiment: diagnostics, not the upstream Linux 6.6 resource gate.\n")
    for directory in sorted(output.iterdir()):
        if not directory.is_dir() or not (directory / "resource-summary.json").exists():
            continue
        summary = json.loads((directory / "resource-summary.json").read_text())
        stages = {}
        metrics = read(directory / "run/internal-metrics.prom")
        for stage in ("genai", "genai_capture_complete"):
            values = {}
            for kind in ("sum", "count"):
                pattern = rf'^agentsight_stage_duration_seconds_{kind}\{{[^\n]*stage="{stage}"[^\n]*\}}\s+(\S+)'
                match = re.search(pattern, metrics, re.MULTILINE)
                if match:
                    values[kind] = float(match[1])
            if values.get("count"):
                stages[stage] = values["sum"] / values["count"] * 1000
        print(f"{directory.name}: stage averages in ms = {json.dumps(stages)}")
        print(json.dumps(summary, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("environment", "sha", "watch", "measure", "summarize"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--duration", type=int, default=300)
    parser.add_argument("--cpu-budget", type=int, default=25)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--stop", type=Path)
    parser.add_argument("--profile", action="store_true")
    args = parser.parse_args()
    if args.mode == "environment":
        environment(args.output)
    elif args.mode == "sha":
        sha(args.output)
    elif args.mode == "watch":
        watch(args)
    elif args.mode == "measure":
        measure(args)
    else:
        summarize(args.output)


if __name__ == "__main__":
    main()
