import argparse
import json
import os
import random
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _util import write_json_atomic
from run_broadcast import (
    COUNTRIES, TWEET_LIMIT, fmt_rate, hashtag, load_json, log, post_to_x,
)

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(HERE, ".."))
ANALYSIS = os.path.join(HERE, "state", "analysis.json")
ARCHIVE = os.path.join(HERE, "archive")
NEWS_DIR = (os.environ.get("BANKNOTE_NEWS_DIR")
            or os.path.join(REPO, "web", "public", "news"))
STATE_FILE = os.path.join(HERE, "state", "commentary_state.json")

MODEL = "claude-sonnet-5"
SLOT_MINUTES = 30
MIN_POST_GAP_MIN = 25
REPEAT_COOLDOWN_DAYS = 2
NEWS_MAX_AGE_H = 72
NEWS_FRESH_H = 24
NEWS_MAX_ITEMS = 6

ANCHORS = {"NGN": 6, "ARS": 10, "TRY": 13, "PHP": 17, "PKR": 20}

MAJORS = {
    "USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD", "CNY", "HKD",
    "SGD", "SEK", "NOK", "DKK", "KRW", "TWD", "ILS", "PLN", "CZK", "HUF",
}

LANGUAGES = {"ARS": "Spanish", "TRY": "Turkish"}

SIGNAL_WEIGHT = {"level": 3, "divergence": 2, "move": 2, "streak": 1}
SCOPE_BONUS = {"window": 2, "365d": 1}

BANNED = [
    "testnet",
    "buy ", "sell ", "invest", "should ", "we expect", "forecast", "prediction",
    "guarantee", "will rise", "will fall", "opportunity",
]


def country_index():
    d = load_json(COUNTRIES, {})
    ccys, display = d.get("ccys") or [], d.get("display") or []
    owners = {}
    for i, (ccy, name) in enumerate(zip(ccys, display)):
        owners.setdefault(ccy, []).append((i, name))
    return {c: v[0] for c, v in owners.items() if len(v) == 1}


def flag_emoji(cca2):
    c = (cca2 or "").strip().upper()
    if len(c) != 2 or not c.isalpha():
        return ""
    return "".join(chr(0x1F1E6 + ord(ch) - ord("A")) for ch in c)


def cca2_of(cid):
    d = load_json(COUNTRIES, {})
    codes = d.get("cca2") or []
    return codes[cid] if 0 <= cid < len(codes) else ""


def latest_fixing(ccy):
    if not os.path.isdir(ARCHIVE):
        return None
    for day in sorted(os.listdir(ARCHIVE), reverse=True):
        path = os.path.join(ARCHIVE, day, "fixings.json")
        if not os.path.exists(path):
            continue
        fx = load_json(path, {})
        rates = fx.get("rates")
        if isinstance(rates, list):
            rates = dict(zip(fx.get("ccys") or [], rates))
        if not isinstance(rates, dict) or ccy not in rates:
            continue
        try:
            return {"rate": float(rates[ccy]) / 1e18, "day": fx.get("day") or day}
        except (TypeError, ValueError):
            continue
    return None


def recent_news(country_id, now_ts):
    path = os.path.join(NEWS_DIR, f"{country_id}.json")
    d = load_json(path, {})
    out = []
    for item in (d.get("items") or []):
        ts = item.get("ts")
        if ts and (now_ts - ts) > NEWS_MAX_AGE_H * 3600:
            continue
        title = (item.get("t") or "").strip()
        if title:
            out.append({"headline": title, "source": item.get("d") or item.get("dom") or ""})
        if len(out) >= NEWS_MAX_ITEMS:
            break
    return out


def score(entry):
    total = 0
    for s in entry.get("signals") or []:
        total += SIGNAL_WEIGHT.get(s["type"], 1)
        if s["type"] == "level":
            total += SCOPE_BONUS.get(s.get("scope"), 0)
    if entry["ccy"] in ANCHORS:
        total += 1
    return total


