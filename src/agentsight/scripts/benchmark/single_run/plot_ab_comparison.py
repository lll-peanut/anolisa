#!/usr/bin/env python3
"""Render an A/B comparison HTML from two benchmark run directories."""

from __future__ import annotations

import argparse
import html
from pathlib import Path
from typing import Any

from plot_metrics import (
    PlotSeries,
    build_chart,
    load_json,
    read_metrics,
    scan_stability,
    warmup_slope,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-dir", type=Path, required=True)
    parser.add_argument("--optimized-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--title", default="AgentSight A/B 对比")
    parser.add_argument("--rss-target-mb", type=float, default=350.0)
    parser.add_argument("--warmup-seconds", type=float, default=300.0)
    parser.add_argument("--slope-window-seconds", type=float, default=900.0)
    parser.add_argument("--max-points", type=int, default=700)
    return parser.parse_args()


def load_run(directory: Path) -> dict[str, Any]:
    summary = load_json(directory / "run-summary.json")
    report = load_json(directory / "report.json")
    worker = load_json(directory / "worker-result.json")
    metrics_path = directory / "metrics.csv"
    metrics: dict[str, Any] = {}
    if metrics_path.is_file():
        rows, times, columns = read_metrics(metrics_path)
        metrics = {"rows": rows, "times": times, "columns": columns}
    resources = summary.get("resources") if isinstance(summary.get("resources"), dict) else {}
    return {
        "dir": directory,
        "summary": summary,
        "report": report,
        "worker": worker,
        "metrics": metrics,
        "resources": resources,
    }


def number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def run_value(run: dict[str, Any], key: str) -> float | None:
    summary = run["summary"]
    report = run["report"]
    resources = run["resources"]
    latency = summary.get("latency_ms") if isinstance(summary.get("latency_ms"), dict) else {}
    tps = summary.get("tokens_per_second") if isinstance(summary.get("tokens_per_second"), dict) else {}
    if key == "rss_peak":
        return number((resources.get("rss_mb") or {}).get("max"))
    if key == "rss_avg":
        return number((resources.get("rss_mb") or {}).get("avg"))
    if key == "rss_slope":
        metrics = run["metrics"]
        if metrics:
            post, _, _ = warmup_slope(
                metrics["times"],
                metrics["columns"].get("rss_mb", []),
                300.0,
                900.0,
            )
            if post is not None:
                return post
        return number((resources.get("rss_mb") or {}).get("slope"))
    if key == "cpu_avg":
        return number((resources.get("cpu_pct") or {}).get("avg"))
    if key == "cpu_p95":
        return number((resources.get("cpu_pct") or {}).get("p95"))
    if key == "qps":
        return number(summary.get("effective_qps"))
    if key == "tps":
        return number(tps.get("generated"))
    if key == "p99":
        value = number(latency.get("p99"))
        return value / 1000.0 if value is not None else None
    if key == "drop_rate":
        value = number(summary.get("drop_rate"))
        return value * 100.0 if value is not None else None
    if key == "completeness":
        value = report.get("admitted_completeness")
        if not isinstance(value, (int, float)):
            value = report.get("completeness_ratio")
        return number(value)
    if key == "token_accuracy":
        return number(report.get("token_accuracy"))
    if key == "fd_slope":
        return number((resources.get("file_descriptors") or {}).get("slope_per_hour"))
    if key == "thread_slope":
        return number((resources.get("threads") or {}).get("slope_per_hour"))
    return None


def run_rss_slope(run: dict[str, Any], warmup: float, window: float) -> float | None:
    metrics = run["metrics"]
    if metrics:
        post, _, _ = warmup_slope(
            metrics["times"],
            metrics["columns"].get("rss_mb", []),
            warmup,
            window,
        )
        if post is not None:
            return post
    return number((run["resources"].get("rss_mb") or {}).get("slope"))


def stability_counts(run: dict[str, Any]) -> tuple[int, int]:
    info = scan_stability(run["dir"], run["worker"])
    return int(info["panic"]), int(info["oom"])


def format_metric(key: str, value: float | None) -> str:
    if value is None:
        return "—"
    if key in {"rss_peak", "rss_avg"}:
        return f"{value:.1f} MiB"
    if key == "rss_slope":
        return f"{value:+.1f} MiB/h"
    if key in {"cpu_avg", "cpu_p95", "drop_rate"}:
        return f"{value:.1f} %" if key != "drop_rate" else f"{value:.2f}%"
    if key == "qps":
        return f"{value:.1f}"
    if key == "tps":
        return f"{value:,.0f} tok/s"
    if key == "p99":
        return f"{value:.2f} s"
    if key in {"completeness", "token_accuracy"}:
        return f"{value * 100:.1f}%"
    if key in {"fd_slope", "thread_slope"}:
        return f"{value:+.1f}/h"
    return f"{value:g}"


def delta_and_verdict(key: str, base: float | None, opt: float | None) -> tuple[str, str]:
    if base is None or opt is None:
        return "—", "N/A"
    if base == 0:
        delta = "0.0%" if opt == 0 else "—"
    else:
        delta = f"{(opt - base) / abs(base) * 100:+.1f}%"
    lower_is_better = key in {"rss_peak", "rss_avg", "rss_slope", "cpu_avg", "cpu_p95", "p99", "drop_rate", "fd_slope", "thread_slope"}
    if base == 0:
        return delta, "持平"
    change = (opt - base) / abs(base)
    if abs(change) <= 0.02:
        return delta, "持平"
    if lower_is_better:
        return delta, "改善" if change < 0 else "轻微退化"
    return delta, "改善" if change > 0 else "轻微退化"


def verdict_class(verdict: str) -> str:
    if verdict == "改善" or verdict == "通过":
        return "delta-good"
    if verdict in {"轻微退化", "需关注"}:
        return "delta-warn"
    return "delta-neutral"


def kpi(label: str, value: str, sub: str) -> str:
    return (
        f'<div class="kpi"><div class="label">{html.escape(label)}</div>'
        f'<div class="value">{html.escape(value)}</div>'
        f'<div class="sub">{sub}</div></div>'
    )


def make_chart(
    chart_id: str,
    title: str,
    baseline: dict[str, Any],
    optimized: dict[str, Any],
    key: str,
    unit: str,
    target: float | None,
    warmup: float,
    max_points: int,
) -> str:
    baseline_metrics = baseline["metrics"]
    optimized_metrics = optimized["metrics"]
    if not baseline_metrics or not optimized_metrics:
        return '<div class="empty">缺少 metrics.csv，无法绘图</div>'
    base_values = baseline_metrics["columns"].get(key, [])
    opt_values = optimized_metrics["columns"].get(key, [])
    times = baseline_metrics["times"]
    reference = (target, f"目标 {target:g} {unit}") if target is not None else None
    svg, _ = build_chart(
        chart_id,
        title,
        times,
        [
            PlotSeries("baseline", unit, "var(--series-1)", "--series-1", base_values, 1),
            PlotSeries("optimized", unit, "var(--series-2)", "--series-2", opt_values, 1),
        ],
        reference,
        False,
        max_points,
        warmup=warmup,
    )
    return svg


def render(args: argparse.Namespace) -> Path:
    baseline = load_run(args.baseline_dir)
    optimized = load_run(args.optimized_dir)
    baseline_panic, baseline_oom = stability_counts(baseline)
    optimized_panic, optimized_oom = stability_counts(optimized)

    metric_specs = [
        ("rss_peak", "RSS 峰值"),
        ("rss_avg", "RSS 平均"),
        ("rss_slope", "RSS 斜率（warmup 后）"),
        ("cpu_avg", "CPU 平均"),
        ("cpu_p95", "CPU P95"),
        ("qps", "有效 QPS"),
        ("tps", "Token 吞吐"),
        ("p99", "Latency P99"),
        ("drop_rate", "事件丢弃率"),
        ("completeness", "有效完整率"),
        ("token_accuracy", "Token 准确率"),
        ("fd_slope", "FD 斜率"),
        ("thread_slope", "线程斜率"),
    ]
    rows: list[str] = []
    for key, label in metric_specs:
        if key == "rss_slope":
            base = run_rss_slope(baseline, args.warmup_seconds, args.slope_window_seconds)
            opt = run_rss_slope(optimized, args.warmup_seconds, args.slope_window_seconds)
        else:
            base = run_value(baseline, key)
            opt = run_value(optimized, key)
        delta, verdict = delta_and_verdict(key, base, opt)
        rows.append(
            f"<tr><td>{html.escape(label)}</td>"
            f"<td>{html.escape(format_metric(key, base))}</td>"
            f"<td>{html.escape(format_metric(key, opt))}</td>"
            f'<td class="{verdict_class(verdict)}">{html.escape(delta)}</td>'
            f"<td>{html.escape(verdict)}</td></tr>"
        )
    panic_ok = baseline_panic == 0 and baseline_oom == 0 and optimized_panic == 0 and optimized_oom == 0
    panic_verdict = "通过" if panic_ok else "失败"
    panic_status = f'<span class="{"delta-good" if panic_ok else "delta-warn"}">{panic_verdict}</span>'
    rows.append(
        f"<tr><td>Panic / OOM</td><td>{baseline_panic} / {baseline_oom}</td>"
        f"<td>{optimized_panic} / {optimized_oom}</td><td>—</td>"
        f'<td class="{"delta-good" if panic_ok else "delta-warn"}">{panic_verdict}</td></tr>'
    )

    rss_peak_base = run_value(baseline, "rss_peak")
    rss_peak_opt = run_value(optimized, "rss_peak")
    rss_avg_base = run_value(baseline, "rss_avg")
    rss_avg_opt = run_value(optimized, "rss_avg")
    cpu_base = run_value(baseline, "cpu_avg")
    cpu_opt = run_value(optimized, "cpu_avg")
    p99_base = run_value(baseline, "p99")
    p99_opt = run_value(optimized, "p99")
    qps_base = run_value(baseline, "qps")
    qps_opt = run_value(optimized, "qps")
    completeness_base = run_value(baseline, "completeness")
    completeness_opt = run_value(optimized, "completeness")
    drop_base = run_value(baseline, "drop_rate")
    drop_opt = run_value(optimized, "drop_rate")
    rss_delta = f"{(rss_peak_opt - rss_peak_base) / abs(rss_peak_base) * 100:+.1f}%" if rss_peak_base and rss_peak_opt is not None else "—"
    avg_delta = f"{(rss_avg_opt - rss_avg_base) / abs(rss_avg_base) * 100:+.1f}%" if rss_avg_base and rss_avg_opt is not None else "—"
    cpu_delta = f"{(cpu_opt - cpu_base) / abs(cpu_base) * 100:+.1f}%" if cpu_base and cpu_opt is not None else "—"
    p99_delta = f"{p99_opt - p99_base:+.2f} s" if p99_base is not None and p99_opt is not None else "—"
    rss_class = verdict_class(delta_and_verdict("rss_peak", rss_peak_base, rss_peak_opt)[1])
    avg_class = verdict_class(delta_and_verdict("rss_avg", rss_avg_base, rss_avg_opt)[1])
    cpu_class = verdict_class(delta_and_verdict("cpu_avg", cpu_base, cpu_opt)[1])
    p99_class = verdict_class(delta_and_verdict("p99", p99_base, p99_opt)[1])

    rss_svg = make_chart(
        "chart-ab-rss", "常驻内存", baseline, optimized, "rss_mb", "MiB",
        args.rss_target_mb, args.warmup_seconds, args.max_points,
    )
    cpu_svg = make_chart(
        "chart-ab-cpu", "CPU", baseline, optimized, "cpu_pct", "%",
        None, args.warmup_seconds, args.max_points,
    )

    document = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(args.title)}</title>
