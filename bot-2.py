import os, io, json, time, hashlib, re, html, glob
from datetime import datetime
from urllib.parse import urljoin, urlparse, unquote
from zoneinfo import ZoneInfo
from email.utils import formatdate
from xml.sax.saxutils import escape

import feedparser
import requests
from PIL import Image, ImageDraw, ImageFont

GEMINI_KEY = os.environ["GEMINI_API_KEY"]
FB_TOKEN = os.environ.get("FB_PAGE_TOKEN", "").strip()
FB_PAGE_ID = os.environ.get("FB_PAGE_ID", "").strip()

# একাধিক পেজের জন্য প্রোফাইল (PROFILE এনভায়রনমেন্ট ভ্যালু দিয়ে বাছাই হয়)
PROFILE = (os.environ.get("PROFILE", "sports") or "sports").strip()
PROFILES = {
    "sports": dict(feeds="feeds.txt", state="posted.json", rss="feed.xml", logo="logo.png",
                   accent=(230, 40, 50), role="বাংলা ক্রীড়া সংবাদকর্মী", extra="",
                   skip=("dream 11", "dream11", "fantasy", "prediction", "betting",
                         "ফ্যান্টাসি", "ড্রিম ১১", "ড্রিম১১", "ভবিষ্যদ্বাণী")),
    "general": dict(feeds="feeds_general.txt", state="posted_general.json", rss="feed_general.xml",
                    logo="logo_general.png", accent=(0, 120, 215), role="বাংলা সংবাদকর্মী",
                    extra="অপরাধ, রাজনীতি, ধর্ম বা সংবেদনশীল বিষয়ে নিরপেক্ষ ভাষা ব্যবহার করবে। অনিশ্চিত দাবি বা গুজবকে নিশ্চিত তথ্য হিসেবে লিখবে না।\n",
                    skip=("betting", "casino", "জুয়া")),
}
CFG = PROFILES.get(PROFILE, PROFILES["sports"])
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "").strip()
MAX_POSTS = int(os.environ.get("MAX_POSTS", "1"))
PUBLISH = os.environ.get("PUBLISH", "false").lower() == "true"
USE_SOURCE_IMAGE = os.environ.get("USE_SOURCE_IMAGE", "true").lower() == "true"

LOGO_PATH = CFG["logo"] if os.path.exists(CFG["logo"]) else "logo.png"
STATE_FILE = CFG["state"]
UA = {"User-Agent": "Mozilla/5.0 (compatible; NewsCardBot/1.0)"}
BROWSER = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "bn-BD,bn;q=0.9,en;q=0.8",
}
BN = str.maketrans("0123456789", "০১২৩৪৫৬৭৮৯")
_working_model = None


class GeminiError(Exception):
    pass


class FBError(Exception):
    pass


def find_font():
    files = sorted(glob.glob("fonts/*.ttf") + glob.glob("fonts/*.otf"))
    bold = [f for f in files if "bold" in f.lower() and "semi" not in f.lower() and "extra" not in f.lower()]
    return (bold or files or [None])[0]


FONT_PATH = find_font()


def load_state():
    try:
        return json.load(open(STATE_FILE, encoding="utf-8"))
    except Exception:
        return []


def save_state(s):
    json.dump(s[-500:], open(STATE_FILE, "w", encoding="utf-8"))


def clean(t):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", t or ""))).strip()



def fetch_text(url, via_proxy=False):
    """সরাসরি পেজ আনে; আটকে গেলে (403 ইত্যাদি) r.jina.ai রিডার দিয়ে চেষ্টা করে।"""
    if not via_proxy:
        try:
            r = requests.get(url, headers=BROWSER, timeout=25)
            if r.status_code == 200:
                return r.content.decode("utf-8", "replace")
            print(f"direct fetch {r.status_code}: {url[:70]}")
        except Exception as e:
            print("direct fetch error:", url[:70], e)
    try:
        r = requests.get("https://r.jina.ai/" + url,
                         headers={"Accept": "text/plain"}, timeout=75)
        if r.status_code == 200 and len(r.text) > 500:
            print("via proxy ok:", url[:70])
            return r.content.decode("utf-8", "replace")
        print(f"proxy fetch {r.status_code} len={len(r.text)}: {url[:70]}")
    except Exception as e:
        print("proxy fetch error:", url[:70], e)
    return None


