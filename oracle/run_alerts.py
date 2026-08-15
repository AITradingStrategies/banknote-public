import argparse
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _util import write_json_atomic
from fixing_prototype import src_fxratesapi
from run_broadcast import (
    HERE, TWEET_LIMIT, covered_ccys, currency_names, fmt_rate, hashtag,
    load_json, log, post_to_x,
)

STATE_FILE = os.path.join(HERE, "state", "alerts_state.json")
BASELINE_FILE = os.path.join(HERE, "state", "broadcast_state.json")

ALERT_PCT = 2.0
SIGMA_MULT = 6.0
REVIEW_PCT = 10.0
OUTLIER_PCT = 2.0
CONFIRM_POLLS = 2
MAX_PER_DAY = 6
COMMENTARY_DEFER_H = 3
MAX_BASELINE_AGE_H = 36

ANALYSIS_FILE = os.path.join(HERE, "state", "analysis.json")
ARCHIVE = os.path.join(HERE, "archive")


def thresholds():
    payload = load_json(ANALYSIS_FILE, {})
    out = {}
    for ccy, entry in (payload.get("currencies") or {}).items():
        vol = entry.get("volatility") or {}
        sigma = vol.get("sigma_pct")
        if sigma:
            out[ccy] = max(ALERT_PCT, SIGMA_MULT * sigma)
    return out


def source_ranges():
    if not os.path.isdir(ARCHIVE):
        return {}
    for day in sorted(os.listdir(ARCHIVE), reverse=True):
        path = os.path.join(ARCHIVE, day, "sources_raw.json")
        if not os.path.exists(path):
            continue
        tables = load_json(path, {})
        if not isinstance(tables, dict) or len(tables) < 2:
            continue
        per = {}
        for quotes in tables.values():
            if not isinstance(quotes, dict):
                continue
            for ccy, rate in quotes.items():
                try:
                    r = float(rate)
                except (TypeError, ValueError):
                    continue
                if r > 0:
                    per.setdefault(ccy, []).append(r)
        return {c: (min(v), max(v)) for c, v in per.items() if len(v) >= 3}
    return {}


def outlier_pct(rate, rng):
    if not rng:
        return 0.0
    lo, hi = rng
    if rate > hi:
        return (rate / hi - 1) * 100
    if rate < lo:
        return (lo / rate - 1) * 100
    return 0.0


def baseline():
    snap = load_json(BASELINE_FILE, {})
    rates = snap.get("ref_rates") or {}
    taken = snap.get("taken")
    if not rates or not taken:
        return None, None, None
    try:
        t = datetime.fromisoformat(taken)
    except ValueError:
        return None, None, None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    age_h = (datetime.now(timezone.utc) - t).total_seconds() / 3600
    return rates, age_h, t


def since_label(taken):
    now = datetime.now(timezone.utc)
    if taken.date() == now.date():
        return f"since {taken:%H:%M} UTC"
    return f"since {taken:%d %b}, {taken:%H:%M} UTC"


