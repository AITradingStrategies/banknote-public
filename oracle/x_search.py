import json
import os
import urllib.error
import urllib.parse
import urllib.request

SEARCH = "https://api.x.com/2/tweets/search/recent"


class Fatal(Exception):
    pass


def bearer():
    tok = os.environ.get("X_BEARER_TOKEN")
    if not tok:
        raise Fatal("X_BEARER_TOKEN is not set (search needs app-only auth)")
    return {"Authorization": f"Bearer {tok}"}


def search(query, n):
    params = {
        "query": query,
        "max_results": str(max(10, min(n, 100))),
        "tweet.fields": "lang,created_at,public_metrics",
        "expansions": "author_id",
        "user.fields": "public_metrics",
    }
    url = SEARCH + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=bearer())
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = f"HTTP {e.code}: {e.read().decode()[:200]}"
        if e.code in (401, 403, 429):
            raise Fatal(detail)
        raise RuntimeError(detail)
    users = {u["id"]: u for u in (body.get("includes") or {}).get("users") or []}
    out = []
    for t in (body.get("data") or [])[:n]:
        u = users.get(t.get("author_id")) or {}
        out.append({
            "id": t["id"],
            "text": t.get("text") or "",
            "lang": t.get("lang"),
            "author_id": t.get("author_id"),
            "created_at": t.get("created_at"),
            "replies": (t.get("public_metrics") or {}).get("reply_count"),
            "followers": (u.get("public_metrics") or {}).get("followers_count"),
        })
    return out
