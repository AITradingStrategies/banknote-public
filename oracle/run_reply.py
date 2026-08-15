import argparse
import datetime as dt
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from listen_queries import build, load, local_terms, topic_terms
from reply_filter import codes_in, screen
from run_broadcast import log, post_to_x
from run_commentary import (ANALYSIS, LANGUAGES, MODEL, TWEET_LIMIT,
                            URL, latest_fixing, load_json, number_tokens,
                            traceable)
from x_search import Fatal, search

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "state", "reply_state.json")

RAMP = [(0, 4), (7, 8), (14, 12)]

MAX_PER_CCY_PER_DAY = 2

MAX_POST_AGE_MIN = 180

MAX_FIXING_AGE_H = 30

CCYS_PER_RUN = 3
POSTS_PER_SEARCH = 10
USD_PER_POST = 0.005

TIERS = {
    "frontier": ["NGN", "GHS", "KES", "EGP", "PKR", "BDT", "VND", "IDR",
                 "PHP", "INR", "ZAR", "ARS", "TRY", "MXN", "COP"],
    "major": ["JPY", "EUR", "GBP", "CAD", "CHF", "CNY"],
}

BANNED = [
    "actually", "incorrect", "wrong", "fyi", "correction", "no,", "nope",
    "banknote", "check out", "follow", "our platform", "trade ", "buy ",
    "click", "join", "sign up", "dm ",
]

SYSTEM = """You write one line of fact under somebody else's post, as a \
currency data account.

Rules, all of them hard:
- ONE sentence. Under 120 characters. No greeting, no sign-off, no emoji.
- Write it in the LANGUAGE GIVEN, naturally, as a person from that country \
would write it.
- Use ONLY the numbers supplied. Never round them differently, never add one.
- State the rate. The RATE is from the daily fixing, and `fixing_age` says \
whether that fixing is from today or yesterday: say the one given, never the \
other. If a move is given, state \
it too; the MOVE is day on day - never say it happened "at the fixing",
because it did not. **If the move is null there was no move worth reporting: \
give the rate alone and say nothing about direction.**
- If `cross` is supplied ("one" X "equals" N "of" Y), the post compared those \
two currencies and the cross comes from the same fixings: state it, e.g. \
"1 KWD is 4,400 NGN at the latest fixing", with or without the USD rate.
- `pair_move_pct` is the move of the PAIR (local currency per dollar), signed. \
`currency_direction` is the same fact said about the currency. They are \
opposites and both are given to you already correct: never work one out from \
the other, and never flip a sign.
- Do NOT correct, contradict, congratulate, agree, or address the person. Do \
not use "actually", "in fact", "no". You are adding a number to a \
conversation, not answering back.
- `recent_replies`, when present, are this account's latest replies. The \
facts may be identical - the fixing is daily - but the sentence must not be: \
never produce a reply matching one of them, and build the sentence \
differently. Same numbers, different sentence.
- No hashtags, no links, no mention of who you are.

Good, in English: "USD/NGN is 1,610 at today's fixing, down 0.4% on the day."
Bad: "Actually the naira is at 1,610 - you're out of date."
"""

SCHEMA = {
    "type": "object",
    "properties": {"reply": {"type": "string"}},
    "required": ["reply"],
    "additionalProperties": False,
}

X_LANGS = {
    "en": "English", "es": "Spanish", "pt": "Portuguese", "fr": "French",
    "de": "German", "it": "Italian", "nl": "Dutch", "tr": "Turkish",
    "ar": "Arabic", "fa": "Persian", "ur": "Urdu", "hi": "Hindi",
    "bn": "Bengali", "ta": "Tamil", "ne": "Nepali", "si": "Sinhala",
    "th": "Thai", "vi": "Vietnamese", "id": "Indonesian", "in": "Indonesian",
    "ms": "Malay", "tl": "Filipino", "ja": "Japanese", "ko": "Korean",
    "zh": "Chinese", "ru": "Russian", "uk": "Ukrainian", "pl": "Polish",
    "cs": "Czech", "hu": "Hungarian", "ro": "Romanian", "sw": "Swahili",
    "am": "Amharic", "my": "Burmese", "km": "Khmer", "lo": "Lao",
    "el": "Greek", "he": "Hebrew", "iw": "Hebrew", "da": "Danish",
    "sv": "Swedish", "no": "Norwegian",
}


def reply_language(facts, post_lang):
    return X_LANGS.get((post_lang or "").lower()) or facts["language"]


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def load_state():
    try:
        with open(STATE_FILE) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"first_day": None, "replied": [], "accounts": {}, "days": {}}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    state["replied"] = state["replied"][-500:]
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=1, sort_keys=True)
    os.replace(tmp, STATE_FILE)