<style>
:root {{ color-scheme:light; --page:#f9f9f7; --surface-1:#fcfcfb; --text-primary:#0b0b0b;
  --text-secondary:#52514e; --text-muted:#898781; --grid:#e1e0d9; --axis:#c3c2b7;
  --border:rgba(11,11,11,.10); --series-1:#2a78d6; --series-2:#eb6834;
  --status-good:#0ca30c; --status-warning:#c98a00; }}
@media (prefers-color-scheme:dark) {{ :root:where(:not([data-theme="light"])) {{
  color-scheme:dark; --page:#0d0d0d; --surface-1:#1a1a19; --text-primary:#f0efec;
  --text-secondary:#c3c2b7; --text-muted:#898781; --grid:#2c2c2a; --axis:#383835;
  --border:rgba(255,255,255,.10); --series-1:#3987e5; --series-2:#d95926; }} }}
:root[data-theme="dark"] {{ color-scheme:dark; --page:#0d0d0d; --surface-1:#1a1a19;
  --text-primary:#f0efec; --text-secondary:#c3c2b7; --text-muted:#898781;
  --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,.10);
  --series-1:#3987e5; --series-2:#d95926; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--page); color:var(--text-primary);
  font-family:system-ui,-apple-system,"Segoe UI",sans-serif; }}
.viz-root {{ max-width:1480px; margin:0 auto; padding:32px 24px 64px; }}
header {{ display:flex; align-items:flex-start; justify-content:space-between; gap:24px; }}
h1 {{ font-size:22px; margin:0 0 6px; letter-spacing:-.01em; }}
.sub {{ margin:0; color:var(--text-secondary); font-size:13px; }}
button {{ border:1px solid var(--border); background:var(--surface-1);
  color:var(--text-primary); border-radius:8px; padding:7px 12px; font:inherit;
  font-size:12px; cursor:pointer; }}
.kpis {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr));
  gap:12px; margin-top:24px; }}
.kpi {{ background:var(--surface-1); border:1px solid var(--border);
  border-radius:12px; padding:13px 15px; min-height:92px; }}
.kpi .label {{ font-size:11px; color:var(--text-muted); text-transform:uppercase;
  letter-spacing:.05em; }}
.kpi .value {{ font-size:23px; font-weight:600; margin-top:5px;
  font-variant-numeric:tabular-nums; }}
.kpi .sub {{ font-size:11px; color:var(--text-secondary); margin-top:5px;
  overflow-wrap:anywhere; }}
h2 {{ font-size:15px; margin:30px 0 12px; }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(430px,1fr)); gap:16px; }}
.card {{ background:var(--surface-1); border:1px solid var(--border); border-radius:12px;
  padding:14px 16px 8px; min-width:0; }}