def compose(ccy, name, rate, pct, since):
    sign = "+" if pct > 0 else ""
    tag = hashtag(name)
    who = f"{tag} (${ccy})" if tag else f"${ccy}"
    return (f"{who} {sign}{pct:.1f}% {since}.\n\n"
            f"{fmt_rate(rate)}/USD, intraday market rate.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--post", action="store_true", help="publish to X")
    ap.add_argument("--commit", action="store_true",
                    help="save poll state without posting")
    ap.add_argument("--force", action="store_true",
                    help="post even past the review threshold or the daily cap")
    args = ap.parse_args()

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log("intraday alert check")

    base, age_h, taken = baseline()
    if not base:
        log("no reference snapshot yet (needs one digest run to write "
            "ref_rates) - nothing to measure against")
        return 0
    if age_h > MAX_BASELINE_AGE_H:
        log(f"fixing snapshot is {age_h:.1f}h old (limit {MAX_BASELINE_AGE_H}h)"
            " - skipping rather than comparing against stale numbers")
        return 0

    try:
        live = src_fxratesapi()
    except Exception as e:
        log(f"live source unavailable ({type(e).__name__}) - nothing to do")
        return 0
    log(f"{len(live)} live quotes; baseline {len(base)} currencies, {age_h:.1f}h old")

    state = load_json(STATE_FILE, {})
    if state.get("date") != today:
        state = {"date": today, "alerted": [], "alerted_at": {},
                 "pending": {}, "count": 0}
    alerted = set(state.get("alerted") or [])
    pending = dict(state.get("pending") or {})
    commentary = load_json(os.path.join(HERE, "state", "commentary_state.json"), {})
    commentary_recent = commentary.get("recent") or {}
    now_ts = int(datetime.now(timezone.utc).timestamp())

    covered = covered_ccys()
    bars = thresholds()
    ranges = source_ranges()
    if bars:
        log(f"per-currency bars loaded for {len(bars)} currencies "
            f"(floor {ALERT_PCT}%, {SIGMA_MULT} sigma)")
    moves = {}
    for ccy, now in live.items():
        if ccy not in covered:
            continue
        prev = base.get(ccy)
        if not prev or not now:
            continue
        pct = (prev / now - 1) * 100
        bar = bars.get(ccy, ALERT_PCT)
        if abs(pct) >= bar and ccy not in alerted:
            moves[ccy] = {"pct": pct, "rate": now, "bar": round(bar, 2),
                          "outlier": round(outlier_pct(now, ranges.get(ccy)), 2)}

    still = {}
    for ccy, m in moves.items():
        seen = int((pending.get(ccy) or {}).get("seen", 0)) + 1
        still[ccy] = {"pct": m["pct"], "rate": m["rate"], "seen": seen,
                      "bar": m["bar"], "outlier": m["outlier"]}
        log(f"  {ccy:4s} {m['pct']:+6.2f}%  (bar {m['bar']}%)  seen {seen}/{CONFIRM_POLLS}"
            + (f"  OUTLIER +{m['outlier']}% outside the fixing range"
               if m["outlier"] >= OUTLIER_PCT else ""))
    for ccy in pending:
        if ccy not in still:
            log(f"  {ccy:4s} fell back below its bar - cleared")
    state["pending"] = still

    ready = [(c, m) for c, m in still.items() if m["seen"] >= CONFIRM_POLLS]
    ready.sort(key=lambda cm: abs(cm[1]["pct"]), reverse=True)

    if not ready:
        log("nothing confirmed this poll")
    elif state.get("count", 0) >= MAX_PER_DAY and not args.force:
        log(f"daily cap reached ({MAX_PER_DAY}) - holding "
            + ", ".join(c for c, _ in ready))
        ready = []

    names = currency_names()
    posted_any = False
    for ccy, m in ready[:1]:
        covered_ago = now_ts - int(commentary_recent.get(ccy) or 0)
        if covered_ago < COMMENTARY_DEFER_H * 3600 and not args.force:
            log(f"HELD: {ccy} had a commentary write-up "
                f"{covered_ago // 60}m ago - deferring, still pending")
            continue
        text = compose(ccy, names.get(ccy), m["rate"], m["pct"], since_label(taken))
        log(f"--- alert ({len(text)} chars) ---")
        for line in text.split("\n"):
            log("  | " + line)
        if len(text) > TWEET_LIMIT:
            log("too long; skipping")
            continue
        if m.get("outlier", 0) >= OUTLIER_PCT and not args.force:
            log(f"HELD: {ccy} live {fmt_rate(m['rate'])} sits {m['outlier']}% outside "
                f"the range all sources agreed on at the last fixing - "
                f"suspected bad print, not posting")
            state["pending"].pop(ccy, None)
            continue
        if abs(m["pct"]) >= REVIEW_PCT and not args.force:
            log(f"REVIEW: {ccy} {m['pct']:+.1f}% from a single source - "
                "check it, then --force")
            if args.post:
                return 2
            continue
        if args.post:
            post_to_x(text)
            alerted.add(ccy)
            state.setdefault("alerted_at", {})[ccy] = now_ts
            state["count"] = int(state.get("count", 0)) + 1
            state["pending"].pop(ccy, None)
            posted_any = True
            log(f"posted alert: {ccy}")

    state["alerted"] = sorted(alerted)
    if args.post or args.commit:
        write_json_atomic(STATE_FILE, state, indent=1, sort_keys=True)
        log(f"state saved -> {STATE_FILE}"
            + (" (posted)" if posted_any else ""))
    else:
        log("dry run - nothing posted, state unchanged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
