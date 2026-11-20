"""Render a chart spec + query result to a PNG with matplotlib.

Styling follows a small, consistent system: one categorical palette assigned in
fixed order, thin marks, recessive grid/axes, a single y-axis, text in neutral
ink (never in series colour), a legend only when there are 2+ series.
"""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
from typing import Any, Sequence

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

SURFACE = "#fcfcfb"
TEXT_PRIMARY = "#0b0b0b"
TEXT_SECONDARY = "#52514e"
GRID = "#e4e3df"
# Categorical slots, fixed order (validated palette). Max 8 series; more fold into "Other".
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
MAX_SERIES = len(SERIES)

CHART_TYPES = ("bar", "hbar", "line", "none")


def _num(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _fmt(v: float, _pos: Any = None) -> str:
    a = abs(v)
    if a >= 1e9:
        return f"{v / 1e9:.1f}B"
    if a >= 1e6:
        return f"{v / 1e6:.1f}M"
    if a >= 1e3:
        return f"{v / 1e3:.0f}K" if a >= 1e4 else f"{v / 1e3:.1f}K"
    return f"{v:.0f}" if float(v).is_integer() else f"{v:.2f}"


def _style(ax: plt.Axes, value_axis: str) -> None:
    ax.set_facecolor(SURFACE)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(colors=TEXT_SECONDARY, labelsize=9, length=0)
    ax.grid(axis=value_axis, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    fmt = FuncFormatter(_fmt)
    (ax.yaxis if value_axis == "y" else ax.xaxis).set_major_formatter(fmt)


def render_chart(
    spec: dict[str, Any],
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    out_path: str | Path,
) -> str | None:
    """Draw the chart described by ``spec`` and return the PNG path (or None).

    spec keys: chart_type (bar|hbar|line|none), x, y, series (optional), title.
    """
    chart_type = spec.get("chart_type", "none")
    if chart_type == "none" or not rows:
        return None
    cols = list(columns)
    x, y, series = spec.get("x"), spec.get("y"), spec.get("series")
    for name in (x, y) + ((series,) if series else ()):
        if name not in cols:
            raise ValueError(f"Chart column '{name}' not in result columns {cols}")
    xi, yi = cols.index(x), cols.index(y)
    si = cols.index(series) if series else None

    # Group into series (preserving first-seen order so colour follows the entity).
    groups: "OrderedDict[str, list[tuple[Any, float]]]" = OrderedDict()
    for r in rows:
        key = str(r[si]) if si is not None else y
        groups.setdefault(key, []).append((r[xi], _num(r[yi])))
    if len(groups) > MAX_SERIES:
        keep = list(groups)[: MAX_SERIES - 1]
        other: "OrderedDict[Any, float]" = OrderedDict()
        for k in list(groups)[MAX_SERIES - 1:]:
            for xv, yv in groups.pop(k):
                other[xv] = other.get(xv, 0.0) + yv
        groups = OrderedDict((k, groups[k]) for k in keep)
        groups["Other"] = list(other.items())

    fig, ax = plt.subplots(figsize=(9, 5), dpi=144)
    fig.patch.set_facecolor(SURFACE)
    multi = len(groups) > 1

    if chart_type == "line":
        _style(ax, "y")
        labels = list(OrderedDict.fromkeys(str(p[0]) for pts in groups.values() for p in pts))
        pos = {lab: i for i, lab in enumerate(labels)}
        for i, (name, pts) in enumerate(groups.items()):
            xs = [pos[str(p[0])] for p in pts]
            ys = [p[1] for p in pts]
            ax.plot(xs, ys, color=SERIES[i], linewidth=2, marker="o", markersize=3.5,
                    markeredgecolor=SURFACE, markeredgewidth=1, label=name)
            if not multi and ys:  # direct-label the last point only
                ax.annotate(_fmt(ys[-1]), (xs[-1], ys[-1]), textcoords="offset points",
                            xytext=(6, 0), va="center", fontsize=9, color=TEXT_PRIMARY)
        step = max(1, len(labels) // 12)
        rotate = len(labels) > 8
        ax.set_xticks(range(0, len(labels), step))
        ax.set_xticklabels(labels[::step], rotation=45 if rotate else 0, ha="right" if rotate else "center")
        ax.set_xlim(-0.5, len(labels) - 0.5 + (0.8 if not multi else 0))
        ax.set_ylim(bottom=0)
    else:
        horizontal = chart_type == "hbar"
        _style(ax, "x" if horizontal else "y")
        cats = list(OrderedDict.fromkeys(str(p[0]) for pts in groups.values() for p in pts))
        n = len(groups)
        width = 0.72 / n
        for i, (name, pts) in enumerate(groups.items()):
            lookup = {str(p[0]): p[1] for p in pts}
            pos = [c + (i - (n - 1) / 2) * width for c in range(len(cats))]
            vals = [lookup.get(c, 0.0) for c in cats]
            if horizontal:
                bars = ax.barh(pos, vals, height=width * 0.92, color=SERIES[i], label=name)
            else:
                bars = ax.bar(pos, vals, width=width * 0.92, color=SERIES[i], label=name)
            if not multi and len(cats) <= 15:
                ax.bar_label(bars, labels=[_fmt(v) for v in vals], padding=3, fontsize=8.5,
                             color=TEXT_PRIMARY)
        if horizontal:
            ax.set_yticks(range(len(cats)))
            ax.set_yticklabels(cats)
            ax.invert_yaxis()  # largest (first) at the top
        else:
            ax.set_xticks(range(len(cats)))
            rotate = len(cats) > 6 or max((len(c) for c in cats), default=0) > 10
            ax.set_xticklabels(cats, rotation=30 if rotate else 0, ha="right" if rotate else "center")

    value_label = spec.get("y_label") or y
    if chart_type == "hbar":
        ax.set_xlabel(value_label, color=TEXT_SECONDARY, fontsize=9)
    else:
        ax.set_ylabel(value_label, color=TEXT_SECONDARY, fontsize=9)
    ax.set_title(spec.get("title") or "", loc="left", color=TEXT_PRIMARY, fontsize=12.5,
                 fontweight="bold", pad=14)
    if multi:
        leg = ax.legend(frameon=False, fontsize=9, loc="upper left", bbox_to_anchor=(1.0, 1.0))
        for t in leg.get_texts():
            t.set_color(TEXT_PRIMARY)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(out_path, facecolor=SURFACE)
    plt.close(fig)
    return str(out_path)
