import argparse
import hashlib
import hmac
import json
import os
import random
import re
import statistics
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _util import write_json_atomic
from fixing_prototype import SOURCES, compute_fixing

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(HERE, ".."))
COUNTRIES = (os.environ.get("BANKNOTE_COUNTRIES")
             or os.path.join(REPO, "web", "src", "data", "countries.json"))
STATE_FILE = os.path.join(HERE, "state", "broadcast_state.json")

QUORUM = 3
REF_SOURCE = "fxratesapi"
MIN_MOVE_PCT = 0.25
MAX_LINES = 3
REVIEW_PCT = 10.0
TWEET_LIMIT = 280

CASHTAG_PRIORITY = [
    "NGN", "ARS", "TRY", "PHP", "PKR", "EGP", "KES", "GHS", "ZAR",
    "IDR", "VND", "BRL", "INR", "UAH", "ETB", "LBP", "VES",
]


try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError, OSError):
    pass


def log(msg):
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"[{stamp}] {msg}", flush=True)


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def currency_names():
    d = load_json(COUNTRIES, {})
    ccys, display = d.get("ccys") or [], d.get("display") or []
    owners = {}
    for ccy, name in zip(ccys, display):
        owners.setdefault(ccy, []).append(name)
    return {c: names[0] for c, names in owners.items() if len(names) == 1}


def covered_ccys():
    covered = set(load_json(COUNTRIES, {}).get("fixCcys") or [])
    if not covered:
        log("WARNING: no fixCcys in countries.json - nothing is postable")
    return covered


def hashtag(name):
    if not name:
        return None
    flat = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    parts = [p for p in re.split(r"[^A-Za-z0-9]+", flat) if p]
    slug = "".join(p if p[0].isupper() else p.capitalize() for p in parts)
    return f"#{slug}" if slug else None


def pick_cashtag(ccys):
    for c in CASHTAG_PRIORITY:
        if c in ccys:
            return c
    return ccys[0] if ccys else None


def read_sources():
    tables = {}
    for name, fn in SOURCES.items():
        try:
            tables[name] = fn()
            log(f"  [ok]   {name:16s} {len(tables[name]):4d} currencies")
        except Exception as e:
            log(f"  [FAIL] {name:16s} {type(e).__name__}")
    return tables


def fixings_now(tables):
    out = {}
    for ccy in sorted(set().union(*tables.values())):
        quotes = {s: t[ccy] for s, t in tables.items() if ccy in t}
        fixing, _survivors, _dropped, _spread = compute_fixing(quotes)
        if fixing:
            out[ccy] = fixing
    return out


def movers(now_rates, prev_rates, allowed):
    out = []
    for ccy, now in now_rates.items():
        if ccy not in allowed:
            continue
        prev = prev_rates.get(ccy)
        if not prev or not now:
            continue
        pct = (prev / now - 1) * 100
        if abs(pct) >= MIN_MOVE_PCT:
            out.append({"ccy": ccy, "pct": pct, "rate": now, "prev": prev})
    out.sort(key=lambda m: abs(m["pct"]), reverse=True)
    return out


def fmt_rate(v):
    if v >= 1000:
        return f"{v:,.0f}"
    if v >= 10:
        return f"{v:,.1f}"
    return f"{v:,.3f}".rstrip("0").rstrip(".")


def compose(top, names, when):
    head = f"Currency moves, {when:%d %b %Y}"
    lead = pick_cashtag([m["ccy"] for m in top])
    top = sorted(top, key=lambda m: m["ccy"] != lead)
    rows = []
    for m in top:
        tagged = m["ccy"] == lead
        name = names.get(m["ccy"])
        code = f"${m['ccy']}" if tagged else m["ccy"]
        if name:
            label = f"{hashtag(name) if tagged else name} ({code})"
        else:
            label = code
        sign = "+" if m["pct"] > 0 else ""
        rows.append(f"{label} {fmt_rate(m['rate'])}/USD  {sign}{m['pct']:.1f}%")
    tail = f"Median of {QUORUM}+ independent sources."
    text = head + "\n\n" + "\n".join(rows) + "\n\n" + tail
    while len(text) > TWEET_LIMIT and len(rows) > 1:
        rows.pop()
        text = head + "\n\n" + "\n".join(rows) + "\n\n" + tail
    return text


def _sign(method, url, params, consumer_secret, token_secret):
    base = "&".join([
        method.upper(),
        urllib.parse.quote(url, safe=""),
        urllib.parse.quote("&".join(f"{k}={v}" for k, v in sorted(params.items())), safe=""),
    ])
    key = f"{urllib.parse.quote(consumer_secret, safe='')}&{urllib.parse.quote(token_secret, safe='')}"
    import base64
    return base64.b64encode(
        hmac.new(key.encode(), base.encode(), hashlib.sha1).digest()).decode()


