import argparse
import collections
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(HERE, ".."))
COUNTRIES = (os.environ.get("BANKNOTE_COUNTRIES")
             or os.path.join(REPO, "web", "src", "data", "countries.json"))
OUT = os.path.join(HERE, "state", "listen_queries.json")
TERMS = os.path.join(HERE, "listen_terms.json")
TOPICS = os.path.join(HERE, "listen_topics.json")

ENGLISH_WORDS = {"real", "won", "dong", "rand", "kina", "lek", "dram", "kip",
                 "mark", "loti", "leone", "crown", "pound", "dollar", "euro"}

COUNTRY_ALIASES = {
    "Türkiye": ["Turkey"],
    "Ivory Coast": ["Côte d'Ivoire", "Cote d'Ivoire"],
    "Myanmar": ["Burma"],
    "Eswatini": ["Swaziland"],
    "Czechia": ["Czech Republic"],
    "DR Congo": ["DRC", "Congo-Kinshasa"],
    "Timor-Leste": ["East Timor"],
    "North Macedonia": ["Macedonia"],
    "Cape Verde": ["Cabo Verde"],
    "United Arab Emirates": ["UAE"],
    "United Kingdom": ["UK", "Britain"],
    "United States": ["USA", "US"],
    "South Korea": ["Korea"],
    "Netherlands": ["Holland"],
}

NO_SPACE_SCRIPTS = {"Thai", "Khmer", "Lao", "Burmese"}
PHRASE_CHARS = 6

HAND = {
    "USD": {"terms": ['"US dollar"', '"dollar index"', "DXY"], "pairs": []},
    "EUR": {"terms": [], "pairs": ["EURUSD", '"EUR/USD"']},
    "XOF": {"terms": ['"CFA franc"', '"franc CFA"']},
    "XAF": {"terms": ['"CFA franc"', '"franc CFA"']},
    "XCD": {"terms": ['"East Caribbean dollar"', '"EC dollar"']},
}


def load():
    with open(COUNTRIES, encoding="utf-8") as fh:
        d = json.load(fh)
    info, fix = d.get("ccyInfo") or {}, d.get("fixCcys") or []
    owners = collections.defaultdict(list)
    for ccy, disp in zip(d.get("ccys") or [], d.get("display") or []):
        if ccy:
            owners[ccy].append(disp)
    return info, fix, owners


def local_terms():
    try:
        with open(TERMS, encoding="utf-8") as fh:
            d = json.load(fh)
    except OSError:
        return {}
    d.pop("_note", None)
    return d


def topic_terms():
    try:
        with open(TOPICS, encoding="utf-8") as fh:
            d = json.load(fh)
    except OSError:
        return {}
    d.pop("_note", None)
    return d


def loose_query(unit, ambiguous, local_bare, name):
    seen, words = set(), []

    def add(term):
        key = term.strip('"').lower()
        if key and key not in seen:
            seen.add(key)
            words.append(term)

    if not ambiguous:
        add(unit)
    if " " not in name:
        add(f'"{name}"')
    for t in local_bare:
        add(t)
    if not words:
        return None
    return "(" + " OR ".join(words) + ") -is:retweet"


def country_query(ccy, owners, lang, topics):
    places = owners.get(ccy) or []
    if not places or len(places) > 3:
        return None, "shared or country-less - no country query"
    words = list((topics.get("English") or {}).get("terms") or [])
    local_set = (topics.get(lang) or {}).get("terms") or []
    for w in local_set:
        if w not in words:
            words.append(w)
    if not words:
        return None, f"no topic phrases for {lang}"
    labels = []
    for place in places:
        pretty = place.title()
        for label in [pretty] + COUNTRY_ALIASES.get(pretty, []):
            if label not in labels:
                labels.append(label)
    where = " OR ".join(f'"{p}"' for p in labels)
    return f"({where}) ({' OR '.join(words)}) -is:retweet", None