.card h3 {{ font-size:14px; margin:0 0 2px; }}
.card .meta {{ font-size:11px; color:var(--text-muted); margin-bottom:4px; }}
.legend {{ display:flex; flex-wrap:wrap; gap:14px; font-size:11px;
  color:var(--text-secondary); margin:4px 0 2px; }}
.legend > span {{ display:inline-flex; align-items:center; gap:6px; }}
.swatch {{ width:10px; height:10px; border-radius:3px; display:inline-block; }}
svg.chart {{ width:100%; height:auto; display:block; overflow:visible; }}
.axis {{ fill:var(--text-muted); font-size:11px; font-variant-numeric:tabular-nums; }}
.gridline {{ stroke:var(--grid); stroke-width:1; }}
.baseline {{ stroke:var(--axis); stroke-width:1; }}
.series-line {{ fill:none; stroke-width:2; stroke-linejoin:round; stroke-linecap:round; }}
.reference {{ stroke:var(--text-muted); stroke-width:1.5; stroke-dasharray:5 4; }}
.hit {{ fill:transparent; }}
.last-label {{ fill:var(--text-secondary); font-size:11px; font-weight:500;
  font-variant-numeric:tabular-nums; paint-order:stroke; stroke:var(--surface-1);
  stroke-width:3px; stroke-linejoin:round; }}