def fresh_news_ccys(index, now_ts):
    out = set()
    for ccy, (cid, _) in index.items():
        d = load_json(os.path.join(NEWS_DIR, f"{cid}.json"), {})
        for item in (d.get("items") or [])[:NEWS_MAX_ITEMS]:
            ts = item.get("ts")
            if ts and (now_ts - ts) <= NEWS_FRESH_H * 3600:
                out.add(ccy)
                break
    return out


def in_scope(entry, index):
    ccy = entry.get("ccy")
    if not ccy or ccy not in index or entry.get("error"):
        return False
    return not entry.get("stateless") and ccy not in MAJORS


def eligible(entry, index, state, now_ts):
    if not in_scope(entry, index):
        return False
    last = (state.get("recent") or {}).get(entry["ccy"], 0)
    return (now_ts - last) > REPEAT_COOLDOWN_DAYS * 86400


def movement_rank(entry):
    vol = (entry.get("volatility") or {}).get("sigma_pct") or 0.1
    ch = entry.get("changes") or {}
    best = 0.0
    for key, scale in (("7d", 2.6), ("30d", 5.5)):
        pct = (ch.get(key) or {}).get("pct")
        if pct:
            best = max(best, abs(pct) / (vol * scale))
    return best


def pick(entries, index, state, now_ts, now):
    posted_today = set(state.get("anchors_today") or [])
    live = {c: e for c, e in entries.items() if eligible(e, index, state, now_ts)}

    due = [c for c, hour in ANCHORS.items()
           if c not in posted_today and now.hour >= hour and c in entries
           and c in index and not entries[c].get("error")]
    if due:
        due.sort(key=lambda c: ANCHORS[c])
        return "anchor", entries[due[0]]

    movers = sorted(live.values(), key=movement_rank, reverse=True)
    if movers and movement_rank(movers[0]) >= 1.0:
        return "movement", movers[0]

    fresh = sorted(fresh_news_ccys(index, now_ts) & set(live))
    if fresh:
        return "news", live[fresh[0]]

    if live:
        seed = f"{now:%Y-%m-%d}-{now.hour}-{now.minute // SLOT_MINUTES}"
        return "random", live[random.Random(seed).choice(sorted(live))]

    seen = state.get("recent") or {}
    stale = sorted((c for c, e in entries.items() if in_scope(e, index)),
                   key=lambda c: seen.get(c, 0))
    if not stale:
        return None, None
    return "repeat", entries[stale[0]]


def phrase_context(entry):
    ch = entry.get("changes") or {}
    ytd = (ch.get("ytd") or {}).get("pct")
    year = (ch.get("365d") or {}).get("pct")
    out = []
    week = (ch.get("7d") or {}).get("pct")
    if week is not None:
        out.append("little changed over the past week" if abs(week) < 0.5
                   else f"{'stronger' if week > 0 else 'weaker'} by {abs(week):.1f}% over the past week")
    month = (ch.get("30d") or {}).get("pct")
    if month is not None and abs(month) >= 0.5:
        out.append(f"{'up' if month > 0 else 'down'} {abs(month):.1f}% over 30 days")
    said_year = False
    for pct, phrase in ((ytd, "this year"), (year, "over the past year")):
        if pct is not None and abs(pct) >= 0.5:
            out.append(f"{'stronger' if pct > 0 else 'weaker'} by {abs(pct):.1f}% {phrase}")
            said_year = phrase == "over the past year"
            break
    if (not said_year and year is not None and ytd is not None
            and abs(year) >= 3.0 and abs(year) >= 1.5 * abs(ytd)):
        out.append(f"{'stronger' if year > 0 else 'weaker'} by {abs(year):.1f}% "
                   f"over the past year")
    if out:
        return out
    win = entry.get("window_change_pct")
    if win is not None and abs(win) >= 0.5:
        out.append(f"{'stronger' if win > 0 else 'weaker'} by {abs(win):.1f}% "
                   f"since {entry['window']['first']}")
    return out