def daily_cap(state, today):
    first = state.get("first_day")
    if not first:
        return RAMP[0][1]
    age = (dt.date.fromisoformat(today) - dt.date.fromisoformat(first)).days
    cap = RAMP[0][1]
    for after, n in RAMP:
        if age >= after:
            cap = n
    return cap


def age_minutes(created_at):
    if not created_at:
        return None
    try:
        when = dt.datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (now_utc() - when).total_seconds() / 60


def fixing_for(ccy):
    fx = latest_fixing(ccy)
    if not fx:
        return None
    try:
        day = dt.date.fromisoformat(fx["day"])
    except (KeyError, TypeError, ValueError):
        return None
    hours = (now_utc().date() - day).days * 24
    if hours > MAX_FIXING_AGE_H:
        return None
    return fx


def fmt(v):
    if v >= 1000:
        return f"{v:,.0f}"
    if v >= 10:
        return f"{v:,.2f}"
    return f"{v:.4f}".rstrip("0").rstrip(".")


def cross_facts(ccy, fx, post_text):
    others = codes_in(post_text) - {ccy, "USD"}
    if len(others) != 1:
        return None
    other = next(iter(others))
    ofx = fixing_for(other)
    if not ofx or ofx["day"] != fx["day"] or not ofx.get("rate"):
        return None
    if fx["rate"] >= ofx["rate"]:
        return {"one": other, "equals": fmt(fx["rate"] / ofx["rate"]), "of": ccy}
    return {"one": ccy, "equals": fmt(ofx["rate"] / fx["rate"]), "of": other}


def facts_for(ccy, fx, entries, post_text=""):
    entry = entries.get(ccy) or {}
    move = ((entry.get("changes") or {}).get("1d") or {}).get("pct")
    if move is not None and abs(move) < 0.1:
        move = None
    return {
        "pair": f"USD/{ccy}",
        "rate": fmt(fx["rate"]),
        "day": fx["day"],
        "pair_move_pct": None if move is None else f"{move:+.2f}",
        "currency_direction": None if move is None else (
            "weaker" if move > 0 else "stronger"),
        "language": LANGUAGES.get(ccy, "English"),
        "country": entry.get("country") or ccy,
        "fixing_age": ("today" if fx["day"] == now_utc().date().isoformat()
                       else "yesterday"),
        "cross": cross_facts(ccy, fx, post_text),
    }


def compose(facts, lang, recent=()):
    import anthropic
    ask = {
        "language": lang,
        "pair": facts["pair"],
        "rate_at_fixing": facts["rate"],
        "fixing_age": facts["fixing_age"],
        "pair_move_pct_day_on_day": facts["pair_move_pct"],
        "currency_direction": facts["currency_direction"],
    }
    if facts.get("cross"):
        ask["cross"] = facts["cross"]
    if recent:
        ask["recent_replies"] = list(recent)[-4:]
    resp = anthropic.Anthropic().messages.create(
        model=MODEL,
        max_tokens=400,
        system=SYSTEM,
        output_config={"effort": "low",
                       "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content":
                   f"Language: {lang}\nFacts: {json.dumps(ask, ensure_ascii=False)}"}],
    )
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            return json.loads(block.text)["reply"].strip()
    return ""


def validate(text, facts, recent=()):
    problems = []
    if not text.strip():
        return ["empty"]
    fold = " ".join(text.split()).lower()
    if any(fold == " ".join(r.split()).lower() for r in recent):
        problems.append("identical to a recent reply - a bot tell")
    if len(text) > min(TWEET_LIMIT, 200):
        problems.append(f"{len(text)} chars - a reply should be one line")
    if URL.search(text):
        problems.append("contains a URL")
    if "#" in text or "$" in text:
        problems.append("carries a tag; replies do not")
    low = text.lower()
    for word in BANNED:
        if word in low:
            problems.append(f"contains {word!r} - argues or advertises")

    allowed = set()
    cross = facts.get("cross") or {}
    for raw in (facts.get("rate"), facts.get("pair_move_pct"),
                cross.get("equals")):
        raw = (raw or "").replace(",", "").lstrip("+")
        try:
            v = float(raw)
        except ValueError:
            continue
        allowed.add(v)
        allowed.add(abs(v))
    if cross:
        allowed.add(1.0)
    for cands in number_tokens(text):
        if cands and not any(traceable(n, allowed) for n in cands):
            problems.append(f"number {min(cands, key=abs)} is not in the facts")
    return problems


def rotation(want, now):
    if len(want) <= CCYS_PER_RUN:
        return want
    order = list(want)
    random.Random(f"{now:%Y-%m-%d}").shuffle(order)
    start = (now.hour * CCYS_PER_RUN) % len(order)
    doubled = order + order
    return doubled[start:start + CCYS_PER_RUN]


