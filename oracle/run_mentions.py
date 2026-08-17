import argparse
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _util import claim_slot
from reply_filter import (DROP, codes_in, hashtag_wall, no_content,
                          promo_link, visible)
from run_broadcast import log, post_to_x
from run_commentary import (ANALYSIS, MODEL, TWEET_LIMIT, URL,
                            load_json, number_tokens, traceable)
from run_reply import (X_LANGS, age_minutes, facts_for, fixing_for,
                       now_utc)
from x_search import Fatal, mentions, posts_by_ids, user_by_username

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "state", "mentions_state.json")

HANDLE = os.environ.get("BANKNOTE_X_HANDLE", "banknotelolai")

MAX_PER_DAY = 10
MAX_PER_THREAD_PER_DAY = 1
MAX_PER_AUTHOR_PER_DAY = 1
MAX_AGE_MIN = 24 * 60

BANNED = ["check out", "sign up", "join ", "dm ", "link in bio", "follow me",
          "follow us", "click", "you're wrong", "incorrect", "actually,"]

SYSTEM = """You are BanknoteAI, an automated currency-data account, \
answering someone who spoke to it on X. The account publishes a daily FX \
fixing - the median of at least 3 independent sources - for more than a \
hundred currencies.

Rules, all of them hard:
- ONE reply, under 200 characters, in the language the person wrote in.
- Be plain, warm and brief - a knowledgeable person, not a mascot and not a \
press release. No greeting, no emoji, no hashtags, no links, no @-mentions.
- Numbers: use ONLY the ones in the supplied facts, exactly as given. If the \
facts do not cover what they asked, say plainly that you do not have that \
number. NEVER estimate, extrapolate, or remember a figure from anywhere else.
- Each fact's `fixing_age` says when its fixing is from - "today", \
"yesterday", or a weekday name (fixings pause at weekends, so on a Sunday \
the newest is Friday's). Say the one given, translated naturally, and never \
claim the number is more current than that.
- `pair_move_pct` is the PAIR's day-on-day move (local currency per dollar), \
signed; `currency_direction` is the same fact said about the currency. They \
are opposites and both arrive already correct: never work one out from the \
other, never flip a sign.
- If they ask whether to buy, sell, hold, convert, or when: the account \
reports rates and does not advise. Say so kindly, every time, no exceptions.
- If they say the data is wrong: do not argue. Say how the fixing is made - \
daily, the median of independent sources - and leave it there.
- If they ask whether you are a bot or how this works: yes, automated, and \
the method is the sentence above. Honesty over mystique.
- Never promise anything, never ask them to follow, visit, or try anything.
"""

SCHEMA = {
    "type": "object",
    "properties": {"reply": {"type": "string"}},
    "required": ["reply"],
    "additionalProperties": False,
}


def load_state():
    try:
        with open(STATE_FILE) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"our_id": None, "since_id": None, "answered": [], "days": {}}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    state["answered"] = state["answered"][-500:]
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=1, sort_keys=True)
    os.replace(tmp, STATE_FILE)


def junk(text):
    text = visible(text)
    if hashtag_wall(text):
        return "hashtag-wall"
    for name, pattern in DROP:
        if pattern is not None and pattern.search(text):
            return name
    if (no_content(text) and "?" not in text and "؟" not in text
            and not codes_in(text)):
        return "no-content"
    return None


def gather_facts(their_text, our_text):
    entries = (load_json(ANALYSIS, {}).get("currencies") or {})
    out = []
    for ccy in sorted(codes_in(their_text) | codes_in(our_text or "")):
        if ccy == "USD" or len(out) == 2:
            continue
        fx = fixing_for(ccy)
        if fx:
            out.append(facts_for(ccy, fx, entries, their_text))
    return out


def compose(their_text, our_text, lang, facts):
    import anthropic
    ask = {
        "their_message": their_text,
        "our_post_they_are_replying_to": our_text,
        "language_hint": X_LANGS.get((lang or "").lower()) or lang,
        "facts": [{k: f[k] for k in ("pair", "rate", "fixing_age",
                                     "pair_move_pct", "currency_direction",
                                     "cross") if f.get(k) is not None}
                  for f in facts],
    }
    resp = anthropic.Anthropic().messages.create(
        model=MODEL,
        max_tokens=400,
        system=SYSTEM,
        output_config={"effort": "low",
                       "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user",
                   "content": json.dumps(ask, ensure_ascii=False)}],
    )
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            return json.loads(block.text)["reply"].strip()
    return ""