def phrase_signals(entry):
    out = []
    for s in entry.get("signals") or []:
        if s["type"] == "move":
            d = s.get("direction") or "moved"
            when = ("the largest daily move in the tracked window"
                    if s["largest_in_window"]
                    else f"its largest daily move in {s['days_since_larger']} days")
            out.append(f"{d} {s['pct']:.2f}% at the latest fixing, {when}")
        elif s["type"] == "level":
            scope = s.get("scope")
            when = (f"since tracking began in {s['since']}" if scope == "window"
                    else f"in {scope.replace('d', ' days')}")
            out.append(f"its {s['extreme']} level {when}")
        elif s["type"] == "streak":
            out.append(f"{s['days']} consecutive fixings {s['direction']}")
        elif s["type"] == "divergence":
            out.append(
                f"the {s['sources']} independent sources behind the fixing disagree by "
                f"{s['spread_pct']:.2f}%, against a typical {s['typical_pct']:.2f}% "
                f"for this currency")
    return out


def ordinal(n):
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }".replace(" ", "")


def phrase_cross(entry):
    cross = entry.get("cross") or {}
    ch = entry.get("changes") or {}
    out = []
    for key, window, label in (("change_30d", "30d", "this month"),
                               ("change_ytd", "ytd", "this year"),
                               ("change_365d", "365d", "over the past year")):
        c = cross.get(key)
        if not c:
            continue
        r, n = c["rank"], c["of"]
        if r <= 5:
            out.append(f"the {ordinal(r)} strongest of {n} currencies {label}")
        elif r > n - 5:
            out.append(f"the {ordinal(n - r + 1)} weakest of {n} currencies {label}")
        g = cross.get(f"gainers_{window}")
        pct = (ch.get(window) or {}).get("pct")
        if g and pct and pct > 0 and g["gained"] <= 20:
            out.append(f"one of only {g['gained']} of {g['of']} currencies to "
                       f"strengthen against the dollar {label}")

    st = cross.get("steadiness")
    if st and st["rank"] <= 8:
        out.append(f"the {ordinal(st['rank'])} steadiest of {st['of']} currencies tracked")
    elif st and st["rank"] > st["of"] - 8:
        out.append(f"the {ordinal(st['of'] - st['rank'] + 1)} most volatile of "
                   f"{st['of']} currencies tracked")
    return out


def phrase_history(entry):
    out = []
    dist = entry.get("distance") or {}
    off = dist.get("off_weakest_pct")
    if off is not None and off >= 2.0:
        out.append(f"{off:.1f}% above its weakest point of the past year")
    elif off is not None and off <= 0.5:
        out.append("sitting at its weakest point of the past year")
    above = dist.get("above_strongest_pct")
    if above is not None and above >= 5.0:
        out.append(f"{above:.1f}% below its strongest point of the past year")

    reg = entry.get("regime") or {}
    ratio = reg.get("ratio")
    if ratio is not None and ratio >= 1.5:
        out.append(f"moving {ratio:.1f} times as much per day as it has over the past year")
    elif ratio is not None and ratio <= 0.6:
        out.append("moving less each day than it has over the past year")

    q = entry.get("quiet_run") or {}
    if q.get("days"):
        out.append(f"{q['days']} days running without a move of {q['band_pct']}%")

    ec = entry.get("echo") or {}
    for key, label in (("year_ago", "a year ago"), ("jan1", "at the start of the year")):
        v = ec.get(key)
        if v:
            out.append(f"{fmt_rate(v['rate'])}/USD {label}")
    return out


def phrase_angles(entry):
    ch = entry.get("changes") or {}
    vol = entry.get("volatility") or {}
    act = entry.get("activity") or {}
    ext = entry.get("extremes") or {}
    wins = entry.get("extreme_windows") or {}
    out = []

    q = (ch.get("90d") or {}).get("pct")
    if q is not None and abs(q) >= 1.0:
        out.append(f"{'stronger' if q > 0 else 'weaker'} by {abs(q):.1f}% over 90 days")

    for label, key in (("30 days", "30d"), ("the past year", "365d")):
        w = wins.get(key) or {}
        rng = w.get("range_pct")
        if rng is not None and rng >= 1.0:
            out.append(f"has traded in a {rng:.1f}% band over {label}")
        if w.get("weakest"):
            out.append(f"at its weakest point of {label}")
        elif w.get("strongest"):
            out.append(f"at its strongest point of {label}")

    sig = vol.get("sigma_pct")
    if sig:
        out.append(f"moves {sig:.2f}% on a typical day")

    frac = act.get("active_frac")
    if frac is not None and act.get("is_active"):
        out.append(f"the rate changes on {frac * 100:.0f}% of days")

    for phrase, key in (("weaker", "days_since_weaker"), ("stronger", "days_since_stronger")):
        n = ext.get(key)
        if isinstance(n, int) and n >= 20:
            out.append(f"has not been {phrase} in {n} days")
    return out + phrase_cross(entry) + phrase_history(entry)