def build(info, fix, owners, languages=None, local=None, topics=None):
    unit_of = {c: info[c]["name"].split()[-1].lower() for c in fix if c in info}
    shared = collections.Counter(unit_of.values())
    rows = []
    for ccy in fix:
        if ccy not in info:
            continue
        name, unit = info[ccy]["name"], unit_of[ccy]
        ambiguous = shared[unit] > 1 or unit in ENGLISH_WORDS

        hand = HAND.get(ccy)
        terms = []
        if " " in name:
            terms.append(f'"{name}"')
        if hand is not None:
            terms += hand["terms"]
        else:
            for country in owners.get(ccy, [])[:2]:
                terms.append(f'"{country.title()} {unit}"')
        if hand is not None and "pairs" in hand:
            terms += hand["pairs"]
        else:
            terms += [f"USD{ccy}", f'"{ccy}/USD"']
        if not ambiguous:
            terms.append(f'"{unit} rate"')

        entry = (local or {}).get(ccy) or {}
        local_all = entry.get("terms") or []
        spaceless = (languages or {}).get(ccy) in NO_SPACE_SCRIPTS

        def is_phrase(term):
            t = term.strip('"')
            return " " in t or (spaceless and len(t) >= PHRASE_CHARS)

        local_phrases = [t for t in local_all if is_phrase(t)]
        local_bare = [t for t in local_all if not is_phrase(t)]
        terms += local_phrases

        seen, ordered = set(), []
        for t in terms:
            if t.lower() not in seen:
                seen.add(t.lower())
                ordered.append(t)

        reasons = []
        if ambiguous:
            reasons.append(f"bare name '{unit}' is shared or an English word")
        lang = (languages or {}).get(ccy, "English")
        if lang != "English" and not entry:
            reasons.append(f"needs {lang} terms - English-only undercounts it")
        conf = entry.get("confidence")
        if conf in ("low", "medium"):
            reasons.append(f"{lang} terms are {conf}-confidence - a zero here "
                           f"is unmeasured, not quiet")

        cq, cq_why = country_query(ccy, owners, lang, topics or {})
        if cq_why:
            reasons.append(cq_why)
        tconf = ((topics or {}).get(lang) or {}).get("confidence")
        if tconf == "low":
            reasons.append(f"{lang} topic phrases are low-confidence")

        rows.append({
            "ccy": ccy,
            "name": name,
            "language": lang,
            "tight": "(" + " OR ".join(ordered) + ") -is:retweet",
            "loose": loose_query(unit, ambiguous, local_bare, name),
            "loose_terms": ([] if ambiguous else [unit])
                           + ([f'"{name}"'] if " " not in name else [])
                           + local_bare,
            "country": cq,
            "local_terms": entry.get("terms") or [],
            "local_confidence": entry.get("confidence"),
            "needs_hand_authoring": reasons,
        })
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help=f"write {OUT}")
    args = ap.parse_args()

    info, fix, owners = load()
    languages = {}
    try:
        from run_commentary import LANGUAGES
        languages = LANGUAGES
    except Exception:
        pass

    rows = build(info, fix, owners, languages, local_terms(), topic_terms())
    todo = [r for r in rows if r["needs_hand_authoring"]]
    loose = [r for r in rows if r["loose"]]

    for r in rows[:6]:
        print(f"--- {r['ccy']}  ({r['name']}, {r['language']}) ---")
        print(f"  TIGHT: {r['tight']}")
        print(f"  LOOSE: {r['loose'] or '(none - would measure the language, not the currency)'}")
        print(f"  CNTRY: {r['country'] or '(none)'}")
        if r["needs_hand_authoring"]:
            print(f"  TODO : {'; '.join(r['needs_hand_authoring'])}")
        print()

    print(f"{len(rows)} currencies")
    print(f"  {len(loose)} with a usable bare-name query")
    print(f"  {len(todo)} needing hand authoring before the sweep is trustworthy")
    country = [r for r in rows if r["country"]]
    print(f"  {len(country)} with a country-plus-topic query")
    print(f"  ~{len(rows) + len(loose) + len(country) + 1} requests per full sweep"
          f" (+1 global baseline)")
    conf = collections.Counter(r["local_confidence"] for r in rows
                               if r["local_confidence"])
    print(f"  local-language terms: {sum(conf.values())} currencies "
          f"({conf['high']} high, {conf['medium']} medium, {conf['low']} low)")

    if args.write:
        os.makedirs(os.path.dirname(OUT), exist_ok=True)
        with open(OUT, "w") as fh:
            json.dump(rows, fh, ensure_ascii=False, indent=1, sort_keys=True)
        print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
