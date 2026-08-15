import hashlib
import re
import unicodedata
import urllib.parse
from collections import Counter

MAX_EXISTING_REPLIES = 25

MAX_FOLLOWERS = 100_000

MAX_REPLIES_PER_ACCOUNT_PER_DAY = 1

MAX_CURRENCIES_NAMED = 2

_CODES = ("USD|EUR|GBP|JPY|CHF|CAD|AUD|NZD|CNY|CNH|HKD|SGD|"
          "INR|PKR|BDT|LKR|NPR|IDR|MYR|THB|VND|PHP|KRW|TWD|"
          "NGN|GHS|KES|UGX|TZS|ZAR|EGP|MAD|DZD|TND|XOF|XAF|ETB|"
          "TRY|RUB|UAH|PLN|CZK|HUF|RON|SEK|NOK|DKK|ILS|"
          "SAR|AED|QAR|KWD|BHD|OMR|JOD|LBP|IQD|IRR|"
          "BRL|ARS|MXN|CLP|COP|PEN|UYU|VES|BOB|"
          "XAU|XAG|BTC|ETH")
CODES = re.compile(rf"\b(?:{_CODES})\b")
PAIRS = re.compile(rf"\b({_CODES})({_CODES})\b")

WORD_CODES = {
    "dollar": {"USD", "AUD", "CAD", "NZD", "SGD", "HKD", "TWD"},
    "dollars": {"USD", "AUD", "CAD", "NZD", "SGD", "HKD", "TWD"},
    "euro": {"EUR"}, "euros": {"EUR"}, "iene": {"JPY"},
    "pound": {"GBP", "EGP", "LBP"}, "pounds": {"GBP", "EGP", "LBP"},
    "sterling": {"GBP"}, "libra": {"GBP"}, "livre": {"GBP", "EGP", "LBP"},
    "yen": {"JPY"}, "yuan": {"CNY", "CNH"}, "iuan": {"CNY"},
    "renminbi": {"CNY", "CNH"},
    "franc": {"CHF", "XOF", "XAF"}, "francs": {"CHF", "XOF", "XAF"},
    "rupee": {"INR", "PKR", "LKR", "NPR"},
    "rupees": {"INR", "PKR", "LKR", "NPR"},
    "rupiah": {"IDR"}, "ringgit": {"MYR"}, "baht": {"THB"}, "dong": {"VND"},
    "peso": {"MXN", "ARS", "CLP", "COP", "PHP", "UYU"},
    "pesos": {"MXN", "ARS", "CLP", "COP", "PHP", "UYU"},
    "real": {"BRL"}, "reais": {"BRL"},
    "naira": {"NGN"}, "cedi": {"GHS"},
    "shilling": {"KES", "UGX", "TZS"}, "shillings": {"KES", "UGX", "TZS"},
    "rand": {"ZAR"}, "birr": {"ETB"}, "taka": {"BDT"},
    "dinar": {"KWD", "BHD", "JOD", "IQD", "DZD", "TND"},
    "dirham": {"AED", "MAD"}, "riyal": {"SAR", "QAR"}, "rial": {"OMR", "IRR"},
    "lira": {"TRY"}, "ruble": {"RUB"}, "rouble": {"RUB"},
    "hryvnia": {"UAH"}, "zloty": {"PLN"}, "koruna": {"CZK"},
    "forint": {"HUF"}, "krona": {"SEK"}, "krone": {"NOK", "DKK"},
    "shekel": {"ILS"},
    "dolar": {"USD"}, "dólar": {"USD"}, "dolares": {"USD"}, "dólares": {"USD"},
}
WORDS = re.compile(r"\b(?:" + "|".join(sorted(WORD_CODES, key=len, reverse=True))
                   + r")\b", re.I)

FIGURE = re.compile(r"\d[\d\s.,]*\d|\b\d\b")