def build_facts(entry, cid, name, fixing, news):
    ch = entry.get("changes") or {}
    return {
        "ccy": entry["ccy"],
        "country": name,
        "hashtag": hashtag(name),
        "flag": flag_emoji(cca2_of(cid)),
        "language": LANGUAGES.get(entry["ccy"], "English"),
        "rate": fmt_rate(fixing["rate"]) if fixing else None,
        "rate_day": fixing["day"] if fixing else None,
        "claims": phrase_signals(entry) or phrase_context(entry),
        "angles": phrase_angles(entry),
        "moved": bool(entry.get("signals")),
        "context": {
            "30d_pct": (ch.get("30d") or {}).get("pct"),
            "ytd_pct": (ch.get("ytd") or {}).get("pct"),
            "365d_pct": (ch.get("365d") or {}).get("pct"),
        },
        "news": news,
    }


NUM = re.compile(r"\d+(?:[.,]\d+)*")


def numbers_in(text):
    out = []
    for tok in NUM.findall(text):
        t = tok
        if "," in t and "." in t:
            t = t.replace(".", "").replace(",", ".") if t.rfind(",") > t.rfind(".") \
                else t.replace(",", "")
        elif "," in t:
            t = t.replace(",", ".") if len(t.split(",")[-1]) != 3 else t.replace(",", "")
        else:
            parts = t.split(".")
            if len(parts) > 2 or (len(parts) == 2 and len(parts[-1]) == 3 and len(parts[0]) <= 3
                                  and parts[0] != "0"):
                t = t.replace(".", "")
        try:
            out.append(float(t))
        except ValueError:
            continue
    return out


def allowed_numbers(facts):
    vals = set()

    def add(v):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return
        vals.add(abs(f))

    if facts.get("rate"):
        add(facts["rate"].replace(",", ""))
    for c in list(facts["claims"]) + list(facts.get("angles") or []):
        for n in numbers_in(c):
            add(n)
    for v in facts["context"].values():
        add(v)
    for part in re.findall(r"\d+", facts["context"].get("window_start") or ""):
        add(part)
    for part in re.findall(r"\d+", facts.get("rate_day") or ""):
        add(part)
    return vals


def traceable(value, allowed):
    v = abs(value)
    for a in allowed:
        if a == v:
            return True
        for d in range(0, 5):
            if round(a, d) == v:
                return True
        if a > 0 and abs(v - a) / a < 0.005:
            return True
    return False


NO_REPORTING = re.compile(
    r"\b(no|nothing|without any|there is no)\b[^.]{0,40}\b"
    r"(public )?(report|reporting|reports|news|coverage|explanation|"
    r"informe|noticia|cobertura)\w*"
    r"|\b(haber|rapor|a[cç][ıi]klama)\w*[^.]{0,40}\b"
    r"(yok|bulunmuyor|bulunmamaktad[ıi]r)", re.I)

CAUSAL = re.compile(r"\b(because|due to|owing to|after|amid|following|driven by|"
                    r"on the back of|prompted by|debido a|tras|por|nedeniyle|sonras)\b")