# ---------- ছবি সংগ্রহ ----------
def entry_image(e):
    cands = []

    def w(m):
        try:
            return int(m.get("width") or 0)
        except Exception:
            return 0

    for key in ("media_content", "media_thumbnail"):
        for m in e.get(key, []) or []:
            if m.get("url"):
                cands.append((w(m), m["url"]))
    for l in (e.get("links", []) or []) + (e.get("enclosures", []) or []):
        if str(l.get("type", "")).startswith("image") and l.get("href"):
            cands.append((0, l["href"]))
    blob = (e.get("summary") or "") + " ".join(c.get("value", "") for c in (e.get("content", []) or []))
    m = re.search(r'<img[^>]+src=["\']([^"\']+)', blob)
    if m:
        cands.append((0, m.group(1)))
    if cands:
        cands.sort(key=lambda x: x[0], reverse=True)
        return cands[0][1]
    return None


def og_image(link):
    t = fetch_text(link)
    if not t:
        return None
    m = (re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', t)
         or re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', t))
    return html.unescape(m.group(1)) if m else None


def download(url):
    try:
        r = requests.get(url, headers=BROWSER, timeout=25)
        r.raise_for_status()
        return Image.open(io.BytesIO(r.content)).convert("RGB")
    except Exception as ex:
        print("image skip:", url[:80], ex)
        return None


def get_photo(item):
    if not USE_SOURCE_IMAGE:
        return None
    best = None
    u = item.get("image")
    if u and re.search(r"logo|thumbnail\.jpg|default", u, re.I):
        u = None
    if u:
        cand = [u]
        if "bbci.co.uk" in u:
            up = re.sub(r"/standard/\d+/", "/standard/976/", u)
            if up != u:
                cand = [up, u]
        for c in cand:
            img = download(c)
            if img and img.width >= 600:
                return img
            best = best or img
    og = og_image(item["link"])
    if og and re.search(r"logo|thumbnail\.jpg|default", og, re.I):
        og = None
    if og:
        img = download(og)
        if img and img.width >= 600:
            return img
        best = best or img
    return best if best and best.width >= 300 else None



# ---------- RSS ছাড়া সাইট (পেজ স্ক্র্যাপ) ----------
SKIP_WORDS = CFG["skip"]
SITE_NAMES = {"tsports.com": "টি স্পোর্টস", "bdcrictime.com": "BDCricTime", "prothomalo.com": "প্রথম আলো",
              "jagonews24.com": "জাগো নিউজ ২৪", "feeds.bbci.co.uk": "BBC Sport", "theguardian.com": "The Guardian",
              "skysports.com": "Sky Sports", "espn.co.uk": "ESPN", "cricinfo.com": "ESPNcricinfo",
              "banglatribune.com": "বাংলা ট্রিবিউন", "bd24live.com": "বিডি২৪লাইভ", "risingbd.com": "রাইজিংবিডি",
              "daily-bangladesh.com": "ডেইলি বাংলাদেশ", "ajkerpatrika.com": "আজকের পত্রিকা",
              "bd-journal.com": "বাংলাদেশ জার্নাল"}
TS_SECTIONS = ("football", "cricket", "hocky", "tenis", "others", "fifa-world-cup")
BDC_SKIP = {"news", "series", "player", "team", "teams", "photos", "videos", "fixtures", "result", "calendar",
            "live-scores", "players", "icc-rankings", "category", "about-us", "contact", "faq", "dmca",
            "privacy-policy", "careers", "advertise-with-us", "write-for-us", "internship", "terms-conditions"}


def host_of(url):
    h = urlparse(url).netloc.lower()
    return h[4:] if h.startswith("www.") else h


def is_article(url):
    p = urlparse(url)
    host = host_of(url)
    parts = [x for x in unquote(p.path).strip("/").split("/") if x]
    if host == "tsports.com":
        return len(parts) >= 3 and parts[0] in TS_SECTIONS and parts[1].isdigit()
    if host == "prothomalo.com":
        last = parts[-1] if parts else ""
        return (parts and parts[0] == "sports" and len(parts) in (2, 3)
                and re.fullmatch(r"[a-z0-9]{8,12}", last) is not None and any(c.isdigit() for c in last))
    if host.endswith("bdcrictime.com"):
        slug = parts[0] if len(parts) == 1 else ""
        bad = any(w in slug for w in ("dream-11", "dream11", "fantasy", "prediction"))
        return len(parts) == 1 and slug not in BDC_SKIP and slug.count("-") >= 4 and not bad
    # অন্য যেকোনো সাইট: লিংকের ধরন দেখে আর্টিকেল চেনার সাধারণ নিয়ম
    if not parts or len(parts) < 2 and parts[0].count("-") < 4:
        return False
    if any(w in parts for w in ("tag", "tags", "category", "author", "video", "videos", "photo", "gallery",
                                "page", "search", "topic", "topics", "about", "contact", "epaper", "archive")):
        return False
    last = parts[-1]
    return bool(re.search(r"\d{5,}", last) or last.count("-") >= 4 or len(last) >= 28
                or (len(parts) >= 2 and re.search(r"\d{5,}", "/".join(parts))))


def meta(t, prop):
    m = (re.search(r'<meta[^>]+(?:property|name)=["\']%s["\'][^>]+content=["\']([^"\']*)' % re.escape(prop), t, re.I)
         or re.search(r'<meta[^>]+content=["\']([^"\']*)["\'][^>]+(?:property|name)=["\']%s["\']' % re.escape(prop), t, re.I))
    return clean(m.group(1)) if m else ""


IMG_RE = re.compile(r"!\[[^\]]*\]\((https?://[^)\s]+)\)")


def parse_markdown_article(t):
    m = re.search(r"^Title:\s*(.+)$", t, re.M)
    title = clean(m.group(1)) if m else ""
    title = re.split(r"\s+[|\-–]\s+(প্রথম আলো|Prothom Alo|T Sports|TSports|টি স্পোর্টস).*$", title)[0].strip()
    ts = 0
    m = re.search(r"^Published Time:\s*(.+)$", t, re.M)
    if m:
        try:
            ts = datetime.fromisoformat(m.group(1).strip().replace("Z", "+00:00")).timestamp()
        except Exception:
            ts = 0
    content = t.split("Markdown Content:", 1)[-1]
    imgs = [u for u in IMG_RE.findall(content)
            if re.search(r"news_photos|media\.prothomalo|\.(jpe?g|png|webp)", u, re.I)
            and not re.search(r"logo|icon|avatar|advert|search", u, re.I)]
    image = next((u for u in imgs if re.search(r"news_photos|media\.prothomalo", u)), imgs[0] if imgs else None)
    text = IMG_RE.sub("", content)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    lines = [l.strip() for l in text.splitlines() if len(l.strip()) > 45 and not l.strip().startswith(("#", "|", "-", "*"))]
    return {"title": title, "summary": " ".join(lines)[:1500], "image": image, "ts": ts}


def parse_article(t):
    if t.lstrip().startswith("Title:") or "Markdown Content:" in t[:1500]:
        return parse_markdown_article(t)
    title = meta(t, "og:title")
    if not title:
        m = re.search(r"<title>(.*?)</title>", t, re.S | re.I)
        title = clean(m.group(1)) if m else ""
    desc = meta(t, "og:description") or meta(t, "description")
    paras = " ".join(clean(p) for p in re.findall(r"<p[^>]*>(.*?)</p>", t, re.S | re.I))
    body = paras if len(paras) > len(desc) else desc
    ts = 0
    pub = meta(t, "article:published_time")
    if pub:
        try:
            ts = datetime.fromisoformat(pub.replace("Z", "+00:00")).timestamp()
        except Exception:
            ts = 0
    return {"title": title, "summary": body[:1500], "image": meta(t, "og:image") or None, "ts": ts}


def fetch_article(link):
    t = fetch_text(link)
    art = parse_article(t) if t else None
    if not art or not art["title"]:
        t = fetch_text(link, via_proxy=True)
        art = parse_article(t) if t else None
    return art


def extract_links(t, url):
    raw = re.findall(r'href=["\']([^"\']+)["\']', t)
    raw += re.findall(r"\]\((https?://[^)\s]+)\)", t)
    raw += re.findall(r"https?://[^\s)\]\"'<>]+", t)
    links, seen = [], set()
    for h in raw:
        full = urljoin(url, html.unescape(h)).split("#")[0].split("?")[0].rstrip(".,;")
        if full in seen or not is_article(full):
            continue
        seen.add(full)
        links.append(full)
    return links


def scrape_page(url, state, limit=6):
    out = []
    t = fetch_text(url)
    links = extract_links(t, url) if t else []
    if not links:
        print("সরাসরি লিংক মেলেনি, প্রক্সি দিয়ে চেষ্টা:", url)
        t = fetch_text(url, via_proxy=True)
        links = extract_links(t, url) if t else []
        if not links:
            print("DEBUG: প্রক্সি-পেজ দৈর্ঘ্য =", len(t or ""), "| শুরু:", repr((t or "")[:250]))
    print(f"{url}: {len(links)} article links")
    src = SITE_NAMES.get(host_of(url), host_of(url))
    for i, link in enumerate(links):
        if len(out) >= limit:
            break
        uid = hashlib.md5(link.encode()).hexdigest()
        if uid in state:
            continue
        art = fetch_article(link)
        if not art or not art["title"] or any(w in art["title"].lower() for w in SKIP_WORDS):
            continue
        out.append({"uid": uid, "link": link, "source": src, "title": art["title"],
                    "summary": art["summary"], "image": art["image"],
                    "ts": art["ts"] or (time.time() - i * 60)})
    return out

# ---------- খবর সংগ্রহ ----------
def collect(state):
    feeds = [l.strip() for l in open(CFG["feeds"], encoding="utf-8") if l.strip() and not l.startswith("#")]
    items = []
    for line in feeds:
        url, _, flt = line.partition("|")
        url = url.strip()
        filters = [x.strip() for x in flt.split(",") if x.strip()]
        if url.startswith("page:"):
            items += scrape_page(url[5:].strip(), state)
            continue
        try:
            d = feedparser.parse(url)
        except Exception as e:
            print("feed error", url, e)
            continue
        src = SITE_NAMES.get(host_of(url)) or clean(d.feed.get("title", url))
        if "bbci.co.uk" in url:
            src = "BBC Sport" if "/sport/" in url else "BBC News"
        before = len(items)
        for e in d.entries:
            link = e.get("link", "")
            if filters and not any(f in link for f in filters):
                continue
            if any(w in (e.get("title") or "").lower() for w in SKIP_WORDS):
                continue
            uid = hashlib.md5((e.get("id") or link).encode()).hexdigest()
            if uid in state:
                continue
            ts = e.get("published_parsed") or e.get("updated_parsed")
            items.append({
                "uid": uid, "link": link, "source": src,
                "title": clean(e.get("title")), "summary": clean(e.get("summary"))[:1200],
                "image": entry_image(e),
                "ts": time.mktime(ts) if ts else 0,
            })
        print(f"{url}: {len(items) - before} new items")
    items.sort(key=lambda x: x["ts"], reverse=True)
    return items


# ---------- Gemini ----------
def candidate_models():
    if GEMINI_MODEL:
        return [GEMINI_MODEL]
    cands = ["gemini-flash-latest"]
    try:
        r = requests.get(
            "https://generativelanguage.googleapis.com/v1beta/models?pageSize=200",
            headers={"x-goog-api-key": GEMINI_KEY}, timeout=30)
        names = []
        for m in r.json().get("models", []):
            n = m["name"].replace("models/", "")
            ok = "generateContent" in m.get("supportedGenerationMethods", [])
            bad = any(x in n for x in ("image", "tts", "live", "audio", "embed", "robotics", "computer", "customtools"))
            if ok and "flash" in n and not bad and n not in cands:
                names.append(n)
        cands += sorted(names, reverse=True)[:5]
    except Exception as e:
        print("model list error:", e)
    print("model candidates:", cands)
    return cands


def rewrite(item):
    global _working_model
    prompt = f"""তুমি একজন {CFG['role']}। নিচের খবরের শিরোনাম ও সারাংশ থেকে নিজের ভাষায় বাংলায় লেখো।
কঠোর নিয়ম: শুধু নিচের তথ্য ব্যবহার করবে, নিজে থেকে কোনো তথ্য, সংখ্যা বা উদ্ধৃতি যোগ করবে না। মূল লেখার বাক্য হুবহু কপি করবে না।
{CFG['extra']}JSON আকারে শুধু এই দুটি ফিল্ড দাও:
{{"headline": "সর্বোচ্চ ৭০ অক্ষরের আকর্ষণীয় বাংলা শিরোনাম", "caption": "২-৩ বাক্যের বাংলা সারসংক্ষেপ (সর্বোচ্চ ৪০০ অক্ষর)"}}

শিরোনাম: {item['title']}
সারাংশ: {item['summary']}"""
    r = None
    done = False
    for attempt in range(3):
        models = [_working_model] if _working_model else candidate_models()
        for model in models:
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={"x-goog-api-key": GEMINI_KEY, "Content-Type": "application/json"},
                json={"contents": [{"parts": [{"text": prompt}]}],
                      "generationConfig": {"responseMimeType": "application/json", "temperature": 0.4}},
                timeout=90,
            )
            if r.status_code == 200:
                _working_model = model
                done = True
                break
            if r.status_code in (404, 429, 500, 503):
                print(f"model {model} -> {r.status_code}, trying next/retry")
                _working_model = None
                continue
            raise GeminiError(f"Gemini {r.status_code} ({model}): {r.text[:400]}")
        if done:
            break
        time.sleep(15 * (attempt + 1))
    if not done:
        raise GeminiError("Gemini busy/unavailable: " + (r.text[:300] if r is not None else ""))
    txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    data = json.loads(txt)
    return data["headline"].strip(), data["caption"].strip()


