"""Codex-style terminal token activity graphs for Claude Code.

The TUI combines exact token rows from the standalone local index with the
aggregates Claude keeps in ``stats-cache.json``. If historical transcripts are
gone, model totals remain source data while their calendar placement is marked
as reconstructed rather than presented as exact.
"""

from __future__ import print_function

import argparse
import bisect
import json
import math
import os
import select
import shutil
import signal
import sqlite3
import sys
from collections import defaultdict
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timedelta
from io import StringIO
from pathlib import Path

from collector import DEFAULT_DB_PATH

DEFAULT_STATS_PATH = Path(
    os.environ.get("CLAUDE_STATS_CACHE", Path.home() / ".claude" / "stats-cache.json")
)
MODES = ("daily", "weekly", "cumulative")
MONTH_NAMES = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
DAY_NAMES = ("Su", "Mo", "Tu", "We", "Th", "Fr", "Sa")

MAGENTA = (207, 68, 202)
TEXT = (240, 242, 245)
MUTED = (139, 148, 169)
ORANGE = (255, 160, 101)
GOLD = (202, 181, 139)
HEAT = (
    (45, 51, 58),
    (77, 75, 63),
    (116, 105, 79),
    (167, 145, 104),
    (255, 226, 165),
)


class UsageSnapshot(object):
    def __init__(
        self,
        daily,
        lifetime,
        peak,
        streak,
        best_streak,
        longest_task_seconds,
        exact_tokens=0,
        reconstructed_tokens=0,
        model_coverage=None,
        raw_date_range=None,
        stats_date_range=None,
    ):
        self.daily = daily
        self.lifetime = int(lifetime)
        self.peak = int(peak)
        self.streak = int(streak)
        self.best_streak = int(best_streak)
        self.longest_task_seconds = int(longest_task_seconds)
        self.exact_tokens = int(exact_tokens)
        self.reconstructed_tokens = int(reconstructed_tokens)
        self.model_coverage = model_coverage or {}
        self.raw_date_range = raw_date_range or (None, None)
        self.stats_date_range = stats_date_range or (None, None)

    @property
    def exact_percent(self):
        if self.lifetime <= 0:
            return 0.0
        return 100.0 * self.exact_tokens / self.lifetime

    @property
    def reconstructed_percent(self):
        if self.lifetime <= 0:
            return 0.0
        return 100.0 * self.reconstructed_tokens / self.lifetime


class Palette(object):
    def __init__(self, enabled=True):
        self.enabled = bool(enabled)

    def color(self, text, rgb, bold=False):
        if not self.enabled:
            return str(text)
        weight = "1;" if bold else ""
        return "\033[{}38;2;{};{};{}m{}\033[0m".format(
            weight, rgb[0], rgb[1], rgb[2], text
        )

    def text(self, text, bold=False):
        return self.color(text, TEXT, bold=bold)

    def muted(self, text):
        return self.color(text, MUTED)

    def orange(self, text, bold=False):
        return self.color(text, ORANGE, bold=bold)

    def magenta(self, text):
        return self.color(text, MAGENTA)


def canonical_model(model):
    model = str(model or "unknown").lower()
    families = (
        "claude-fable-5",
        "claude-mythos-5",
        "claude-opus-4-8",
        "claude-opus-4-7",
        "claude-opus-4-6",
        "claude-opus-4-5",
        "claude-sonnet-4-7",
        "claude-sonnet-4-6",
        "claude-sonnet-4-5",
        "claude-haiku-4-7",
        "claude-haiku-4-6",
        "claude-haiku-4-5",
    )
    for family in families:
        if model.startswith(family):
            return family
    return model


