"""AI Radar news collector.

Gathers AI news from free public sources and saves data/news.json.
Uses only Python's standard library, so nothing needs to be installed.
"""
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

OUT_NEWS = "data/news.json"
OUT_STATUS = "data/status.json"
MAX_ITEMS = 150
TIMEOUT = 20
UA = "Mozilla/5.0 (compatible; AI-Radar-Bot/1.0)"

# (name, feed address, only keep items that mention AI?)
FEEDS = [
    ("Hugging Face", "https://huggingface.co/blog/feed.xml", False),
    ("Google DeepMind", "https://deepmind.google/blog/rss.xml", False),
    ("OpenAI", "https://openai.com/news/rss.xml", False),
    ("NVIDIA", "https://blogs.nvidia.com/feed/", True),
]

# Hacker News searches (public API). Only posts with enough upvotes are kept.
HN_QUERIES = ["OpenAI", "Anthropic", "Claude", "Gemini", "DeepSeek", "LLM", "AI model", "Qwen", "Llama"]
HN_MIN_POINTS = 30

AI_WORDS = re.compile(
    r"\b(ai|a\.i\.|llms?|gpts?|chatgpt|openai|anthropic|claude|gemini|deepseek|grok|mistral|qwen|llama|"
    r"copilot|nvidia|neural|generative|agentic|machine learning|language models?|ai models?)\b", re.I)

FIELD_RULES = [
    ("code", r"\b(code|coding|codex|copilot|swe-bench|programming)\b"),
    ("video", r"\b(video|sora|veo|runway|kling)\b"),
    ("image", r"\b(image|images|diffusion|midjourney|flux|dall-e)\b"),
    ("audio", r"\b(audio|voice|speech|music|tts|whisper)\b"),
    ("vision", r"\b(vision|multimodal|vlm)\b"),
    ("open-source", r"\b(open[- ]source|open[- ]weights?|llama|qwen|mistral|deepseek|hugging face)\b"),
    ("text", r"\b(llm|gpt|claude|gemini|chatbot|language model|reasoning)\b"),
]


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read()


def clean(text):
    text = html.unescape(re.sub(r"<[^>]+>", "", text or ""))
    return re.sub(r"\s+", " ", text).strip()[:200]


def guess_field(title):
    low = title.lower()
    for field, pattern in FIELD_RULES:
        if re.search(pattern, low):
            return field
    return ""


def local(tag):
    return tag.rsplit("}", 1)[-1]


def child_text(el, names):
    for c in el:
        if local(c.tag) in names and c.text and c.text.strip():
            return c.text.strip()
    return ""


def get_link(el):
    for c in el:
        if local(c.tag) == "link":
            if c.text and c.text.strip():
                return c.text.strip()
            href = c.get("href")
            if href and c.get("rel", "alternate") == "alternate":
                return href
    return ""


def parse_date(s):
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)
    except Exception:
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def make_item(title, source, url, dt):
    return {
        "title": title,
        "source": source,
        "url": url,
        "date": dt.strftime("%Y-%m-%d") if dt else "",
        "ts": dt.isoformat() if dt else "1970-01-01T00:00:00+00:00",
        "field": guess_field(title),
    }


def read_feed(name, url, ai_only):
    root = ET.fromstring(fetch(url))
    items = []
    for el in root.iter():
        if local(el.tag) not in ("item", "entry"):
            continue
        title = clean(child_text(el, {"title"}))
        link = get_link(el)
        if not title or not link.startswith(("http://", "https://")):
            continue
        if ai_only and not AI_WORDS.search(title):
            continue
        dt = parse_date(child_text(el, {"pubDate", "published", "updated", "date"}))
        items.append(make_item(title, name, link, dt))
    return items


def read_hn(query):
    params = urllib.parse.urlencode({
        "tags": "story",
        "query": query,
        "numericFilters": "points>=%d" % HN_MIN_POINTS,
        "hitsPerPage": 20,
    })
    data = json.loads(fetch("https://hn.algolia.com/api/v1/search_by_date?" + params))
    items = []
    for h in data.get("hits", []):
        title = clean(h.get("title"))
        url = h.get("url") or "https://news.ycombinator.com/item?id=%s" % h.get("objectID")
        if not title or not url.startswith(("http://", "https://")):
            continue
        if not AI_WORDS.search(title):  # drop posts that are not clearly about AI
            continue
        dt = datetime.fromtimestamp(h["created_at_i"], tz=timezone.utc) if h.get("created_at_i") else None
        items.append(make_item(title, "Hacker News", url, dt))
    return items


def key_for(item):
    return re.sub(r"[^a-z0-9]", "", item["title"].lower())[:80]


def main():
    os.makedirs("data", exist_ok=True)
    collected, status = [], []

    for name, url, ai_only in FEEDS:
        try:
            got = read_feed(name, url, ai_only)
            collected += got
            status.append({"source": name, "ok": True, "items": len(got)})
        except Exception as e:
            status.append({"source": name, "ok": False, "error": str(e)[:150]})

    hn_total, hn_errors = 0, 0
    for q in HN_QUERIES:
        try:
            got = read_hn(q)
            collected += got
            hn_total += len(got)
        except Exception:
            hn_errors += 1
    status.append({"source": "Hacker News", "ok": hn_errors < len(HN_QUERIES), "items": hn_total,
                   "failed_queries": hn_errors})

    for s in status:
        print(("OK   " if s["ok"] else "FAIL ") + s["source"], s.get("items", ""), s.get("error", ""))

    if not any(s["ok"] for s in status):
        print("Every source failed. Keeping old data.")
        sys.exit(1)

    old = []
    if os.path.exists(OUT_NEWS):
        try:
            with open(OUT_NEWS, encoding="utf-8") as f:
                old = json.load(f).get("items", [])
        except Exception:
            old = []

    merged, seen_urls, seen_titles = [], set(), set()
    for item in collected + old:  # fresh items win over old ones
        u = item["url"].rstrip("/")
        k = key_for(item)
        if u in seen_urls or k in seen_titles:
            continue
        seen_urls.add(u)
        seen_titles.add(k)
        merged.append(item)

    merged.sort(key=lambda i: i["ts"], reverse=True)
    merged = merged[:MAX_ITEMS]

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(OUT_NEWS, "w", encoding="utf-8") as f:
        json.dump({"updated": now, "items": merged}, f, ensure_ascii=False, indent=1)
    with open(OUT_STATUS, "w", encoding="utf-8") as f:
        json.dump({"updated": now, "sources": status}, f, indent=1)
    print("Saved %d items." % len(merged))


if __name__ == "__main__":
    main()