# ---------- ফটোকার্ড ----------
def wrap(draw, text, font, maxw):
    lines, cur = [], ""
    for w in text.split():
        t = (cur + " " + w).strip()
        if draw.textlength(t, font=font) <= maxw:
            cur = t
        else:
            lines.append(cur)
            cur = w
    lines.append(cur)
    return lines


def make_font(sz):
    try:
        return ImageFont.truetype(FONT_PATH, sz, layout_engine=ImageFont.Layout.RAQM)
    except Exception:
        return ImageFont.truetype(FONT_PATH, sz)


def tw(d, text, font):
    return d.textlength(text, font=font)


def shadow_text(d, xy, text, font, fill, shadow=(0, 0, 0)):
    x, y = xy
    d.text((x + 3, y + 3), text, font=font, fill=shadow)
    d.text((x, y), text, font=font, fill=fill)


def overlay_round(img, box, radius, fill):
    ov = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(ov).rounded_rectangle(box, radius, fill=fill)
    return Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB")


def make_qr(url, size):
    try:
        import qrcode
        q = qrcode.QRCode(box_size=6, border=1)
        q.add_data(url)
        q.make(fit=True)
        return q.make_image(fill_color="black", back_color="white").convert("RGB").resize((size, size))
    except Exception as e:
        print("qr skipped:", e)
        return None


