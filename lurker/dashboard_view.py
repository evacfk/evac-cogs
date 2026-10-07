"""Template + pure helpers for the Lurker dashboard overview. No discord/redbot imports.

Read-only by design: it shows settings and COUNTS only. Member ids, per-member
activity, stored roles and the report event log are never put in the page data.
All values reach the template as variables and go through `|e`.
"""
from __future__ import annotations

from typing import Optional


def format_age(ts: Optional[float], now_ts: float) -> str:
    if not ts:
        return "never"
    seconds = max(int(now_ts - ts), 0)
    if seconds < 5:
        return "just now"
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _sweep_text(last_sweep: dict, now_ts: float) -> str:
    if not last_sweep:
        return "never"
    text = (
        f"{format_age(last_sweep.get('ts'), now_ts)}: {last_sweep.get('flagged', 0)} flagged "
        f"of {last_sweep.get('candidates', 0)} candidates"
    )
    if last_sweep.get("aborted"):
        text += " (ABORTED by safety limit)"
    return text


def _yearclub_text(last: dict, now_ts: float) -> str:
    if not last:
        return "never run"
    text = f"{format_age(last.get('ts'), now_ts)}: {last.get('changed', 0)} changed"
    if last.get("errors"):
        text += f", {last['errors']} errors"
    if last.get("aborted"):
        text += " (ABORTED)"
    return text


def build_overview(
    *,
    enabled: bool,
    threshold_days: int,
    role_name: Optional[str],
    channel_name: Optional[str],
    report_channel_name: Optional[str],
    report_interval_days: int,
    last_report_ts: float,
    last_sweep: dict,
    sweep_max: int,
    exempt_names: list,
    yearclub_enabled: bool,
    yearclub_role_count: int,
    yearclub_last: dict,
    lurker_member_count: Optional[int],
    tracked_count: int,
    now_ts: float,
) -> list:
    """Ordered (label, value) pairs of plain strings -- the only data the page sees."""
    return [
        ("Automatic sweep", "enabled" if enabled else "disabled"),
        ("Inactivity threshold", f"{threshold_days} days"),
        ("Lurker role", role_name or "not set"),
        ("Reactivation channel", channel_name or "not set"),
        ("Members currently flagged", "unknown" if lurker_member_count is None else str(lurker_member_count)),
        ("Members with tracked activity", str(tracked_count)),
        ("Last sweep", _sweep_text(last_sweep, now_ts)),
        ("Sweep safety limit", f"{sweep_max} flags per sweep"),
        ("Exempt roles", ", ".join(exempt_names) if exempt_names else "none (mods/boosters can be flagged)"),
        (
            "Weekly report",
            f"{report_channel_name} every {report_interval_days} days, last sent {format_age(last_report_ts, now_ts)}"
            if report_channel_name
            else "off",
        ),
        (
            "Year club",
            ("enabled" if yearclub_enabled else "disabled")
            + f", {yearclub_role_count} roles, last run {_yearclub_text(yearclub_last, now_ts)}",
        ),
    ]


PAGE_TEMPLATE = """\
<div class="lurker-page">
  <h3>Lurker</h3>
  <p class="text-muted">Read-only overview for {{ guild_name|e }}. Change settings with <code>.lurkerset</code>.</p>
  {% if denied %}
  <p>You need the mod role, Manage Roles or Manage Server to view this page.</p>
  {% else %}
  <div class="table-responsive">
    <table class="table table-sm align-middle">
      <tbody>
        {% for label, value in rows %}
        <tr><th scope="row">{{ label|e }}</th><td>{{ value|e }}</td></tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
  {% endif %}
</div>
"""