def candidates(want, entries, state):
    info, fix, owners = load()
    rows = {r["ccy"]: r for r in
            build(info, fix, owners, LANGUAGES, local_terms(), topic_terms())}
    today = now_utc().date().isoformat()
    per_ccy = (state.get("days", {}).get(today, {}).get("by_ccy") or {})
    out, searched = [], 0
    for ccy in want:
        if ccy not in rows:
            continue
        if per_ccy.get(ccy, 0) >= MAX_PER_CCY_PER_DAY:
            log(f"  {ccy}: at its daily cap")
            continue
        if not fixing_for(ccy):
            log(f"  {ccy}: no fixing fresh enough to quote")
            continue
        try:
            posts = search(rows[ccy]["tight"], POSTS_PER_SEARCH)
            searched += 1
        except Fatal as e:
            log(f"  {e}")
            break
        except Exception as e:
            log(f"  {ccy}: search failed ({e})")
            continue
        for p in posts:
            if p["id"] in set(state.get("replied") or []):
                continue
            if state.get("accounts", {}).get(str(p.get("author_id"))) == today:
                continue
            age = age_minutes(p.get("created_at"))
            if age is None or age > MAX_POST_AGE_MIN:
                continue
            if not screen(p).ok:
                continue
            out.append((ccy, age, p))
        if out:
            break
    if searched:
        log(f"  {searched} search(es), ~${searched * POSTS_PER_SEARCH * USD_PER_POST:.2f}")
    def asked(p):
        return "?" in p["text"] or "؟" in p["text"]
    out.sort(key=lambda t: (not asked(t[2]), t[1]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ccy", help="comma-separated; default is the rotation")
    ap.add_argument("--post", action="store_true", help="actually reply")
    args = ap.parse_args()

    state = load_state()
    today = now_utc().date().isoformat()
    day = state.setdefault("days", {}).setdefault(today, {"n": 0, "by_ccy": {}})
    cap = daily_cap(state, today)
    if day["n"] >= cap:
        log(f"{day['n']}/{cap} replies already today - done until tomorrow")
        return 0
    unlocked = min(cap, -(-cap * (now_utc().hour + 1) // 24))
    if day["n"] >= unlocked:
        log(f"{day['n']}/{cap} today, {unlocked} slot(s) unlocked - paced "
            "until later")
        return 0

    want = ([c.strip().upper() for c in args.ccy.split(",")] if args.ccy
            else rotation(TIERS["frontier"] + TIERS["major"], now_utc()))
    log(f"this hour: {', '.join(want)}")
    entries = (load_json(ANALYSIS, {}).get("currencies") or {})
    if not entries:
        log("no analysis - nothing to quote")
        return 1

    found = candidates(want, entries, state)
    if not found:
        log("nothing worth replying to this hour")
        return 0
    ccy, age, post = found[0]
    log(f"target: {ccy}, post {post['id']}, {age:.0f} min old")
    if os.environ.get("BANKNOTE_LOG_POSTS") == "1":
        log(f"  under: {' '.join(post['text'].split())[:140]}")

    facts = facts_for(ccy, fixing_for(ccy), entries, post.get("text") or "")
    lang = reply_language(facts, post.get("lang"))
    recent = state.get("texts") or []
    try:
        text = compose(facts, lang, recent)
    except Exception as e:
        log(f"compose failed ({type(e).__name__}: {e})")
        return 1

    problems = validate(text, facts, recent)
    log(f"  reply ({lang}): {text}")
    if problems:
        for p in problems:
            log(f"  REJECTED: {p}")
        return 1

    enabled = os.environ.get("BANKNOTE_REPLY_ENABLED") == "1"
    if not (args.post and enabled):
        why = []
        if not args.post:
            why.append("--post not given")
        if not enabled:
            why.append("BANKNOTE_REPLY_ENABLED is not 1")
        log(f"not posting ({'; '.join(why)})")
        return 0

    resp = post_to_x(text, reply_to=post["id"])
    rid = ((resp or {}).get("data") or {}).get("id")
    log(f"replied to {post['id']} (our post {rid})")
    state["first_day"] = state.get("first_day") or today
    state["replied"].append(post["id"])
    state.setdefault("posts", []).append(
        {"id": rid, "to": post["id"], "ccy": ccy, "day": today})
    state["posts"] = state["posts"][-200:]
    state["texts"] = (recent + [text])[-6:]
    state.setdefault("accounts", {})[str(post.get("author_id"))] = today
    day["n"] += 1
    day["by_ccy"][ccy] = day["by_ccy"].get(ccy, 0) + 1
    state["days"] = {d: v for d, v in state["days"].items() if d >= today}
    save_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