def load_json(path):
    try:
        with Path(path).open(encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def parse_timestamp(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


def read_stats_cache(path):
    raw = load_json(path)
    totals = defaultdict(int)
    daily_direct = defaultdict(lambda: defaultdict(int))
    for model, usage in (raw.get("modelUsage") or {}).items():
        usage = usage or {}
        key = canonical_model(model)
        totals[key] += sum(
            int(usage.get(field) or 0)
            for field in (
                "inputTokens",
                "outputTokens",
                "cacheReadInputTokens",
                "cacheCreationInputTokens",
            )
        )
    for row in raw.get("dailyModelTokens") or []:
        day = str(row.get("date") or "")[:10]
        if len(day) != 10:
            continue
        for model, tokens in (row.get("tokensByModel") or {}).items():
            daily_direct[canonical_model(model)][day] += int(tokens or 0)
    return dict(totals), {model: dict(days) for model, days in daily_direct.items()}


def read_raw_usage(db_path):
    raw_daily_all = defaultdict(lambda: defaultdict(int))
    raw_daily_direct = defaultdict(lambda: defaultdict(int))
    raw_model_all = defaultdict(int)
    longest_task = 0
    path = Path(db_path)
    if not path.exists():
        return raw_daily_all, raw_daily_direct, raw_model_all, longest_task

    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT substr(timestamp, 1, 10) AS day,
                   coalesce(model, 'unknown') AS model,
                   sum(input_tokens) AS input_tokens,
                   sum(output_tokens) AS output_tokens,
                   sum(cache_read_tokens) AS cache_read_tokens,
                   sum(cache_creation_tokens) AS cache_creation_tokens
            FROM turns
            WHERE timestamp IS NOT NULL AND length(timestamp) >= 10
            GROUP BY substr(timestamp, 1, 10), coalesce(model, 'unknown')
            """
        ).fetchall()
        for row in rows:
            model = canonical_model(row["model"])
            day = row["day"]
            direct = int(row["input_tokens"] or 0) + int(row["output_tokens"] or 0)
            total = direct + int(row["cache_read_tokens"] or 0) + int(
                row["cache_creation_tokens"] or 0
            )
            raw_daily_direct[model][day] += direct
            raw_daily_all[model][day] += total
            raw_model_all[model] += total

        # A Claude session can be resumed for weeks, so session first/last is not
        # a useful "task" duration. Treat gaps over one hour as a new task.
        current_session = None
        block_start = None
        previous = None
        for row in connection.execute(
            """
            SELECT session_id, timestamp
            FROM turns
            WHERE timestamp IS NOT NULL
            ORDER BY session_id, timestamp
            """
        ):
            moment = parse_timestamp(row["timestamp"])
            if moment is None:
                continue
            if (
                row["session_id"] != current_session
                or previous is None
                or (moment - previous).total_seconds() > 3600
            ):
                block_start = moment
            if block_start is not None:
                longest_task = max(longest_task, int((moment - block_start).total_seconds()))
            current_session = row["session_id"]
            previous = moment
    except sqlite3.Error:
        pass
    finally:
        connection.close()
    return raw_daily_all, raw_daily_direct, raw_model_all, longest_task


def allocate_integer(total, weights):
    """Allocate an integer total proportionally while preserving it exactly."""
    weights = {key: int(value) for key, value in weights.items() if int(value) > 0}
    if total <= 0 or not weights:
        return {}
    weight_sum = sum(weights.values())
    result = {
        key: (int(total) * weight) // weight_sum for key, weight in weights.items()
    }
    remainder = int(total) - sum(result.values())
    if remainder:
        largest = max(weights, key=weights.get)
        result[largest] += remainder
    return result


def calculate_streaks(daily):
    days = sorted(
        date.fromisoformat(day) for day, tokens in daily.items() if int(tokens) > 0
    )
    if not days:
        return 0, 0
    best = current = 1
    previous = days[0]
    for day in days[1:]:
        if day == previous + timedelta(days=1):
            current += 1
        else:
            current = 1
        best = max(best, current)
        previous = day
    return current, best


def build_snapshot(db_path=DEFAULT_DB_PATH, stats_path=DEFAULT_STATS_PATH):
    stats_payload = load_json(stats_path)
    stats_totals, stats_daily_direct = read_stats_cache(stats_path)
    raw_daily_all, raw_daily_direct, raw_model_all, longest_task = read_raw_usage(db_path)

    daily = defaultdict(int)
    for model_days in raw_daily_all.values():
        for day, tokens in model_days.items():
            daily[day] += int(tokens)

    models = set(stats_totals) | set(raw_model_all) | set(stats_daily_direct)
    model_coverage = {}
    for model in models:
        raw_total = int(raw_model_all.get(model, 0))
        target_total = max(raw_total, int(stats_totals.get(model, 0)))
        residual = target_total - raw_total
        model_coverage[model] = {
            "exact": raw_total,
            "total": target_total,
            "reconstructed": residual,
        }
        if residual <= 0:
            continue
        weights = {}
        all_days = set(stats_daily_direct.get(model, {})) | set(
            raw_daily_direct.get(model, {})
        )
        for day in all_days:
            missing_direct = int(stats_daily_direct.get(model, {}).get(day, 0)) - int(
                raw_daily_direct.get(model, {}).get(day, 0)
            )
            if missing_direct > 0:
                weights[day] = missing_direct
        if not weights:
            weights = dict(stats_daily_direct.get(model, {}))
        if not weights:
            fallback_day = max(raw_daily_all.get(model, {}) or {date.today().isoformat(): 0})
            weights = {fallback_day: 1}
        for day, tokens in allocate_integer(residual, weights).items():
            daily[day] += tokens

    normalized = dict(sorted((day, int(tokens)) for day, tokens in daily.items()))
    lifetime = sum(normalized.values())
    peak = max(normalized.values()) if normalized else 0
    streak, best_streak = calculate_streaks(normalized)
    raw_days = sorted(day for days in raw_daily_all.values() for day in days)
    stats_days = sorted(day for days in stats_daily_direct.values() for day in days)
    stats_start = str(stats_payload.get("firstSessionDate") or "")[:10] or (
        stats_days[0] if stats_days else None
    )
    stats_end = str(stats_payload.get("lastComputedDate") or "")[:10] or (
        stats_days[-1] if stats_days else None
    )
    exact_tokens = sum(int(value) for value in raw_model_all.values())
    return UsageSnapshot(
        normalized,
        lifetime,
        peak,
        streak,
        best_streak,
        longest_task,
        exact_tokens=exact_tokens,
        reconstructed_tokens=max(0, lifetime - exact_tokens),
        model_coverage=model_coverage,
        raw_date_range=(raw_days[0], raw_days[-1]) if raw_days else (None, None),
        stats_date_range=(stats_start, stats_end),
    )


def maybe_scan(db_path, projects_dir=None):
    try:
        from collector import scan

        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            scan(projects_dir=projects_dir, db_path=Path(db_path), verbose=False)
        return True
    except Exception:
        return False


def fmt_tokens(value):
    value = float(value or 0)
    if value >= 1_000_000_000_000:
        return "{:.1f}T".format(value / 1_000_000_000_000).rstrip("0").rstrip(".")
    if value >= 1_000_000_000:
        return "{:.1f}B".format(value / 1_000_000_000).rstrip("0").rstrip(".")
    if value >= 1_000_000:
        return "{:.0f}M".format(value / 1_000_000)
    if value >= 1_000:
        return "{:.0f}K".format(value / 1_000)
    return str(int(value))


def fmt_duration(seconds):
    seconds = max(0, int(seconds or 0))
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours:
        return "{}h {}m".format(hours, minutes)
    return "{}m".format(minutes)


def shift_month(first, delta):
    month_index = first.year * 12 + first.month - 1 + delta
    return date(month_index // 12, month_index % 12 + 1, 1)


def period_bounds(today):
    current_month = date(today.year, today.month, 1)
    first_month = shift_month(current_month, -11)
    sunday_offset = (first_month.weekday() + 1) % 7
    grid_start = first_month - timedelta(days=sunday_offset)
    return first_month, grid_start, today


def chart_geometry(today):
    first_month, grid_start, end = period_bounds(today)
    weeks = int(math.ceil(((end - grid_start).days + 1) / 7.0))
    return first_month, grid_start, end, weeks


def month_label_line(first_month, grid_start, weeks, slot, left_width):
    width = weeks * slot
    characters = [" "] * width
    last_end = -1
    for offset in range(12):
        month = shift_month(first_month, offset)
        week = max(0, (month - grid_start).days // 7)
        position = week * slot
        label = MONTH_NAMES[month.month - 1]
        if position <= last_end or position + len(label) > width:
            continue
        characters[position : position + len(label)] = list(label)
        last_end = position + len(label)
    return " " * left_width + "".join(characters).rstrip()


def heat_levels(values):
    positive = sorted(int(value) for value in values if int(value) > 0)
    if not positive:
        return []
    thresholds = []
    for quantile in (0.20, 0.45, 0.70, 0.90):
        index = min(len(positive) - 1, int((len(positive) - 1) * quantile))
        thresholds.append(positive[index])
    return thresholds


def heat_level(value, thresholds):
    if int(value) <= 0:
        return 0
    return min(4, 1 + bisect.bisect_right(thresholds, int(value)))


def chart_slot(width, weeks, left_width):
    available = max(weeks, width - left_width - 2)
    return max(1, min(4, available // max(1, weeks)))


def color_block(palette, rgb, width):
    return palette.color("█" * max(1, width), rgb)


def render_header(snapshot, mode, palette, width):
    lines = [palette.magenta("/usage {}".format(mode)), ""]
    lines.append(
        palette.text("Token activity", bold=True)
        + palette.muted("   last 12 months")
    )
    approximate = snapshot.reconstructed_tokens > 0
    approximation = "≈" if approximate else ""
    segments = [
        palette.muted("Lifetime ") + palette.orange(fmt_tokens(snapshot.lifetime)),
        palette.muted("Peak {}".format(approximation))
        + palette.orange(fmt_tokens(snapshot.peak)),
        palette.muted("Streak {}".format(approximation))
        + palette.orange("{}d".format(snapshot.streak))
        + palette.orange(" (best {}d)".format(snapshot.best_streak)),
        palette.muted("Longest observed ")
        + palette.orange(fmt_duration(snapshot.longest_task_seconds)),
    ]
    if width >= 112:
        lines.append((palette.muted(" · ")).join(segments))
    else:
        lines.append((palette.muted(" · ")).join(segments[:2]))
        lines.append((palette.muted(" · ")).join(segments[2:]))
    lines.append(
        palette.muted("Sources ")
        + palette.orange("{:.1f}% exact turns".format(snapshot.exact_percent))
        + palette.muted(" · ")
        + palette.orange(
            "{:.1f}% aggregate allocation".format(snapshot.reconstructed_percent)
        )
    )
    lines.append("")
    return lines


def render_tabs(active, palette):
    parts = []
    for mode in MODES:
        if mode == active:
            parts.append(palette.orange(mode, bold=True))
        else:
            parts.append(palette.muted(mode))
    return "      " + palette.muted(" · ").join(parts)


def render_daily(snapshot, palette, width, today):
    first_month, grid_start, _end, weeks = chart_geometry(today)
    left_width = 6
    slot = chart_slot(width, weeks, left_width)
    block_width = max(1, slot - 1)
    thresholds = heat_levels(snapshot.daily.values())
    lines = [palette.muted(month_label_line(first_month, grid_start, weeks, slot, left_width))]
    for row_index, day_name in enumerate(DAY_NAMES):
        cells = []
        for week in range(weeks):
            day = grid_start + timedelta(days=week * 7 + row_index)
            if day < first_month or day > today:
                cells.append(" " * block_width)
                cells.append(" " * (slot - block_width))
                continue
            value = snapshot.daily.get(day.isoformat(), 0)
            level = heat_level(value, thresholds)
            cells.append(color_block(palette, HEAT[level], block_width))
            cells.append(" " * (slot - block_width))
        lines.append(palette.muted("{:<4}".format(day_name)) + "  " + "".join(cells).rstrip())
    lines.append("")
    legend = [palette.muted("Less ")]
    for rgb in HEAT:
        legend.append(color_block(palette, rgb, 1))
        legend.append(" ")
    legend.append(palette.muted("More"))
    lines.append("      " + "".join(legend))
    lines.append(render_tabs("daily", palette))
    return lines


def weekly_series(snapshot, today):
    first_month, grid_start, _end, weeks = chart_geometry(today)
    values = []
    for week in range(weeks):
        start = grid_start + timedelta(days=week * 7)
        values.append(
            sum(
                int(snapshot.daily.get((start + timedelta(days=offset)).isoformat(), 0))
                for offset in range(7)
                if start + timedelta(days=offset) <= today
            )
        )
    return first_month, grid_start, values


def render_bars(snapshot, mode, palette, width, height, today):
    first_month, grid_start, weekly = weekly_series(snapshot, today)
    weeks = len(weekly)
    left_width = 6
    slot = chart_slot(width, weeks, left_width)
    block_width = max(1, slot - 1)
    if mode == "cumulative":
        before = sum(
            int(tokens)
            for day, tokens in snapshot.daily.items()
            if date.fromisoformat(day) < grid_start
        )
        running = before
        values = []
        for value in weekly:
            running += value
            values.append(running)
    else:
        values = weekly
    maximum = max(values) if values else 0
    chart_height = max(6, min(14, height - 13))
    lines = [palette.muted(month_label_line(first_month, grid_start, weeks, slot, left_width))]
    for row in range(chart_height):
        threshold = chart_height - row
        cells = []
        for value in values:
            units = int(round((float(value) / maximum) * chart_height)) if maximum else 0
            if value and units == 0:
                units = 1
            if units >= threshold:
                cells.append(color_block(palette, GOLD, block_width))
            else:
                cells.append(" " * block_width)
            cells.append(" " * (slot - block_width))
        axis = "max" if row == 0 else ""
        lines.append(palette.muted("{:<4}".format(axis)) + "  " + "".join(cells).rstrip())
    lines.append(palette.muted("{:<4}".format("0")))
    lines.append("")
    if mode == "weekly":
        lines.append(
            "      "
            + palette.muted("Each column = 1 week · tallest ")
            + palette.orange(fmt_tokens(maximum))
        )
    else:
        lines.append(
            "      "
            + palette.muted("Running total · top ")
            + palette.orange(fmt_tokens(maximum))
        )
    lines.append(render_tabs(mode, palette))
    return lines


def render(snapshot, mode="daily", width=120, height=30, colors=True, today=None):
    if mode not in MODES:
        raise ValueError("unknown mode: {}".format(mode))
    today = today or date.today()
    palette = Palette(colors)
    lines = render_header(snapshot, mode, palette, width)
    if mode == "daily":
        lines.extend(render_daily(snapshot, palette, width, today))
    else:
        lines.extend(render_bars(snapshot, mode, palette, width, height, today))
    return "\n".join(lines)


def render_audit(snapshot, db_path=DEFAULT_DB_PATH, stats_path=DEFAULT_STATS_PATH):
    """Render a plain-language provenance report for the current graph."""
    lines = [
        "Claude usage source audit",
        "",
        "Exact turn tokens:       {:>15,}  ({:6.2f}%)".format(
            snapshot.exact_tokens, snapshot.exact_percent
        ),
        "Aggregate allocation:    {:>15,}  ({:6.2f}%)".format(
            snapshot.reconstructed_tokens, snapshot.reconstructed_percent
        ),
        "Combined local lifetime: {:>15,}".format(snapshot.lifetime),
        "",
        "Exact index: {}".format(Path(db_path).expanduser()),
        "Claude aggregate: {}".format(Path(stats_path).expanduser()),
        "Exact date range: {} to {}".format(
            snapshot.raw_date_range[0] or "n/a", snapshot.raw_date_range[1] or "n/a"
        ),
        "Aggregate date range: {} to {}".format(
            snapshot.stats_date_range[0] or "n/a", snapshot.stats_date_range[1] or "n/a"
        ),
        "",
        "Model                 exact turns       local total       allocated   exact",
    ]
    ordered = sorted(
        snapshot.model_coverage.items(),
        key=lambda item: int(item[1]["total"]),
        reverse=True,
    )
    for model, coverage in ordered:
        total = int(coverage["total"])
        exact = int(coverage["exact"])
        reconstructed = int(coverage["reconstructed"])
        percent = 100.0 * exact / total if total else 0.0
        lines.append(
            "{:<21} {:>13,} {:>17,} {:>15,} {:>6.2f}%".format(
                model[:21], exact, total, reconstructed, percent
            )
        )
    lines.extend(
        [
            "",
            "The allocated amount is a real per-model total from Claude's local",
            "stats cache. Only its placement across historical days is reconstructed.",
            "This is token activity, not subscription-quota or billing data.",
        ]
    )
    return "\n".join(lines)


def interactive(snapshot, initial_mode, colors=True):
    try:
        import termios
        import tty
    except ImportError:
        size = shutil.get_terminal_size((120, 30))
        print(render(snapshot, initial_mode, size.columns, size.lines, colors=colors))
        return 0

    mode_index = MODES.index(initial_mode)
    previous_settings = termios.tcgetattr(sys.stdin.fileno())
    resized = [True]

    def on_resize(_signum, _frame):
        resized[0] = True

    previous_handler = signal.signal(signal.SIGWINCH, on_resize)
    try:
        tty.setcbreak(sys.stdin.fileno())
        sys.stdout.write("\033[?1049h\033[?25l")
        sys.stdout.flush()
        while True:
            if resized[0]:
                size = shutil.get_terminal_size((120, 30))
                screen = render(
                    snapshot,
                    MODES[mode_index],
                    width=size.columns,
                    height=size.lines,
                    colors=colors,
                )
                sys.stdout.write("\033[H\033[2J" + screen + "\033[0m\n")
                sys.stdout.flush()
                resized[0] = False
            readable, _, _ = select.select([sys.stdin], [], [], 0.25)
            if not readable:
                continue
            key = os.read(sys.stdin.fileno(), 1)
            if key in (b"q", b"Q", b"\x03", b"\x1b"):
                if key == b"\x1b":
                    following, _, _ = select.select([sys.stdin], [], [], 0.02)
                    if following and os.read(sys.stdin.fileno(), 1) == b"[":
                        final, _, _ = select.select([sys.stdin], [], [], 0.02)
                        arrow = os.read(sys.stdin.fileno(), 1) if final else b""
                        if arrow == b"C":
                            mode_index = (mode_index + 1) % len(MODES)
                            resized[0] = True
                            continue
                        if arrow == b"D":
                            mode_index = (mode_index - 1) % len(MODES)
                            resized[0] = True
                            continue
                break
            if key in (b"d", b"D"):
                mode_index = 0
                resized[0] = True
            elif key in (b"w", b"W"):
                mode_index = 1
                resized[0] = True
            elif key in (b"c", b"C"):
                mode_index = 2
                resized[0] = True
            elif key in (b"l", b"L", b" "):
                mode_index = (mode_index + 1) % len(MODES)
                resized[0] = True
            elif key in (b"h", b"H"):
                mode_index = (mode_index - 1) % len(MODES)
                resized[0] = True
    finally:
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, previous_settings)
        signal.signal(signal.SIGWINCH, previous_handler)
        sys.stdout.write("\033[0m\033[?25h\033[?1049l")
        sys.stdout.flush()
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="claude-usage",
        description="Codex-style daily, weekly, and cumulative Claude token graphs.",
    )
    parser.add_argument("mode", nargs="?", choices=MODES, default="daily")
    parser.add_argument("--no-scan", action="store_true", help="Do not incrementally scan JSONL first")
    parser.add_argument("--once", action="store_true", help="Render once instead of opening the interactive TUI")
    parser.add_argument("--audit", action="store_true", help="Print exact/reconstructed source coverage")
    parser.add_argument("--no-color", action="store_true")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--stats", type=Path, default=DEFAULT_STATS_PATH)
    parser.add_argument("--projects-dir", type=Path, help="Scan one custom JSONL root")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if not args.no_scan:
        maybe_scan(args.db, projects_dir=args.projects_dir)
    snapshot = build_snapshot(args.db, args.stats)
    if args.audit:
        print(render_audit(snapshot, args.db, args.stats))
        return 0
    colors = not args.no_color and "NO_COLOR" not in os.environ
    if args.once or not (sys.stdin.isatty() and sys.stdout.isatty()):
        size = shutil.get_terminal_size((120, 30))
        print(render(snapshot, args.mode, size.columns, size.lines, colors=colors))
        return 0
    return interactive(snapshot, args.mode, colors=colors)


if __name__ == "__main__":
    raise SystemExit(main())