def make_card(headline, photo=None, credit="", link=""):
    """৪:৫ (1080x1350) নিউজ ফটোকার্ড: উপরে ছবি, মাঝে হেডলাইন প্যানেল, নিচে ব্র্যান্ড বার।"""
    W, H = 1080, 1350
    PH, BAR = 700, 150
    p_top, p_bot = PH, H - BAR
    accent = CFG["accent"]

    img = Image.new("RGB", (W, H), (8, 20, 90))
    g = ImageDraw.Draw(img)
    if photo:
        ratio = max(W / photo.width, PH / photo.height)
        ph = photo.resize((int(photo.width * ratio) + 1, int(photo.height * ratio) + 1), Image.LANCZOS)
        x = (ph.width - W) // 2
        y = int((ph.height - PH) * 0.25)  # মুখ যেন কাটা না পড়ে, তাই উপরের দিকে ঝুঁকে ক্রপ
        img.paste(ph.crop((x, y, x + W, y + PH)), (0, 0))
    else:
        for yy in range(PH):
            c = int(30 + 70 * yy / PH)
            g.line([(0, yy), (W, yy)], fill=(15, c // 2 + 15, 90 + c // 2))
    for yy in range(p_top, p_bot):  # হেডলাইন প্যানেল
        t = (yy - p_top) / (p_bot - p_top)
        g.line([(0, yy), (W, yy)], fill=(int(6 + 4 * t), int(30 - 12 * t), int(125 - 55 * t)))
    for yy in range(p_bot, H):  # নিচের ব্র্যান্ড বার
        t = (yy - p_bot) / BAR
        g.line([(0, yy), (W, yy)], fill=(int(48 - 18 * t), 14, int(78 - 22 * t)))
    g.rectangle([0, PH - 4, W, PH + 4], fill=accent)

    d = ImageDraw.Draw(img)
    now = datetime.now(ZoneInfo("Asia/Dhaka"))
    months = ["জানুয়ারি", "ফেব্রুয়ারি", "মার্চ", "এপ্রিল", "মে", "জুন", "জুলাই", "আগস্ট", "সেপ্টেম্বর", "অক্টোবর", "নভেম্বর", "ডিসেম্বর"]
    date_txt = f"{now.day} {months[now.month-1]} {now.year}".translate(BN)

    # হেডলাইন: প্রথম লাইন সাদা, বাকি লাইন হলুদ
    avail_top, avail_bot = p_top + 62, p_bot - 105
    size = 88
    while True:
        f = make_font(size)
        lines = wrap(d, headline, f, W - 130)
        line_h = int(size * 1.32)
        if len(lines) * line_h <= (avail_bot - avail_top) or size <= 40:
            break
        size -= 4
    block = len(lines) * line_h
    y = avail_top + max(0, ((avail_bot - avail_top) - block) // 2)
    for i, ln in enumerate(lines):
        color = (255, 255, 255) if (i == 0 and len(lines) > 1) else (255, 226, 40)
        d.text(((W - tw(d, ln, f)) / 2, y), ln, font=f, fill=color)
        y += line_h

    # তারিখের ফিতা
    df = make_font(30)
    dw = tw(d, date_txt, df)
    d.rounded_rectangle([W / 2 - dw / 2 - 34, PH - 28, W / 2 + dw / 2 + 34, PH + 30], 6,
                        fill=(accent[0] // 2 + 40, accent[1] // 3, accent[2] // 2 + 20), outline=(255, 255, 255), width=2)
    d.text((W / 2 - dw / 2, PH - 22), date_txt, font=df, fill="white")

    # ছবির ক্রেডিট
    if photo and credit:
        cf = make_font(26)
        shadow_text(d, (40, PH - 78), f"ছবি: {credit[:30]}", cf, (255, 255, 255))

    # লোগো (উপরে বাঁয়ে)
    try:
        logo = Image.open(LOGO_PATH).convert("RGBA")
        logo.thumbnail((230, 120))
        img = overlay_round(img, [30, 28, 70 + logo.width, 72 + logo.height], 18, (0, 0, 0, 105))
        img.paste(logo, (50, 50), logo)
        d = ImageDraw.Draw(img)
    except Exception as e:
        print("logo skipped:", e)

    # ফুটার সারি: ওয়েবসাইট | বিস্তারিত কমেন্টে | QR
    fy = p_bot - 82
    host = host_of(link) if link else ""
    if host:
        d.ellipse([60, fy + 4, 92, fy + 36], outline="white", width=3)
        d.ellipse([69, fy + 4, 83, fy + 36], outline="white", width=2)
        d.line([60, fy + 20, 92, fy + 20], fill="white", width=2)
        d.text((104, fy), host[:26], font=make_font(34), fill="white")
    pill_f = make_font(30)
    pt = "বিস্তারিত কমেন্টে"
    pw = tw(d, pt, pill_f)
    px = W / 2 - pw / 2 + (30 if host else 0)
    img = overlay_round(img, [px - 28, fy - 6, px + pw + 28, fy + 48], 26, (255, 255, 255, 38))
    d = ImageDraw.Draw(img)
    d.text((px, fy), pt, font=pill_f, fill="white")
    page_url = os.environ.get("PAGE_URL", "").strip()
    if page_url:
        qr = make_qr(page_url, 118)
        if qr:
            img.paste(qr, (W - 60 - 118, p_bot - 138))

    # নিচের ব্র্যান্ড বার
    page_name = os.environ.get("PAGE_NAME", "").strip()
    follow = os.environ.get("FOLLOW_TEXT", "আরও খবরের জন্য পেজটি ফলো করুন").strip()
    if page_name:
        nf = make_font(46)
        d.text(((W - tw(d, page_name, nf)) / 2, p_bot + 22), page_name, font=nf, fill=(255, 226, 40))
        ff = make_font(28)
        d.text(((W - tw(d, follow, ff)) / 2, p_bot + 88), follow, font=ff, fill=(235, 235, 245))
    else:
        ff = make_font(34)
        d.text(((W - tw(d, follow, ff)) / 2, p_bot + 52), follow, font=ff, fill=(255, 226, 40))

    buf = io.BytesIO()
    img.save(buf, "PNG")
    buf.seek(0)
    return buf


# ---------- Facebook ----------
def post_fb(png, caption):
    r = requests.post(
        f"https://graph.facebook.com/v19.0/{FB_PAGE_ID}/photos",
        data={"caption": caption, "access_token": FB_TOKEN, "published": "true" if PUBLISH else "false"},
        files={"source": ("card.png", png, "image/png")},
        timeout=90,
    )
    print("facebook:", r.status_code, r.text[:300])
    if r.status_code != 200:
        raise FBError(f"Facebook {r.status_code}: {r.text[:300]}")
    j = r.json()
    return j.get("post_id") or j.get("id")


def comment_fb(post_id, text):
    try:
        r = requests.post(
            f"https://graph.facebook.com/v19.0/{post_id}/comments",
            data={"message": text, "access_token": FB_TOKEN},
            timeout=60,
        )
        print("comment:", r.status_code, r.text[:300])
        if r.status_code != 200:
            print("COMMENT ERROR: কমেন্ট হয়নি। টোকেনে pages_manage_engagement পারমিশন লাগতে পারে।")
    except Exception as e:
        print("COMMENT ERROR:", e)


def write_rss(items, path=None):
    path = path or CFG["rss"]
    """সংগৃহীত খবর থেকে একটি আসল RSS ফাইল বানায়।"""
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<rss version="2.0"><channel>',
             "<title>Sports News Feed</title>",
             "<link>https://example.com</link>",
             "<description>সর্বশেষ খবর</description>",
             "<language>bn</language>",
             f"<lastBuildDate>{formatdate(time.time())}</lastBuildDate>"]
    for it in items[:30]:
        parts.append("<item>")
        parts.append(f"<title>{escape(it['title'])}</title>")
        parts.append(f"<link>{escape(it['link'])}</link>")
        parts.append(f"<guid isPermaLink=\"true\">{escape(it['link'])}</guid>")
        parts.append(f"<description>{escape(it['summary'][:300])}</description>")
        parts.append(f"<source url=\"{escape(it['link'])}\">{escape(it['source'])}</source>")
        if it.get("ts"):
            parts.append(f"<pubDate>{formatdate(it['ts'])}</pubDate>")
        if it.get("image"):
            parts.append(f"<enclosure url=\"{escape(it['image'])}\" type=\"image/jpeg\" length=\"0\"/>")
        parts.append("</item>")
    parts.append("</channel></rss>")
    open(path, "w", encoding="utf-8").write("\n".join(parts))
    print("rss written:", min(len(items), 30), "items ->", path)


def main():
    print("profile:", PROFILE)
    if not FB_TOKEN or not FB_PAGE_ID:
        print("এই পেজের FB_PAGE_ID / FB_PAGE_TOKEN সেট করা নেই, তাই বাদ দেওয়া হলো।")
        return
    if not FONT_PATH:
        print("FONT ERROR: fonts ফোল্ডারে কোনো .ttf ফন্ট নেই। Noto Sans Bengali ফন্ট আপলোড করুন।")
        return
    print("font:", FONT_PATH)
    state = load_state()
    posted = 0
    tries = 0
    items = collect(state)
    try:
        write_rss(items)
    except Exception as e:
        print("rss error:", e)
    for item in items:
        if posted >= MAX_POSTS or tries >= 6:
            break
        tries += 1
        try:
            headline, caption = rewrite(item)
            photo = get_photo(item)
            print("photo:", "yes" if photo else "no (gradient card)")
            card = make_card(headline, photo, item["source"] if photo else "", item["link"])
            text = f"{headline}\n\n{caption}\n\nসূত্র: {item['source']}"
            if photo:
                text += f"\nছবি: {item['source']}"
            post_id = post_fb(card, text)
            if post_id and PUBLISH:
                time.sleep(5)
                comment_fb(post_id, f"বিস্তারিত: {item['link']}")
            state.append(item["uid"])
            posted += 1
            time.sleep(10)
        except FBError as e:
            print("FACEBOOK ERROR (stopping):", e)
            break
        except GeminiError as e:
            print("GEMINI ERROR (stopping):", e)
            break
        except Exception as e:
            print("failed:", item["title"][:50], e)
    save_state(state)
    print("posted:", posted)


if __name__ == "__main__":
    main()