DROP = [
    ("promo", re.compile(
        r"\b(?:vip|premium plan|membership|subscribe|sign ?up|join (?:my|our|the)|"
        r"telegram|t\.me/|whatsapp|dm (?:me|us|for)|link in bio|free trial|"
        r"signals?|academy|mentorship|mentor|copy ?trad\w*|"
        r"promo ?code|referral)\b", re.I)),

    ("trader", re.compile(
        r"\b(?:pips?|xauusd|xagusd|dxy|tradingview|order ?block|price action|"
        r"stop ?loss|take ?profit|\btp\d?\b|\bsl\b|poi|liquidity (?:sweep|grab|pool)|"
        r"key ?level|fibonacci|\bfib\b|elliott|wyckoff|scalp\w*|"
        r"long(?:ed|ing)? (?:this|the)|short(?:ed|ing)? (?:this|the)|"
        r"stopped out|entry (?:point|zone|price)|setup|backtest\w*)\b", re.I)),

    ("hashtag-wall", None),

    ("no-content", None),
]

_ACCENTS = str.maketrans("", "", "̧̀́̂̃̈̊")


PROMO_HOSTS = {"chat.whatsapp.com", "wa.me", "t.me", "telegram.me",
               "discord.gg", "linktr.ee", "beacons.ai"}


def promo_link(urls):
    for url in urls or []:
        host = urllib.parse.urlparse(url or "").netloc.lower()
        if any(host == d or host.endswith("." + d) for d in PROMO_HOSTS):
            return host
    return None


def visible(text):
    return "".join(ch for ch in (text or "")
                   if unicodedata.category(ch) != "Cf")


class Verdict:
    __slots__ = ("ok", "reason", "detail")

    def __init__(self, ok, reason="", detail=""):
        self.ok, self.reason, self.detail = ok, reason, detail

    def __repr__(self):
        return f"<{'keep' if self.ok else 'drop'} {self.reason}{': ' + self.detail if self.detail else ''}>"

    def __bool__(self):
        return self.ok


def normalise(text):
    t = unicodedata.normalize("NFKD", text or "").translate(_ACCENTS).lower()
    t = re.sub(r"https?://\S+", "", t)
    t = re.sub(r"[\W_]+", " ", t, flags=re.UNICODE).strip()
    return t


def fingerprint(text):
    return hashlib.sha1(normalise(text).encode()).hexdigest()[:16]


def currencies_named(text):
    text = text or ""
    codes = set(m.group(0).upper() for m in CODES.finditer(text))
    for m in PAIRS.finditer(text):
        codes |= {m.group(1).upper(), m.group(2).upper()}
    extra = set()
    for m in WORDS.finditer(text):
        word = m.group(0).lower()
        meanings = WORD_CODES.get(word, set())
        if not (meanings & codes):
            extra.add(frozenset(meanings) or word)
    return len(codes) + len(extra)


def codes_in(text):
    text = visible(text or "")
    codes = set(m.group(0).upper() for m in CODES.finditer(text))
    for m in PAIRS.finditer(text):
        codes |= {m.group(1).upper(), m.group(2).upper()}
    for m in WORDS.finditer(text):
        meanings = WORD_CODES.get(m.group(0).lower(), set())
        if len(meanings) == 1:
            codes |= meanings
    return codes


def has_figure(text):
    return bool(FIGURE.search(text or ""))


def hashtag_wall(text):
    body = re.sub(r"#\w+", "", text or "")
    body = re.sub(r"https?://\S+", "", body)
    letters = re.findall(r"[^\W\d_]", body, re.UNICODE)
    return len(re.findall(r"#\w+", text or "")) >= 5 and len(letters) < 20


def no_content(text):
    stripped = re.sub(r"https?://\S+", "", text or "")
    stripped = re.sub(r"[@#]\w+", "", stripped)
    return len(re.findall(r"[^\W\d_]", stripped, re.UNICODE)) < 15


