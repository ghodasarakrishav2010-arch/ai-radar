"""AI Radar news collector (v2: pictures, video badges, importance tiers).

Gathers AI news from free public sources and saves data/news.json.
Uses only Python's standard library, so nothing needs to be installed.
"""
import concurrent.futures
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
MAX_PAGE_FETCHES = 60  # how many article pages to open per run to find pictures
TIMEOUT = 20
UA = "Mozilla/5.0 (compatible; AI-Radar-Bot/1.0)"

# (name, feed address, only keep items that mention AI?)
FEEDS = [
    ("Hugging Face", "https://huggingface.co/blog/feed.xml", False),
    ("Google DeepMind", "https://deepmind.google/blog/rss.xml", False),
    ("OpenAI", "https://openai.com/news/rss.xml", False),
    ("NVIDIA", "https://blogs.nvidia.com/feed/", True),
]

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

# Importance: words that help you LEARN or BUILD raise the score, money and politics lower it.
LEARN = re.compile(
    r"\b(releases?|released|launch(?:es|ed)?|introducing|announc\w+|open[- ]source|open[- ]weights?|"
    r"benchmarks?|papers?|research|tutorials?|guide|how to|learn\w*|course|build\w*|agents?|api|sdk|"
    r"tools?|framework|library|models?|reasoning|fine-?tun\w+|training|inference|datasets?|prompts?|"
    r"coding|workflow)\b", re.I)
BUSINESS = re.compile(
    r"\b(raises?|funding|valuation|ipo|stocks?|shares|lawsuits?|sues?|court|layoffs?|rating|junk|"
    r"investors?|billions?|acquires?|acquisition|deals?|revenue|earnings|ban|banned|regulat\w+|"
    r"policy|election|politic\w*)\b", re.I)
OFFICIAL = {"OpenAI": 2, "Google DeepMind": 2, "Hugging Face": 2, "NVIDIA": 1}

IMG_TAG = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']", re.I)
YOUTUBE = re.compile(r"(?:youtube\.com/watch\?(?:[^#]*&)?v=|youtu\.be/|youtube\.com/shorts/)([\w-]{11})")


