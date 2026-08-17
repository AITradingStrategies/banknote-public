import json
import os
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.x.com/2"


class Fatal(Exception):
    pass


def bearer():
    tok = os.environ.get("X_BEARER_TOKEN")
    if not tok:
        raise Fatal("X_BEARER_TOKEN is not set (search needs app-only auth)")
    return {"Authorization": f"Bearer {tok}"}


def _get(path, params):
    url = API + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=bearer())
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = f"HTTP {e.code}: {e.read().decode()[:200]}"
        if e.code in (401, 403, 429):
            raise Fatal(detail)
        raise RuntimeError(detail)


def _shape(body, n):
    users = {u["id"]: u for u in (body.get("includes") or {}).get("users") or []}
    out = []
    for t in (body.get("data") or [])[:n]:
        u = users.get(t.get("author_id")) or {}
        refs = t.get("referenced_tweets") or []
        out.append({
            "id": t["id"],
            "text": t.get("text") or "",
            "lang": t.get("lang"),
            "author_id": t.get("author_id"),
            "created_at": t.get("created_at"),
            "replies": (t.get("public_metrics") or {}).get("reply_count"),
            "followers": (u.get("public_metrics") or {}).get("followers_count"),
            "conversation_id": t.get("conversation_id"),
            "parent": next((r.get("id") for r in refs
                            if r.get("type") == "replied_to"), None),
            "quoted": next((r.get("id") for r in refs
                            if r.get("type") == "quoted"), None),
            "is_retweet": any(r.get("type") == "retweeted" for r in refs),
            "urls": [u.get("unwound_url") or u.get("expanded_url") or u.get("url")
                     for u in (t.get("entities") or {}).get("urls") or []],
        })
    return out


def search(query, n):
    body = _get("/tweets/search/recent", {
        "query": query,
        "max_results": str(max(10, min(n, 100))),
        "tweet.fields": "lang,created_at,public_metrics,entities",
        "expansions": "author_id",
        "user.fields": "public_metrics",
    })
    return _shape(body, n)


def mentions(user_id, since_id=None, n=100):
    params = {
        "max_results": str(max(5, min(n, 100))),
        "tweet.fields": ("lang,created_at,public_metrics,conversation_id,"
                         "referenced_tweets,author_id,entities"),
        "expansions": "author_id",
        "user.fields": "public_metrics",
    }
    if since_id:
        params["since_id"] = str(since_id)
    body = _get(f"/users/{user_id}/mentions", params)
    return sorted(_shape(body, n), key=lambda p: int(p["id"]))


def posts_by_ids(ids):
    body = _get("/tweets", {
        "ids": ",".join(str(i) for i in ids[:100]),
        "tweet.fields": "public_metrics,created_at,author_id",
    })
    out = []
    for t in body.get("data") or []:
        m = t.get("public_metrics") or {}
        out.append({
            "id": t["id"],
            "text": t.get("text") or "",
            "author_id": t.get("author_id"),
            "created_at": t.get("created_at"),
            "impressions": m.get("impression_count"),
            "likes": m.get("like_count"),
            "replies": m.get("reply_count"),
            "retweets": m.get("retweet_count"),
        })
    return out


def user_by_username(handle):
    body = _get(f"/users/by/username/{urllib.parse.quote(handle)}",
                {"user.fields": "public_metrics"})
    u = body.get("data") or {}
    return {"id": u.get("id"),
            "followers": (u.get("public_metrics") or {}).get("followers_count")}
