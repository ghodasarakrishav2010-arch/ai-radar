"""AI Radar leaderboard collector.

Reads the official Arena (LMArena) leaderboard dataset published on Hugging Face
(licence CC BY 4.0, attribution required) and saves data/leaderboards.json.
Needs the free 'pyarrow' package (the GitHub timer installs it).
"""
import io
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

HF = "https://huggingface.co"
REPO = "lmarena-ai/leaderboard-dataset"
OUT = "data/leaderboards.json"
OUT_STATUS = "data/lb_status.json"
SOURCE_URL = "https://arena.ai/leaderboard"
TOP_N = 30
UA = "Mozilla/5.0 (compatible; AI-Radar-Bot/1.0)"

# (id, name shown on the site, dataset subset, label of the source)
FIELDS = [
    ("text", "Text", "text_style_control", "Arena text leaderboard"),
    ("code", "Code", "webdev", "Arena Code (WebDev) leaderboard"),
    ("image", "Image", "text_to_image", "Arena text-to-image leaderboard"),
    ("video", "Video", "text_to_video", "Arena text-to-video leaderboard"),
    ("vision", "Vision", "vision_style_control", "Arena vision leaderboard"),
    ("search", "Search", "search_style_control", "Arena search leaderboard"),
    ("agents", "Agents", "agent", "Arena Agent leaderboard"),
    ("open", "Open-source", "text_style_control", "Arena text leaderboard, open licences only"),
]

COMPANY_NAMES = {
    "openai": "OpenAI", "anthropic": "Anthropic", "google": "Google", "meta": "Meta",
    "xai": "xAI", "deepseek": "DeepSeek", "alibaba": "Alibaba", "moonshot": "Moonshot AI",
    "zai": "Z.ai", "nvidia": "NVIDIA", "minimax": "MiniMax", "mistral": "Mistral",
    "tencent": "Tencent", "xiaomi": "Xiaomi", "microsoft": "Microsoft", "bytedance": "ByteDance",
}


def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def load_subset(subset):
    """Download the 'latest' leaderboard rows of one Arena subset."""
    import pyarrow.parquet as pq

    listing = json.loads(fetch("%s/api/datasets/%s/tree/main/%s" % (HF, REPO, subset)))
    paths = [
        e["path"] for e in listing
        if os.path.basename(e.get("path", "")).startswith("latest-") and e["path"].endswith(".parquet")
    ]
    if not paths:
        raise RuntimeError("no 'latest' file found for " + subset)
    rows = []
    for p in paths:
        url = "%s/datasets/%s/resolve/main/%s" % (HF, REPO, urllib.parse.quote(p))
        rows += pq.read_table(io.BytesIO(fetch(url, timeout=90))).to_pylist()
    return rows


def pick_rows(rows):
    """Use the 'overall' category (or the first one if there is none)."""
    cats = sorted({r.get("category") for r in rows if r.get("category")})
    if not cats:
        raise RuntimeError("no category column")
    cat = "overall" if "overall" in cats else cats[0]
    return cat, [r for r in rows if r.get("category") == cat]


def build_field(fid, name, rows, source, only_open=False):
    cat, rows = pick_rows(rows)
    if only_open:
        rows = [r for r in rows if (r.get("license") or "Proprietary") != "Proprietary"]
    rows = [r for r in rows if r.get("rank") is not None and r.get("model_name")]
    rows.sort(key=lambda r: r["rank"])
    models = []
    for r in rows[:TOP_N]:
        if r.get("rating") is not None:
            score = round(float(r["rating"]))
        elif r.get("score") is not None:
            score = round(float(r["score"]), 3)
        else:
            score = None
        org = (r.get("organization") or "").strip()
        models.append({
            "name": r["model_name"],
            "company": COMPANY_NAMES.get(org.lower(), org.title()),
            "score": score,
        })
    if not models:
        raise RuntimeError("no models for " + fid)
    dates = [r.get("leaderboard_publish_date") for r in rows if r.get("leaderboard_publish_date")]
    return {
        "id": fid,
        "name": name,
        "source": source,
        "source_url": SOURCE_URL,
        "category": cat,
        "published": str(max(dates)) if dates else "",
        "models": models,
    }


def main():
    os.makedirs("data", exist_ok=True)
    old = {}
    if os.path.exists(OUT):
        try:
            with open(OUT, encoding="utf-8") as f:
                old = {x["id"]: x for x in json.load(f).get("fields", [])}
        except Exception:
            old = {}

    cache, fields, status = {}, [], []
    for fid, name, subset, source in FIELDS:
        try:
            if subset not in cache:
                cache[subset] = load_subset(subset)
            fields.append(build_field(fid, name, cache[subset], source, only_open=(fid == "open")))
            status.append({"field": fid, "ok": True})
            print("OK  ", fid)
        except Exception as e:
            msg = str(e)[:150]
            status.append({"field": fid, "ok": False, "error": msg})
            print("FAIL", fid, msg)
            if fid in old:  # keep the previous ranking instead of losing it
                fields.append(old[fid])

    if not any(s["ok"] for s in status):
        print("Every leaderboard failed. Keeping old data.")
        sys.exit(1)

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"updated": now, "fields": fields}, f, ensure_ascii=False, indent=1)
    with open(OUT_STATUS, "w", encoding="utf-8") as f:
        json.dump({"updated": now, "fields": status}, f, indent=1)
    print("Saved %d fields." % len(fields))


if __name__ == "__main__":
    main()