def validate(text, facts):
    problems = []
    if not text.strip():
        return ["empty"]
    if len(text) > min(TWEET_LIMIT, 240):
        problems.append(f"{len(text)} chars - an answer is one breath")
    if URL.search(text):
        problems.append("contains a URL")
    if "#" in text or "@" in text:
        problems.append("carries a tag or a mention; answers do not")
    low = text.lower()
    for word in BANNED:
        if word in low:
            problems.append(f"contains {word!r} - argues or advertises")

    allowed = {1.0, 3.0}
    for f in facts:
        cross = f.get("cross") or {}
        for raw in (f.get("rate"), f.get("pair_move_pct"), cross.get("equals")):
            raw = (raw or "").replace(",", "").lstrip("+")
            try:
                v = float(raw)
            except ValueError:
                continue
            allowed.add(v)
            allowed.add(abs(v))
    for cands in number_tokens(text):
        if cands and not any(traceable(n, allowed) for n in cands):
            problems.append(f"number {min(cands, key=abs)} is not in the facts")
    return problems


def pick(batch, state, our_id, today):
    day = state.get("days", {}).get(today) or {}
    threads = day.get("threads") or {}
    authors = day.get("authors") or {}
    for m in batch:
        why = None
        if m["author_id"] == our_id:
            why = "our own post"
        elif m["is_retweet"]:
            why = "a retweet"
        elif m["id"] in set(state.get("answered") or []):
            why = "already answered"
        elif (age_minutes(m.get("created_at")) or MAX_AGE_MIN + 1) > MAX_AGE_MIN:
            why = "older than a day"
        elif threads.get(m.get("conversation_id"), 0) >= MAX_PER_THREAD_PER_DAY:
            why = "thread at its daily cap"
        elif authors.get(str(m.get("author_id")), 0) >= MAX_PER_AUTHOR_PER_DAY:
            why = "author at their daily cap"
        else:
            why = junk(m["text"])
            if not why and promo_link(m.get("urls")):
                why = "promo-link"
        if why:
            log(f"  {m['id']}: skipped ({why})")
            continue
        return m
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--post", action="store_true", help="actually answer")
    args = ap.parse_args()

    state = load_state()
    today = now_utc().date().isoformat()
    day = state.setdefault("days", {}).setdefault(
        today, {"n": 0, "threads": {}, "authors": {}})
    if day["n"] >= MAX_PER_DAY:
        log(f"{day['n']}/{MAX_PER_DAY} answers already today - done")
        return 0

    try:
        if not state.get("our_id"):
            state["our_id"] = user_by_username(HANDLE)["id"]
        batch = mentions(state["our_id"], state.get("since_id"))
    except Fatal as e:
        log(f"{e}")
        return 0
    log(f"{len(batch)} mention(s) read")
    if not batch:
        return 0

    m = pick(batch, state, state["our_id"], today)
    if not m:
        state["since_id"] = batch[-1]["id"]
        save_state(state)
        log("nothing to answer")
        return 0
    log(f"answering mention {m['id']} "
        f"({age_minutes(m.get('created_at')) or 0:.0f} min old)")

    our_text = None
    if m.get("parent"):
        try:
            parents = posts_by_ids([m["parent"]])
            if parents and parents[0].get("author_id") == state["our_id"]:
                our_text = parents[0].get("text")
        except (Fatal, RuntimeError) as e:
            log(f"  parent lookup failed ({e}); answering without it")

    facts = gather_facts(m["text"], our_text)
    log(f"  {len(facts)} fact set(s): "
        f"{', '.join(f['pair'] for f in facts) or 'none - conversational'}")
    try:
        text = compose(m["text"], our_text, m.get("lang"), facts)
    except Exception as e:
        log(f"compose failed ({type(e).__name__}: {e})")
        return 1

    problems = validate(text, facts)
    log(f"  answer: {text}")
    if problems:
        for p in problems:
            log(f"  REJECTED: {p}")
        return 1

    enabled = os.environ.get("BANKNOTE_MENTIONS_ENABLED") == "1"
    if not (args.post and enabled):
        why = []
        if not args.post:
            why.append("--post not given")
        if not enabled:
            why.append("BANKNOTE_MENTIONS_ENABLED is not 1")
        log(f"not posting ({'; '.join(why)})")
        return 0

    state["answered"].append(m["id"])
    state["since_id"] = m["id"]
    day["n"] += 1
    conv, author = m.get("conversation_id"), str(m.get("author_id"))
    day["threads"][conv] = day["threads"].get(conv, 0) + 1
    day["authors"][author] = day["authors"].get(author, 0) + 1
    state["days"] = {d: v for d, v in state["days"].items() if d >= today}
    save_state(state)
    if not claim_slot([STATE_FILE], f"mentions: slot claim ({today})"):
        log("slot already claimed by a concurrent run - NOT posting")
        return 0
    post_to_x(text, reply_to=m["id"])
    log(f"answered {m['id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