def validate(drafted, facts):
    text = (drafted or {}).get("post") or ""
    cited = (drafted or {}).get("cited_headline")
    problems = []
    if not text or not text.strip():
        return ["empty"]
    if len(text) > TWEET_LIMIT:
        problems.append(f"{len(text)} chars over the {TWEET_LIMIT} limit")
    if text.count("$") > 1:
        problems.append("more than one cashtag (X rejects it)")
    if text.count("#") > 1:
        problems.append("more than one hashtag")

    allowed = allowed_numbers(facts)
    for n in numbers_in(text):
        if not traceable(n, allowed):
            problems.append(f"number {n} is not traceable to the supplied facts")

    low = text.lower()
    for word in BANNED:
        if word in low:
            problems.append(f"banned phrase: {word.strip()!r}")

    supplied = {h["headline"] for h in facts["news"]}
    if facts["news"] and ((drafted or {}).get("claims_no_reporting")
                          or NO_REPORTING.search(low)):
        problems.append("claims no reporting exists while headlines were supplied")
    if CAUSAL.search(low):
        if not cited:
            problems.append("asserts a cause without citing a supplied headline")
        elif cited not in supplied:
            problems.append(f"cites a headline we did not supply: {cited[:60]!r}")
    elif cited and cited not in supplied:
        problems.append(f"cites a headline we did not supply: {cited[:60]!r}")
    return problems


SYSTEM = """You write for a wire service that covers exchange rates, including \
for countries no financial press reports on.

Voice: a news anchor reading a bulletin. Third person. Declarative. Lead with \
what happened, then the context that makes it mean something, then stop.

YOU ARE GIVEN MORE MATERIAL THAN FITS. `claims` is what stands out today;
`angles` is everything else true about this currency - the band it has held,
how much it moves on a typical day, how long since it was last this weak, how
often it moves at all. Pick the two or three that make the most interesting
sentence about THIS currency today and leave the rest. Do not recite the list.

DO NOT FILE THE SAME POST TWICE. A reader sees several of these a day, and a
run of interchangeable sentences is the one failure that matters here - it is
the whole reason a writer does this rather than a form letter. Vary what you
open on and how you build the sentence. Some days the level is the story, some
days the streak, some days how quiet it has been.

HEADLINES, IF ANY EARN IT. You may be given recent stories from the country.
Use one only where it bears on the currency itself - the economy, prices,
trade, public finances, monetary or fiscal policy. Everything else is out:
crime, sport, accidents, human interest. Most days none of them will qualify
and the rate stands on its own; that is normal and preferable to a forced
connection. Name the one you used in cited_headline.

These show the range wanted. Do not copy their facts - they are not about any
currency you will be given:
  "The kwacha held at 27.4 to the dollar, a fifth straight session inside a
   band it has kept all month."
  "Three months of steady decline have left the som 4.1% weaker, though the
   past week has been its quietest since March."
  "At 611 to the dollar the ariary is where it started the year, having moved
   on only a third of trading days since."

Absolute rules:
- Every number you write must appear in the facts you are given. You may round \
(0.404 -> 0.4). You may never compute, estimate, or recall a figure.
- Causation is the one thing you may not infer. Putting a headline beside a \
rate is context and is welcome. Saying the rate moved BECAUSE of it is a \
claim, and you may write it only where the headline itself says so. When you \
are unsure, place the two facts side by side and let the reader join them.
- Never supply a cause, a policy, or an event from your own knowledge of the \
country. If it is not in the facts you were given, it does not exist.
- Do NOT write that no reporting exists whenever any headline was supplied - \
that is a claim about the world, and it is false.
- Only discuss a cause when there is a move to explain. A rate that is little \
changed needs no explanation and no remark about the absence of one.
- No forecasts. No advice. No opinion about whether this is good or bad.
- No first person, no emoji, no questions, no addressing the reader, no \
hashtags or cashtags beyond the single pair supplied to you.
- Write in the requested language, for a reader in that country.

Under 260 characters. Put the supplied hashtag and the cashtag at the end."""