def post_to_x(text, reply_to=None):
    need = ["X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET"]
    missing = [k for k in need if not os.environ.get(k)]
    if missing:
        raise RuntimeError(f"missing credentials: {', '.join(missing)}")
    ck, cs = os.environ["X_API_KEY"], os.environ["X_API_SECRET"]
    tk, ts_ = os.environ["X_ACCESS_TOKEN"], os.environ["X_ACCESS_SECRET"]
    url = "https://api.x.com/2/tweets"
    oauth = {
        "oauth_consumer_key": urllib.parse.quote(ck, safe=""),
        "oauth_nonce": hashlib.md5(str(random.random()).encode()).hexdigest(),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_token": urllib.parse.quote(tk, safe=""),
        "oauth_version": "1.0",
    }
    oauth["oauth_signature"] = urllib.parse.quote(
        _sign("POST", url, oauth, cs, ts_), safe="")
    header = "OAuth " + ", ".join(f'{k}="{v}"' for k, v in sorted(oauth.items()))
    body = {"text": text}
    if reply_to:
        body["reply"] = {"in_reply_to_tweet_id": str(reply_to)}
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Authorization": header, "Content-Type": "application/json",
                 "User-Agent": "banknote-broadcast/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:400]
        except Exception:
            pass
        detail = ""
        try:
            detail = (json.loads(body) or {}).get("detail") or ""
        except ValueError:
            pass
        guess = {
            401: "credentials rejected - check X_API_KEY/X_API_SECRET and "
                 "that the access token pair belongs to the same app",
            403: "authenticated but not permitted - if X gave no reason, the "
                 "app permissions are usually Read-only; set Read and Write, "
                 "then REGENERATE the access token (tokens keep the scope "
                 "they were made under)",
            402: "authentication was fine; X says the account is not entitled "
                 "to post - billing/plan on the developer account, not our code",
            429: "rate limited - back off and retry later",
        }.get(e.code, "unexpected status")
        hint = detail or guess
        log(f"POST /2/tweets -> HTTP {e.code}: {hint}")
        if body:
            log(f"  response: {body}")
        raise RuntimeError(f"x api {e.code}: {hint}") from None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--post", action="store_true", help="publish to X")
    ap.add_argument("--commit", action="store_true",
                    help="save today's snapshot without posting")
    ap.add_argument("--force", action="store_true",
                    help="post even when a move trips the review threshold")
    ap.add_argument("--say", metavar="TEXT",
                    help="publish TEXT verbatim instead of a digest (manual "
                         "announcements; also the only way to prove the "
                         "credentials work without waiting for a real digest)")
    args = ap.parse_args()

    if args.say:
        text = args.say.replace("\\n", "\n").strip()
        if not text:
            log("--say given but empty; nothing to post")
            return 1
        if len(text) > TWEET_LIMIT:
            log(f"FATAL: {len(text)} chars, limit is {TWEET_LIMIT}")
            return 1
        log(f"--- verbatim post ({len(text)} chars) ---")
        for line in text.split("\n"):
            log("  | " + line)
        if not args.post:
            log("dry run - nothing posted")
            return 0
        res = post_to_x(text)
        log(f"posted: {res.get('data', {}).get('id')}")
        return 0

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log("broadcast digest")

    posted = (load_json(STATE_FILE, {}) or {}).get("last_post_date")
    if args.post and posted == today and not args.force:
        log(f"already posted today ({posted}); nothing to do")
        return 0

    tables = read_sources()
    if len(tables) < QUORUM:
        log("FATAL: fewer live sources than quorum; refusing to post")
        return 1

    now_rates = fixings_now(tables)
    names = currency_names()
    state = load_json(STATE_FILE, {})
    prev_rates = state.get("rates") or {}
    log(f"{len(now_rates)} currencies fixed; {len(prev_rates)} in last snapshot")

    def snapshot(posted_date=None, rates=None):
        snap = {
            "taken": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "rates": now_rates if rates is None else rates,
            "ref_source": REF_SOURCE,
            "ref_rates": tables.get(REF_SOURCE) or {},
        }
        if posted_date:
            snap["last_post_date"] = posted_date
        return snap

    if not prev_rates:
        log("no previous snapshot - storing today's and stopping (nothing to compare)")
        if args.commit or args.post:
            write_json_atomic(STATE_FILE, snapshot(), indent=1, sort_keys=True)
        return 0

    ranked = movers(now_rates, prev_rates, covered_ccys())
    if not ranked:
        log("nothing moved more than %.2f%% - no post today" % MIN_MOVE_PCT)
        return 0

    top = ranked[:MAX_LINES]
    for m in top:
        log(f"  {m['ccy']:4s} {m['pct']:+7.2f}%  {fmt_rate(m['prev'])} -> {fmt_rate(m['rate'])}")

    flagged = [m for m in top if abs(m["pct"]) >= REVIEW_PCT]
    text = compose(top, names, datetime.now(timezone.utc))
    log(f"--- post ({len(text)} chars) ---")
    for line in text.split("\n"):
        log("  | " + line)

    if flagged and not args.force:
        log("REVIEW: " + ", ".join(f"{m['ccy']} {m['pct']:+.1f}%" for m in flagged))
        log("a move this size is usually a bad print - check it, then --force")
        if args.post:
            return 2

    posted_now = None
    failed = None
    if args.post:
        try:
            res = post_to_x(text)
            posted_now = today
            log(f"posted: {res.get('data', {}).get('id')}")
        except RuntimeError as e:
            failed = e
            log(f"post FAILED: {e}")
    if args.post or args.commit:
        write_json_atomic(
            STATE_FILE,
            snapshot(posted_now, rates=prev_rates if failed else None),
            indent=1, sort_keys=True)
        log(f"snapshot saved -> {STATE_FILE}"
            + (" (ref_rates only; medians held for the retry)" if failed else ""))
    else:
        log("dry run - nothing posted, snapshot unchanged")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
