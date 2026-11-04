"""Wondercoin distribution maths for `.wld economy`. Pure: no discord or redbot imports.

Most members hold nothing, and counting them would make every average meaningless, so the
percentiles and shares look at *holders* (balance above 0). The balance bands look at everyone.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

MAX_SNAPSHOTS = 52   # one a week for a year is plenty
TOP_N = 10

# (low, high, label); high None means no upper limit.
BUCKETS: Tuple[Tuple[int, Optional[int], str], ...] = (
    (0, 0, "0"),
    (1, 9_999, "1 to 9,999"),
    (10_000, 99_999, "10,000 to 99,999"),
    (100_000, 999_999, "100,000 to 999,999"),
    (1_000_000, 9_999_999, "1M to 9.99M"),
    (10_000_000, None, "10M and up"),
)


def percentile(ascending: Sequence[int], p: int) -> int:
    """Nearest-rank percentile of an ascending list (0 for an empty one)."""
    if not ascending:
        return 0
    rank = max(1, -(-(len(ascending) * p) // 100))  # ceil(n * p / 100) in integers
    return ascending[min(len(ascending), rank) - 1]


def _share(part: int, total: int) -> float:
    return part / total if total else 0.0


def summarize(balances: Sequence[int]) -> Dict:
    """Everything the report shows, from a plain list of balances."""
    values = sorted(max(0, int(b)) for b in balances)
    holders = [v for v in values if v > 0]
    n = len(holders)
    total = sum(holders)
    top1_n = max(1, n // 100) if n else 0
    bands = []
    for low, high, label in BUCKETS:
        members = [v for v in values if v >= low and (high is None or v <= high)]
        bands.append({"label": label, "count": len(members), "coins": sum(members)})
    return {
        "accounts": len(values),
        "holders": n,
        "total": total,
        "mean": total // n if n else 0,
        "median": percentile(holders, 50),
        "p90": percentile(holders, 90),
        "p99": percentile(holders, 99),
        "max": holders[-1] if holders else 0,
        "top10_share": _share(sum(holders[-TOP_N:]), total),
        "top1_n": top1_n,
        "top1pct_share": _share(sum(holders[-top1_n:]) if top1_n else 0, total),
        "bands": bands,
    }


def top_holders(balances: Dict[int, int], n: int = TOP_N) -> List[Tuple[int, int]]:
    """(user id, balance) for the richest `n` holders, biggest first."""
    rich = [(uid, bal) for uid, bal in balances.items() if bal > 0]
    rich.sort(key=lambda item: (-item[1], item[0]))
    return rich[:n]


# ---- snapshots: aggregate numbers only, so a run can show the change since the last one ----

def snapshot(stats: Dict, day: str, active: Optional[Dict] = None) -> Dict:
    """`active` is summarize() of the members without the Lurker role (None when that is unknown)."""
    snap = {
        "date": day,
        "total": stats["total"],
        "holders": stats["holders"],
        "median": stats["median"],
        "p90": stats["p90"],
        "top10_share": round(stats["top10_share"], 4),
    }
    if active is not None:
        snap.update(
            active_total=active["total"],
            active_holders=active["holders"],
            active_median=active["median"],
            active_top10_share=round(active["top10_share"], 4),
        )
    return snap


def add_snapshot(history: Sequence[Dict], entry: Dict) -> List[Dict]:
    """Add `entry`, replacing any earlier one for the same day, and keep the newest MAX_SNAPSHOTS."""
    kept = [s for s in history if s.get("date") != entry["date"]]
    kept.append(entry)
    kept.sort(key=lambda s: s["date"])
    return kept[-MAX_SNAPSHOTS:]


def previous_snapshot(history: Sequence[Dict], day: str) -> Optional[Dict]:
    """The latest snapshot from before `day`, or None if there is none."""
    earlier = [s for s in history if s.get("date", "") < day]
    return max(earlier, key=lambda s: s["date"]) if earlier else None


def _pct(before: int, after: int) -> Optional[float]:
    return (after - before) / before * 100 if before else None


def change(prev: Dict, cur: Dict) -> Dict:
    delta = {
        "since": prev["date"],
        "total": cur["total"] - prev["total"],
        "total_pct": _pct(prev["total"], cur["total"]),
        "holders": cur["holders"] - prev["holders"],
        "median": cur["median"] - prev["median"],
        "median_pct": _pct(prev["median"], cur["median"]),
        "top10_share": cur["top10_share"] - prev["top10_share"],
    }
    if "active_total" in prev and "active_total" in cur:    # older snapshots have no active-only numbers
        delta.update(
            active_total=cur["active_total"] - prev["active_total"],
            active_total_pct=_pct(prev["active_total"], cur["active_total"]),
            active_median=cur["active_median"] - prev["active_median"],
            active_median_pct=_pct(prev["active_median"], cur["active_median"]),
            active_top10_share=cur["active_top10_share"] - prev["active_top10_share"],
        )
    return delta