def compose_with_model(facts):
    try:
        import anthropic
    except ImportError:
        log("anthropic SDK not installed - no post this slot")
        return None

    schema = {
        "type": "object",
        "properties": {
            "post": {"type": "string", "description": "the post, in the requested language"},
            "back_translation": {
                "type": "string",
                "description": "literal English translation of the post, for the log. "
                               "If the post is already English, repeat it.",
            },
            "cited_headline": {
                "type": ["string", "null"],
                "description": "the supplied headline attributed, or null if none",
            },
            "claims_no_reporting": {
                "type": "boolean",
                "description": "true if the post says, in any words or any "
                               "language, that no reporting/news explains the "
                               "move. Answer for what the post means, not for "
                               "which words it uses.",
            },
        },
        "required": ["post", "back_translation", "cited_headline",
                     "claims_no_reporting"],
        "additionalProperties": False,
    }
    try:
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=MODEL,
            max_tokens=2000,
            system=SYSTEM,
            output_config={"effort": "low",
                           "format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": json.dumps(facts, ensure_ascii=False)}],
        )
    except Exception as e:
        log(f"model call failed ({type(e).__name__}: {e})")
        return None

    if resp.stop_reason == "refusal":
        log("model declined this request")
        return None
    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        return json.loads(text)
    except ValueError:
        log("model output was not the requested JSON")
        return None


def with_flag(text, facts):
    flag = facts.get("flag")
    if not flag or flag in text:
        return text
    out = f"{text} {flag}"
    return out if len(out) <= TWEET_LIMIT else text


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--post", action="store_true", help="publish to X")
    ap.add_argument("--commit", action="store_true", help="save state without posting")
    ap.add_argument("--ccy", help="force a currency instead of picking one")
    args = ap.parse_args()

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now = datetime.now(timezone.utc)
    now_ts = int(now.timestamp())

    payload = load_json(ANALYSIS, {})
    entries = (payload.get("currencies") or {})
    if not entries:
        log("no analysis - run run_analysis.py --all first")
        return 1

    state = load_json(STATE_FILE, {})
    if state.get("date") != today:
        state = {"date": today, "anchors_today": [], "recent": state.get("recent", {}),
                 "last_post": state.get("last_post", 0)}

    if args.post and not args.ccy:
        since = now_ts - int(state.get("last_post") or 0)
        if since < MIN_POST_GAP_MIN * 60:
            log(f"last post was {since // 60}m ago, under the {MIN_POST_GAP_MIN}m "
                f"minimum - skipping this slot")
            return 0

    index = country_index()
    if args.ccy:
        entry = entries.get(args.ccy.upper())
        kind = "forced"
        if not entry:
            log(f"{args.ccy} is not in the analysis")
            return 1
    else:
        kind, entry = pick(entries, index, state, now_ts, now)
        if not entry:
            log("nothing eligible this slot")
            return 0

    ccy = entry["ccy"]
    cid, name = index[ccy]
    news = recent_news(cid, now_ts)
    facts = build_facts(entry, cid, name, latest_fixing(ccy), news)

    log(f"slot: {kind} | {ccy} ({name}) score={score(entry)} "
        f"signals={[s['type'] for s in entry['signals']]} news={len(news)}")
    for c in facts["claims"]:
        log(f"  claim: {c}")

    drafted = None
    for attempt in (1, 2):
        drafted = compose_with_model(facts)
        if not drafted:
            continue
        drafted["post"] = with_flag(drafted["post"], facts)
        problems = validate(drafted, facts)
        if not problems:
            break
        for p in problems:
            log(f"  REJECTED (attempt {attempt}): {p}")
        drafted = None
    if not drafted:
        log(f"no acceptable post for {ccy} this slot - skipping")
        return 0
    source = "model"
    log(f"--- post ({source}, {len(drafted['post'])} chars, {facts['language']}) ---")
    for line in drafted["post"].split("\n"):
        log("  | " + line)
    if facts["language"] != "English":
        log("  --- back-translation (what actually went out) ---")
        for line in (drafted.get("back_translation") or "").split("\n"):
            log("  | " + line)

    if args.post:
        post_to_x(drafted["post"])
        if kind == "anchor":
            state.setdefault("anchors_today", []).append(ccy)
        state.setdefault("recent", {})[ccy] = now_ts
        state["last_post"] = now_ts
        log(f"posted: {ccy}")
    if args.post or args.commit:
        write_json_atomic(STATE_FILE, state, indent=1, sort_keys=True)
        log(f"state saved -> {STATE_FILE}")
    else:
        log("dry run - nothing posted, state unchanged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