def screen(post, seen=None, account_replies=None):
    text = visible(post.get("text") or "")

    if no_content(text):
        return Verdict(False, "no-content")
    if hashtag_wall(text):
        return Verdict(False, "hashtag-wall")

    for name, pattern in DROP:
        if pattern is None:
            continue
        m = pattern.search(text)
        if m:
            return Verdict(False, name, m.group(0))

    host = promo_link(post.get("urls"))
    if host:
        return Verdict(False, "promo-link", host)

    n = currencies_named(text)
    if n > MAX_CURRENCIES_NAMED:
        return Verdict(False, "rate-table", f"{n} currencies")

    if not has_figure(text) and "?" not in text and "؟" not in text:
        return Verdict(False, "no-figure")

    replies = post.get("replies")
    if replies is not None and replies > MAX_EXISTING_REPLIES:
        return Verdict(False, "crowded", f"{replies} replies")

    followers = post.get("followers")
    if followers is not None and followers > MAX_FOLLOWERS:
        return Verdict(False, "too-big", f"{followers:,} followers")

    fp = fingerprint(text)
    if seen is not None:
        if fp in seen:
            return Verdict(False, "duplicate")
        seen.add(fp)

    author = post.get("author_id")
    if author and account_replies is not None:
        if account_replies[author] >= MAX_REPLIES_PER_ACCOUNT_PER_DAY:
            return Verdict(False, "account-cap")
        account_replies[author] += 1

    return Verdict(True, "keep")


def screen_all(posts):
    seen, per_account = set(), Counter()
    return [screen(p, seen, per_account) for p in posts]


def _from_sample_log(path):
    raw = open(path, encoding="utf-8", errors="replace").read()
    text = raw.replace("\\n", "\n").replace('\\"', '"')
    hdr = re.compile(r"--- ([A-Z]{3}) \((\w+)\):")
    post = re.compile(r"\[(\w+)\s*\] \((\S+?)\) (.*)$")
    out, ccy, tier = [], None, None
    for line in text.split("\n"):
        m = hdr.search(line)
        if m:
            ccy, tier = m.group(1), m.group(2)
            continue
        m = post.search(line)
        if m and ccy:
            mark, lang, body = m.groups()
            out.append({"ccy": ccy, "tier": tier, "lang": lang,
                        "text": " ".join(body.split()),
                        "worth": mark.isupper() and mark != "NONE"})
    return out


def _legible(post):
    text = post.get("text") or ""
    return text.count("?") <= max(2, len(text) * 0.08)


def _report(path):
    every = _from_sample_log(path)
    posts = [p for p in every if _legible(p)]
    mangled = len(every) - len(posts)
    if mangled:
        print(f"note: {mangled} of {len(every)} posts came back from the log "
              "with their script replaced by '?' and are excluded from "
              "scoring (mostly Arabic, Japanese, Thai, Urdu).\n")
    verdicts = screen_all(posts)
    kept = [(p, v) for p, v in zip(posts, verdicts) if v.ok]
    dropped = [(p, v) for p, v in zip(posts, verdicts) if not v.ok]

    agree_keep = sum(1 for p, _ in kept if p["worth"])
    model_worth = sum(1 for p in posts if p["worth"])
    print(f"{len(posts)} posts   model said worth: {model_worth}   "
          f"filter keeps: {len(kept)}")
    if kept:
        print(f"  of those kept, {agree_keep} ({agree_keep/len(kept):.0%}) "
              "the model also called worth replying")
    if model_worth:
        print(f"  of the model's {model_worth}, the filter kept {agree_keep} "
              f"({agree_keep/model_worth:.0%}) and dropped "
              f"{model_worth - agree_keep}")
    print("\ndropped by reason:")
    for reason, n in Counter(v.reason for _, v in dropped).most_common():
        lost = sum(1 for p, v in dropped if v.reason == reason and p["worth"])
        print(f"  {reason:14s} {n:4d}   (incl. {lost} the model wanted)")
    print("\nkept, by tier:")
    for tier in ("frontier", "pegged", "major"):
        tp = [p for p in posts if p["tier"] == tier]
        tk = [p for p, _ in kept if p["tier"] == tier]
        if tp:
            print(f"  {tier:9s} {len(tk):3d} / {len(tp):3d}  ({len(tk)/len(tp):.0%})")


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        raise SystemExit("usage: reply_filter.py <run_sample job log>")
    _report(sys.argv[1])
