import argparse
import base64
import hashlib
import hmac
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _util import write_json_atomic
from listen_queries import build, load, local_terms, topic_terms
from run_broadcast import _sign, log

HERE = os.path.dirname(os.path.abspath(__file__))
OUTDIR = os.path.join(HERE, "listen")
ENDPOINT = "https://api.x.com/2/tweets/counts/recent"

PAUSE_S = 1.5
RETRIES = 3


def auth_header(url, params):
    bearer = os.environ.get("X_BEARER_TOKEN")
    if bearer:
        return {"Authorization": f"Bearer {bearer}"}, "bearer"

    need = ["X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET"]
    if any(not os.environ.get(k) for k in need):
        raise SystemExit(
            "no credentials: set X_BEARER_TOKEN (preferred for counts), or the "
            "four OAuth 1.0a X_* values")
    ck, cs = os.environ["X_API_KEY"], os.environ["X_API_SECRET"]
    tk, ts_ = os.environ["X_ACCESS_TOKEN"], os.environ["X_ACCESS_SECRET"]
    oauth = {
        "oauth_consumer_key": urllib.parse.quote(ck, safe=""),
        "oauth_nonce": hashlib.md5(str(random.random()).encode()).hexdigest(),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_token": urllib.parse.quote(tk, safe=""),
        "oauth_version": "1.0",
    }
    signing = dict(oauth)
    for k, v in params.items():
        signing[urllib.parse.quote(k, safe="")] = urllib.parse.quote(v, safe="")
    oauth["oauth_signature"] = urllib.parse.quote(
        _sign("GET", url, signing, cs, ts_), safe="")
    header = "OAuth " + ", ".join(f'{k}="{v}"' for k, v in sorted(oauth.items()))
    return {"Authorization": header}, "oauth1"


def counts(query, granularity="hour"):
    params = {"query": query, "granularity": granularity}
    headers, _ = auth_header(ENDPOINT, params)
    url = ENDPOINT + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=headers)
    for attempt in range(1, RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                body = json.loads(resp.read().decode())
            buckets = [{"start": b.get("start"), "n": b.get("tweet_count", 0)}
                       for b in (body.get("data") or [])]
            total = (body.get("meta") or {}).get("total_tweet_count")
            if total is None:
                total = sum(b["n"] for b in buckets)
            return total, buckets
        except urllib.error.HTTPError as e:
            detail = e.read().decode()[:200]
            if e.code == 429 and attempt < RETRIES:
                wait = 60 * attempt
                log(f"    rate limited, waiting {wait}s")
                time.sleep(wait)
                continue
            raise SystemExit(f"HTTP {e.code}: {detail}")
        except urllib.error.URLError as e:
            if attempt < RETRIES:
                time.sleep(5 * attempt)
                continue
            raise SystemExit(f"network: {e}")
    raise SystemExit("unreachable")


def rows_to_requests(rows, kinds):
    out = []
    for r in rows:
        for kind in kinds:
            q = r.get(kind)
            if q:
                out.append((r["ccy"], kind, q, r))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="print the requests and call nothing")
    ap.add_argument("--limit", type=int, help="stop after N requests (use 1 first)")
    ap.add_argument("--ccy", help="comma-separated currencies only")
    ap.add_argument("--kinds", default="tight,country,loose",
                    help="which query variants to run")
    args = ap.parse_args()

    info, fix, owners = load()
    languages = {}
    try:
        from run_commentary import LANGUAGES
        languages = LANGUAGES
    except Exception:
        pass
    rows = build(info, fix, owners, languages, local_terms(), topic_terms())

    if args.ccy:
        want = {c.strip().upper() for c in args.ccy.split(",")}
        rows = [r for r in rows if r["ccy"] in want]
    kinds = [k.strip() for k in args.kinds.split(",") if k.strip()]
    reqs = rows_to_requests(rows, kinds)
    if args.limit:
        reqs = reqs[:args.limit]

    log(f"{len(reqs)} requests "
        f"(~${len(reqs) * 0.005:.2f} at $0.005 each, if billed per request)")
    if args.dry_run:
        for ccy, kind, q, _ in reqs:
            print(f"  {ccy:4s} {kind:8s} {q}")
        log("dry run - nothing called, nothing billed")
        return 0

    started = datetime.now(timezone.utc)
    results, failed = [], 0
    for i, (ccy, kind, q, row) in enumerate(reqs, 1):
        try:
            total, buckets = counts(q)
        except SystemExit as e:
            log(f"  [{i}/{len(reqs)}] {ccy} {kind}: FAILED {e}")
            failed += 1
            continue
        results.append({
            "ccy": ccy,
            "kind": kind,
            "query": q,
            "total": total,
            "buckets": buckets,
            "language": row.get("language"),
            "local_confidence": row.get("local_confidence"),
            "caveats": row.get("needs_hand_authoring") or [],
        })
        log(f"  [{i}/{len(reqs)}] {ccy:4s} {kind:8s} {total:>7,}")
        time.sleep(PAUSE_S)

    if not results:
        log(f"no results ({failed} failed) - writing nothing")
        return 1

    os.makedirs(OUTDIR, exist_ok=True)
    path = os.path.join(OUTDIR, f"{started:%Y-%m-%d}.json")
    write_json_atomic(path, {
        "swept_at": started.isoformat(),
        "endpoint": ENDPOINT,
        "granularity": "hour",
        "requests": len(reqs),
        "failed": failed,
        "results": results,
    }, indent=1, sort_keys=True)
    log(f"wrote {path} ({len(results)} results, {failed} failed)")

    ranked = sorted(results, key=lambda r: -(r["total"] or 0))[:15]
    log("busiest queries:")
    for r in ranked:
        flag = " (low-confidence terms)" if r["local_confidence"] == "low" else ""
        log(f"  {r['total']:>8,}  {r['ccy']:4s} {r['kind']:8s}{flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
