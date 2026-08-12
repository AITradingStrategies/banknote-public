import json
import statistics
import sys
import time
import urllib.request
from datetime import datetime, timezone

OUTLIER_PCT = 2.0
QUORUM = 3
DIVERGE_WARN = 1.0

UA = {"User-Agent": "banknote-oracle-prototype/0.1"}


def fetch_json(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def src_erapi():
    d = fetch_json("https://open.er-api.com/v6/latest/USD")
    if d.get("result") != "success":
        raise RuntimeError("er-api result != success")
    return {k.upper(): float(v) for k, v in d["rates"].items() if v}


def src_fawaz():
    d = fetch_json(
        "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/usd.json"
    )
    out = {}
    for k, v in d.get("usd", {}).items():
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f > 0 and len(k) == 3 and k.isalpha():
            out[k.upper()] = f
    return out


def src_frankfurter():
    d = fetch_json("https://api.frankfurter.app/latest?from=USD")
    return {k.upper(): float(v) for k, v in d.get("rates", {}).items() if v}


def src_floatrates():
    d = fetch_json("https://www.floatrates.com/daily/usd.json")
    out = {}
    for k, row in d.items():
        try:
            f = float(row["rate"])
        except (TypeError, ValueError, KeyError):
            continue
        if f > 0:
            out[row.get("code", k).upper()] = f
    return out


def src_moneyconvert():
    d = fetch_json("https://cdn.moneyconvert.net/api/latest.json")
    out = {}
    for k, v in d.get("rates", {}).items():
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f > 0 and len(k) == 3 and k.isalpha():
            out[k.upper()] = f
    return out


def src_fxratesapi():
    d = fetch_json("https://api.fxratesapi.com/latest")
    if d.get("base", "USD").upper() != "USD":
        raise RuntimeError("fxratesapi base != USD")
    out = {}
    for k, v in d.get("rates", {}).items():
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f > 0 and len(k) == 3 and k.isalpha():
            out[k.upper()] = f
    return out


SOURCES = {
    "er-api": src_erapi,
    "fawaz-cdn": src_fawaz,
    "frankfurter-ecb": src_frankfurter,
    "floatrates": src_floatrates,
    "moneyconvert": src_moneyconvert,
    "fxratesapi": src_fxratesapi,
}


def compute_fixing(quotes):
    if not quotes:
        return None, {}, {}, None
    prelim = statistics.median(quotes.values())
    survivors = {
        s: r for s, r in quotes.items()
        if abs(r - prelim) / prelim * 100 <= OUTLIER_PCT
    }
    dropped = {s: r for s, r in quotes.items() if s not in survivors}
    if len(survivors) < QUORUM:
        return None, survivors, dropped, None
    vals = list(survivors.values())
    fixing = statistics.median(vals)
    spread = (max(vals) - min(vals)) / fixing * 100
    return fixing, survivors, dropped, spread


def main():
    t0 = time.time()
    now = datetime.now(timezone.utc).isoformat()
    print(f"Banknote fixing prototype - {now}")
    print(f"params: outlier>{OUTLIER_PCT}% dropped, quorum>={QUORUM}\n")

    tables = {}
    for name, fn in SOURCES.items():
        try:
            tables[name] = fn()
            print(f"  [ok]   {name:16s} {len(tables[name]):4d} currencies")
        except Exception as e:
            print(f"  [FAIL] {name:16s} {e}")
    if len(tables) < QUORUM:
        print("FATAL: fewer live sources than quorum — no fixings possible.")
        sys.exit(1)

    all_ccys = sorted(set().union(*tables.values()))
    fixings, registry = {}, {}
    n_fixed = n_noquorum = 0
    diverg = []

    for ccy in all_ccys:
        quotes = {s: t[ccy] for s, t in tables.items() if ccy in t}
        fixing, survivors, dropped, spread = compute_fixing(quotes)
        entry = {
            "sources_quoting": len(quotes),
            "survivors": len(survivors),
            "dropped_outliers": {s: round(r, 8) for s, r in dropped.items()},
            "max_spread_pct": round(spread, 4) if spread is not None else None,
        }
        if fixing is not None:
            n_fixed += 1
            fixings[ccy] = fixing
            entry["fixing"] = fixing
            entry["flag"] = "REVIEW" if (spread > DIVERGE_WARN or dropped) else "OK"
            if spread > DIVERGE_WARN or dropped:
                diverg.append((ccy, spread, len(quotes), len(dropped)))
        else:
            n_noquorum += 1
            entry["fixing"] = None
            entry["flag"] = "NO_QUORUM"
        registry[ccy] = entry

    print(f"\nuniverse: {len(all_ccys)} currencies seen across sources")
    print(f"  fixed (quorum>={QUORUM}): {n_fixed}")
    print(f"  no quorum:                {n_noquorum}")

    hist = {}
    for e in registry.values():
        hist[e["sources_quoting"]] = hist.get(e["sources_quoting"], 0) + 1
    print("  sources-quoting histogram:",
          {k: hist[k] for k in sorted(hist, reverse=True)})

    diverg.sort(key=lambda x: -(x[1] or 0))
    print(f"\nREVIEW flags (spread>{DIVERGE_WARN}% or outlier dropped): "
          f"{len(diverg)}")
    for ccy, spread, nq, ndrop in diverg[:20]:
        print(f"  {ccy}: spread {spread:.2f}%  ({nq} quoting, {ndrop} dropped)")

    nq_list = [c for c, e in registry.items() if e["flag"] == "NO_QUORUM"]
    if nq_list:
        print(f"\nNO_QUORUM ({len(nq_list)}): {', '.join(nq_list)}")

    out = {
        "generated_utc": now,
        "params": {"outlier_pct": OUTLIER_PCT, "quorum": QUORUM},
        "sources_live": list(tables.keys()),
        "fixings_usd_per_ccy": {k: fixings[k] for k in sorted(fixings)},
    }
    with open("fixings.json", "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)
    with open("registry_draft.json", "w", encoding="utf-8") as f:
        json.dump({"generated_utc": now, "currencies": registry}, f, indent=1)
    print(f"\nwrote fixings.json + registry_draft.json  ({time.time()-t0:.1f}s)")


if __name__ == "__main__":
    main()