.empty {{ padding:24px 4px; color:var(--text-secondary); font-size:13px; }}
table {{ width:100%; border-collapse:collapse; margin-top:12px; font-size:12px;
  font-variant-numeric:tabular-nums; }}
th, td {{ padding:7px 8px; text-align:right; border-bottom:1px solid var(--grid); }}
th:first-child, td:first-child, th:last-child, td:last-child {{ text-align:left; }}
th {{ color:var(--text-muted); font-weight:500; font-size:11px; }}
.delta-good {{ color:var(--status-good); font-weight:600; }}
.delta-warn {{ color:var(--status-warning); font-weight:600; }}
.delta-neutral {{ color:var(--text-secondary); }}
ul {{ margin:8px 0 0 18px; padding:0; color:var(--text-secondary);
  font-size:13px; line-height:1.75; }}
footer {{ margin-top:24px; color:var(--text-muted); font-size:11px; }}
@media (max-width:640px) {{ .viz-root {{ padding:20px 14px 48px; }}
  .grid {{ grid-template-columns:1fr; }} }}
@media print {{ :root {{ color-scheme:light; --page:#fff; --surface-1:#fff; }}
  button {{ display:none; }} }}
</style>
</head>
<body>
<div class="viz-root">
<header>
  <div>
    <h1>{html.escape(args.title)}</h1>
    <p class="sub">baseline <code>{html.escape(args.baseline_dir.name)}</code> · optimized <code>{html.escape(args.optimized_dir.name)}</code> · 同一环境/配置/负载</p>
  </div>
  <button id="theme-toggle" type="button" aria-label="切换主题">主题</button>
</header>

<section class="kpis" aria-label="A/B 摘要">
  {kpi("RSS 峰值", format_metric("rss_peak", rss_peak_opt), f"baseline {format_metric('rss_peak', rss_peak_base)} · <span class=\"{rss_class}\">{html.escape(rss_delta)}</span>")}
  {kpi("RSS 平均", format_metric("rss_avg", rss_avg_opt), f"baseline {format_metric('rss_avg', rss_avg_base)} · <span class=\"{avg_class}\">{html.escape(avg_delta)}</span>")}
  {kpi("CPU 平均", format_metric("cpu_avg", cpu_opt), f"baseline {format_metric('cpu_avg', cpu_base)} · <span class=\"{cpu_class}\">{html.escape(cpu_delta)}</span>")}
  {kpi("有效 QPS", format_metric("qps", qps_opt), f"baseline {format_metric('qps', qps_base)}")}
  {kpi("Token 吞吐", format_metric("tps", run_value(optimized, "tps")), f"baseline {format_metric('tps', run_value(baseline, 'tps'))}")}
  {kpi("Latency P99", format_metric("p99", p99_opt), f"baseline {format_metric('p99', p99_base)} · <span class=\"{p99_class}\">{html.escape(p99_delta)}</span>")}
  {kpi("事件丢弃率", format_metric("drop_rate", drop_opt), f"baseline {format_metric('drop_rate', drop_base)}")}
  {kpi("有效完整率", format_metric("completeness", completeness_opt), f"baseline {format_metric('completeness', completeness_base)}")}
  {kpi("Panic / OOM", f"{optimized_panic} / {optimized_oom}", f"baseline {baseline_panic} / {baseline_oom} · {panic_status}")}
</section>

<section><h2>RSS 对比</h2>
<div class="grid">
<article class="card"><h3>常驻内存</h3><div class="meta">目标 {args.rss_target_mb:g} MiB · warmup {args.warmup_seconds:g}s</div>
<div class="legend"><span><i class="swatch" style="background:var(--series-1)"></i>baseline</span><span><i class="swatch" style="background:var(--series-2)"></i>optimized</span></div>{rss_svg}</article>
<article class="card"><h3>CPU</h3><div class="meta">warmup {args.warmup_seconds:g}s</div>
<div class="legend"><span><i class="swatch" style="background:var(--series-1)"></i>baseline</span><span><i class="swatch" style="background:var(--series-2)"></i>optimized</span></div>{cpu_svg}</article>
</div>
</section>

<section><h2>关键指标对比</h2>
<table><thead><tr><th>指标</th><th>baseline</th><th>optimized</th><th>Δ</th><th>判定</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
<p class="sub">Δ 为 optimized 相对 baseline；RSS/延迟/丢弃越低越好，吞吐/完整率越高越好。判定只做展示，最终 PASS/FAIL 由冻结门槛决定。</p>
</section>

<section><h2>结论</h2>
<ul>
<li>RSS 峰值 {html.escape(rss_delta)}，平均 {html.escape(avg_delta)}；以 real artifact 为准。</li>
<li>有效 QPS 与 Token 吞吐需保持不低于 baseline；完整率/丢弃率不得回退。</li>
<li>CPU {html.escape(cpu_delta)}、P99 {html.escape(p99_delta)}，是否通过取决于冻结门槛。</li>
<li>Panic/OOM：baseline {baseline_panic}/{baseline_oom}，optimized {optimized_panic}/{optimized_oom}。</li>
</ul>
</section>

<footer>只读 A/B 对比报告 · 结论必须结合冻结门槛和真实 artifact</footer>
</div>
<script>
(function () {{
  const toggle = document.getElementById('theme-toggle');
  if (!toggle) return;
  toggle.addEventListener('click', function () {{
    const root = document.documentElement;
    const dark = root.dataset.theme
      ? root.dataset.theme === 'dark'
      : window.matchMedia('(prefers-color-scheme: dark)').matches;
    root.dataset.theme = dark ? 'light' : 'dark';
  }});
}})();
</script>
</body>
</html>
"""
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(document, encoding="utf-8")
    print(args.output)
    return args.output


def main() -> int:
    args = parse_args()
    render(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