def fetch(url, timeout=TIMEOUT, limit=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(limit) if limit else r.read()


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


def normalize_image(url, base=""):
    """Return a safe https picture address, or an empty string."""
    if not url:
        return ""
    url = urllib.parse.urljoin(base, html.unescape(url.strip()))
    if url.startswith("http://"):
        url = "https://" + url[7:]
    return url if url.startswith("https://") else ""


def feed_media(el, base):
    """Find a picture (and whether it is a video) inside one feed entry."""
    image, video = "", False
    for c in el.iter():
        t, typ, medium = local(c.tag), (c.get("type") or ""), (c.get("medium") or "")
        if t in ("content", "enclosure") and c.get("url"):
            if medium == "video" or typ.startswith("video"):
                video = True
            elif not image and (medium == "image" or typ.startswith("image")):
                image = c.get("url")
        elif t == "thumbnail" and c.get("url") and not image:
            image = c.get("url")
    if not image:
        for c in el.iter():
            if local(c.tag) in ("description", "summary", "content", "encoded") and c.text:
                m = IMG_TAG.search(c.text)
                if m:
                    image = m.group(1)
                    break
    return normalize_image(image, base), video


def meta_content(text, names):
    for tag in re.findall(r"<meta\b[^>]*>", text, re.I):
        m = re.search(r"(?:property|name)=[\"']([^\"']+)[\"']", tag, re.I)
        if m and m.group(1).lower() in names:
            c = re.search(r"content=[\"']([^\"']*)[\"']", tag, re.I)
            if c and c.group(1):
                return html.unescape(c.group(1))
    return ""


def page_meta(url):
    """Open an article page and read its preview picture (og:image)."""
    text = fetch(url, timeout=8, limit=250000).decode("utf-8", "ignore")
    img = meta_content(text, ("og:image", "og:image:url", "twitter:image"))
    vid = bool(meta_content(text, ("og:video", "og:video:url"))) or \
        "video" in meta_content(text, ("og:type",)).lower()
    return normalize_image(img, url), vid


def make_item(title, source, url, dt, image=None, video=False, points=None):
    item = {
        "title": title,
        "source": source,
        "url": url,
        "date": dt.strftime("%Y-%m-%d") if dt else "",
        "ts": dt.isoformat() if dt else "1970-01-01T00:00:00+00:00",
        "field": guess_field(title),
    }
    yt = YOUTUBE.search(url)
    if yt:
        image, video = "https://i.ytimg.com/vi/%s/hqdefault.jpg" % yt.group(1), True
    if image:
        item["image"] = image
    if video:
        item["video"] = True
    if points is not None:
        item["points"] = points
    return item


def importance(item):
    """Score a story and give it a tier: 'top' (learn and build) or 'medium' (industry news)."""
    title = item["title"]
    score = min(len({m.lower() for m in LEARN.findall(title)}), 3)
    score -= min(2 * len({m.lower() for m in BUSINESS.findall(title)}), 4)
    score += OFFICIAL.get(item["source"], 0)
    pts = item.get("points") or 0
    score += 2 if pts >= 300 else 1 if pts >= 100 else 0
    return score, ("top" if score >= 3 else "medium")


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
        image, video = feed_media(el, link)
        items.append(make_item(title, name, link, dt, image=image or None, video=video))
    return items


def read_hn(query):
    params = urllib.parse.urlencode({
        "tags": "story", "query": query,
        "numericFilters": "points>=%d" % HN_MIN_POINTS, "hitsPerPage": 20,
    })
    data = json.loads(fetch("https://hn.algolia.com/api/v1/search_by_date?" + params))
    items = []
    for h in data.get("hits", []):
        title = clean(h.get("title"))
        url = h.get("url") or "https://news.ycombinator.com/item?id=%s" % h.get("objectID")
        if not title or not url.startswith(("http://", "https://")) or not AI_WORDS.search(title):
            continue
        dt = datetime.fromtimestamp(h["created_at_i"], tz=timezone.utc) if h.get("created_at_i") else None
        items.append(make_item(title, "Hacker News", url, dt, points=h.get("points") or 0))
    return items


def key_for(item):
    return re.sub(r"[^a-z0-9]", "", item["title"].lower())[:80]


def add_pictures(items):
    """Open article pages that have no picture yet (a limited number per run)."""
    todo = [i for i in items if "image" not in i][:MAX_PAGE_FETCHES]

    def work(i):
        try:
            return i, page_meta(i["url"])
        except Exception:
            return i, ("", False)

    found = 0
    with concurrent.futures.ThreadPoolExecutor(8) as ex:
        for i, (img, vid) in ex.map(work, todo):
            i["image"] = img  # an empty text means "tried, nothing found"
            if vid:
                i["video"] = True
            found += 1 if img else 0
    return len(todo), found


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
    old_by_url = {o["url"].rstrip("/"): o for o in old}

    merged, seen_urls, seen_titles = [], set(), set()
    for item in collected + old:  # fresh items win over old ones
        u = item["url"].rstrip("/")
        k = key_for(item)
        if u in seen_urls or k in seen_titles:
            continue
        seen_urls.add(u)
        seen_titles.add(k)
        o = old_by_url.get(u)
        if o and "image" not in item and "image" in o:  # keep pictures we already found
            item["image"] = o["image"]
            if o.get("video"):
                item["video"] = True
        merged.append(item)

    merged.sort(key=lambda i: i["ts"], reverse=True)
    merged = merged[:MAX_ITEMS]

    tried, found = add_pictures(merged)
    print("Pictures: opened %d pages, found %d." % (tried, found))
    for item in merged:
        item["score"], item["tier"] = importance(item)

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(OUT_NEWS, "w", encoding="utf-8") as f:
        json.dump({"updated": now, "items": merged}, f, ensure_ascii=False, indent=1)
    with open(OUT_STATUS, "w", encoding="utf-8") as f:
        json.dump({"updated": now, "sources": status,
                   "with_picture": sum(1 for i in merged if i.get("image"))}, f, indent=1)
    print("Saved %d items." % len(merged))


if __name__ == "__main__":
    main()
