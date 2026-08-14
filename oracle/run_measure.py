import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from run_broadcast import log
from x_search import Fatal, posts_by_ids, user_by_username

HERE = os.path.dirname(os.path.abspath(__file__))
REPLY_STATE = os.path.join(HERE, "state", "reply_state.json")
OUT = os.path.join(HERE, "state", "measure.jsonl")

HANDLE = os.environ.get("BANKNOTE_X_HANDLE", "banknotelolai")
WINDOW_DAYS = 28
USD_PER_POST = 0.005


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def our_replies():
    try:
        with open(REPLY_STATE) as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        return []
    cutoff = (now_utc().date() - dt.timedelta(days=WINDOW_DAYS)).isoformat()
    return [p for p in state.get("posts") or []
            if p.get("id") and (p.get("day") or "") >= cutoff]


def main():
    posts = our_replies()
    billed = 0
    try:
        account = user_by_username(HANDLE)
        billed += 1
        rows = []
        if posts:
            metrics = {m["id"]: m for m in
                       posts_by_ids([p["id"] for p in posts])}
            billed += len(metrics)
            for p in posts:
                m = metrics.get(str(p["id"]))
                if m:
                    rows.append({**p, **m})
    except Fatal as e:
        log(f"{e}")
        return 0

    log(f"@{HANDLE}: {account.get('followers')} followers")
    if not rows:
        log("no replies recorded in the window - nothing to measure yet")
    for r in rows:
        log(f"  {r['day']}  {r['ccy']:4s}  {r.get('impressions') or 0:5d} "
            f"impressions  {r.get('likes') or 0:2d} likes  "
            f"{r.get('replies') or 0:2d} replies")
    total = lambda k: sum(r.get(k) or 0 for r in rows)
    if rows:
        log(f"  {len(rows)} replies: {total('impressions')} impressions, "
            f"{total('likes')} likes, {total('replies')} replies back")
    log(f"  {billed} object(s) read, ~${billed * USD_PER_POST:.2f}")

    snap = {
        "t": now_utc().strftime("%Y-%m-%dT%H:%MZ"),
        "followers": account.get("followers"),
        "replies_measured": len(rows),
        "impressions": total("impressions"),
        "likes": total("likes"),
        "replies_to_ours": total("replies"),
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "a") as fh:
        fh.write(json.dumps(snap, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
