#!/usr/bin/env python3
"""
Apple Newsroom -> Telegram

Controlla il feed ufficiale di Apple Newsroom ogni 10 secondi. Appena esce un
nuovo articolo ti manda su Telegram:
  1. subito: titolo + sommario + link
  2. un collage verticale 9:16 con le prime 3 foto orizzontali (serve Pillow)
  3. tutte le foto dell'articolo in alta qualità (come file, senza compressione)
  4. i video dell'articolo (H.264, sotto i 50 MB di Telegram; serve ffmpeg)
  5. i link per scaricare tutti i media (zip)

In chat: /config per scegliere il separatore del collage (nessuno, nero, hex).

Solo Python 3 (già su macOS). Per il collage: pip3 install pillow (opzionale).

Uso:
  python3 bot.py              avvia il monitoraggio
  python3 bot.py --chat-id    mostra il tuo chat id (dopo aver scritto al bot)
  python3 bot.py --test       invia l'ultimo articolo già uscito per provare tutto
"""
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

BASE = Path(__file__).resolve().parent


def load_env():
    p = BASE / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
INTERVAL = float(os.environ.get("POLL_SECONDS", "10"))
SEND_AS = os.environ.get("SEND_AS", "document").lower()  # document | photo
API = os.environ.get("TELEGRAM_API", "https://api.telegram.org")
FEED_URL = os.environ.get("FEED_URL", "https://www.apple.com/newsroom/rss-feed.rss")
SITE = "https://www.apple.com"
STATE_FILE = BASE / "seen.json"
SETTINGS_FILE = BASE / "settings.json"
FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.0 Safari/605.1.15")
ATOM = "{http://www.w3.org/2005/Atom}"


def log(*a):
    print(datetime.now().strftime("%H:%M:%S"), *a, flush=True)


# --------------------------------------------------------------------- HTTP
def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def download(url, tries=4):
    err = None
    for i in range(tries):
        try:
            return http_get(url, timeout=40)
        except Exception as e:  # noqa: BLE001
            err = e
            time.sleep(2 * (i + 1))
    raise err


# --------------------------------------------------------------------- feed
def fetch_feed():
    try:
        data = http_get(f"{FEED_URL}?t={int(time.time())}")  # aggira cache CDN
    except urllib.error.HTTPError:
        data = http_get(FEED_URL)
    root = ET.fromstring(data)
    out = []
    for en in root.iter(ATOM + "entry"):
        link = ""
        for l in en.findall(ATOM + "link"):
            if l.get("rel") in (None, "alternate"):
                link = l.get("href", "")
                break

        def t(tag, en=en):
            x = en.find(ATOM + tag)
            return (x.text or "").strip() if x is not None else ""

        cat = en.find(ATOM + "category")
        out.append({
            "id": t("id") or link,
            "title": t("title"),
            "link": link,
            "updated": t("updated"),
            "category": cat.get("term", "") if cat is not None else "",
            "summary": t("content"),
        })
    return out


# ------------------------------------------------------------ article media
IMG_RE = re.compile(
    r'(?:https?://www\.apple\.com)?'
    r'(/newsroom/images/\d{4}/\d{2}/[^"\'\s)<>,]+?\.(?:jpe?g|png|webp))'
    r'(?=["\'\s)<>,]|$)', re.I)
VIDEO_RE = re.compile(
    r'(?:https?://www\.apple\.com)?(/newsroom/videos/[^"\'\s)<>,&]+?\.(?:mp4|m3u8))'
    r'(?=["\'\s)<>,&]|$)', re.I)
ZIP_RE = re.compile(
    r'(?:https?://www\.apple\.com)?(/newsroom/[^"\'\s)<>,]+?\.zip)(?=["\'\s)<>,]|$)', re.I)


def img_key(path):
    name = path.rsplit("/", 1)[-1]
    name = re.sub(r'\.(large|medium|small)(_2x)?\.(jpe?g|png|webp)$', '', name, flags=re.I)
    name = re.sub(r'(_big|_medium|_small)?\.(jpe?g|png|webp)$', '', name, flags=re.I)
    return name


def img_rank(path):
    m = re.search(r'\.(large|medium|small)(_2x)?\.(?:jpe?g|png|webp)$', path, re.I)
    if not m:
        return 6  # file "nudo", il più grande
    base = {"large": 4, "medium": 2, "small": 0}[m.group(1).lower()]
    return base + (1 if m.group(2) else 0)


class AltParser(HTMLParser):
    """Raccoglie le didascalie (alt) delle immagini, per chiave."""

    def __init__(self):
        super().__init__()
        self.alts = {}

    def handle_starttag(self, tag, attrs):
        if tag != "img":
            return
        a = dict(attrs)
        alt = (a.get("alt") or "").strip()
        for v in (a.get("src"), a.get("data-src"), a.get("srcset"), a.get("data-srcset")):
            for m in IMG_RE.finditer(v or ""):
                self.alts.setdefault(img_key(m.group(1)), alt)


def extract_media(page_html, link):
    slug = link.rstrip("/").split("/")[-1]
    ap = AltParser()
    try:
        ap.feed(page_html)
    except Exception:  # noqa: BLE001
        pass

    def collect(allow_tile):
        cands, order = {}, []
        for m in IMG_RE.finditer(page_html):
            path = m.group(1)
            low = path.lower()
            if f"/{slug}/" not in path:
                continue
            is_tile = "/tile/" in low or ".og." in low
            if is_tile != allow_tile:
                continue
            k = img_key(path)
            if k not in cands:
                order.append(k)
                cands[k] = set()
            cands[k].add(path)
        # per ogni foto: varianti ordinate dalla migliore alla peggiore
        return [(k, sorted(cands[k], key=img_rank, reverse=True)) for k in order]

    found = collect(False) or collect(True)  # se non c'è altro, almeno la cover
    images = [{"key": k, "url": SITE + ps[0], "fallbacks": [SITE + p for p in ps[1:]],
               "alt": ap.alts.get(k, "")} for k, ps in found]

    zips = []
    for m in ZIP_RE.finditer(page_html):
        u = SITE + m.group(1)
        if u not in zips:
            zips.append(u)
    all_media = next((z for z in zips if z.rsplit("/", 1)[-1].startswith("Media-of-")), None)
    videos = [z for z in zips if "/videos/" in z]
    clips = []
    for m in VIDEO_RE.finditer(page_html):
        u = SITE + m.group(1)
        if u not in clips:
            clips.append(u)
    return {"images": images, "all_media": all_media, "videos": videos, "clips": clips}


# ----------------------------------------------------------------- telegram
def multipart(fields, files):
    boundary = uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"'
                      f'\r\n\r\n{v}\r\n').encode("utf-8"))
    for name, fname, data, ctype in files:
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
                      f'filename="{fname}"\r\nContent-Type: {ctype}\r\n\r\n').encode("utf-8")
                     + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def tg(method, fields=None, files=None, tries=5, need_chat=True):
    url = f"{API}/bot{TOKEN}/{method}"
    fields = dict(fields or {})
    if need_chat:
        fields.setdefault("chat_id", CHAT_ID)
    for _ in range(tries):
        if files:
            body, ctype = multipart(fields, files)
        else:
            body = urllib.parse.urlencode(fields).encode()
            ctype = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=body, headers={"Content-Type": ctype})
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                ra = json.loads(raw).get("parameters", {}).get("retry_after")
            except Exception:  # noqa: BLE001
                ra = None
            if e.code == 429 and ra:
                time.sleep(ra + 1)
                continue
            if e.code >= 500:
                time.sleep(2)
                continue
            raise RuntimeError(f"Telegram {method} {e.code}: {raw[:300]}") from None
        except (urllib.error.URLError, TimeoutError):
            time.sleep(2)
    raise RuntimeError(f"Telegram {method}: troppi tentativi")


def esc(s):
    return html.escape(s or "", quote=False)


def send_text(text, **extra):
    tg("sendMessage", {"text": text, "parse_mode": "HTML",
                       "disable_web_page_preview": "true", **extra})


def ctype_of(fn):
    return "image/png" if fn.lower().endswith(".png") else (
        "image/webp" if fn.lower().endswith(".webp") else "image/jpeg")


def send_images(items):
    """items: [(filename, bytes, caption)] -> album da 10 (file originali)."""
    kind = "photo" if SEND_AS == "photo" else "document"
    for i in range(0, len(items), 10):
        chunk = items[i:i + 10]
        if len(chunk) == 1:
            fn, data, cap = chunk[0]
            tg("sendPhoto" if kind == "photo" else "sendDocument",
               {"caption": cap, "parse_mode": "HTML"},
               [(kind, fn, data, ctype_of(fn))])
            continue
        media, files = [], []
        for j, (fn, data, cap) in enumerate(chunk):
            media.append({"type": kind, "media": f"attach://f{j}",
                          "caption": cap, "parse_mode": "HTML"})
            files.append((f"f{j}", fn, data, ctype_of(fn)))
        tg("sendMediaGroup", {"media": json.dumps(media)}, files)


# ------------------------------------------------------------------ collage
try:
    from PIL import Image, ImageOps
    import io
except ImportError:  # collage disattivato finché non installi Pillow
    Image = None

COLLAGE_W = int(os.environ.get("COLLAGE_WIDTH", "1080"))       # altezza = W*16/9
COLLAGE_GAP = int(os.environ.get("COLLAGE_GAP", "8"))          # px del separatore, se attivo (/config)
COLLAGE_ON = os.environ.get("COLLAGE", "1") not in ("0", "false", "no")


def make_collage(datas):
    """datas: [bytes] nell'ordine dell'articolo -> JPEG 9:16 con 3 fasce orizzontali.

    Scelta: le prime 3 foto orizzontali dell'articolo (in ordine di apparizione);
    se ce ne sono meno di 3, completa con le prime altre foto trovate.
    """
    if Image is None or not COLLAGE_ON:
        return None
    imgs = []
    for d in datas:
        try:
            im = ImageOps.exif_transpose(Image.open(io.BytesIO(d)))
            im.load()
            if im.mode != "RGB":
                bg = Image.new("RGB", im.size, (0, 0, 0))
                im = im.convert("RGBA")
                bg.paste(im, mask=im.split()[3])
                im = bg
            imgs.append(im)
        except Exception as e:  # noqa: BLE001
            log("immagine non leggibile per il collage:", e)
    land = [im for im in imgs if im.width >= im.height * 1.15]
    rest = [im for im in imgs if im not in land]
    chosen = (land[:3] + rest)[:3]
    if len(chosen) < 2:
        return None
    n = len(chosen)
    W, H = COLLAGE_W, COLLAGE_W * 16 // 9
    sep = SETTINGS.get("sep")  # None = foto attaccate, altrimenti "#RRGGBB"
    gap = COLLAGE_GAP if sep else 0
    usable = H - gap * (n - 1)
    heights = [usable // n + (1 if i < usable % n else 0) for i in range(n)]
    canvas = Image.new("RGB", (W, H), sep or "#000000")
    y = 0
    for im, h in zip(chosen, heights):
        cell = ImageOps.fit(im, (W, h), method=Image.LANCZOS, centering=(0.5, 0.5))
        canvas.paste(cell, (0, y))
        y += h + gap
    out = io.BytesIO()
    canvas.save(out, "JPEG", quality=95, subsampling=0)
    return out.getvalue()


# ----------------------------------------------------------------- pipeline
def fmt_date(iso):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().strftime("%d/%m %H:%M")
    except Exception:  # noqa: BLE001
        return ""


def announce(entry, test=False):
    """Messaggio immediato: usa solo i dati del feed, nessuna attesa."""
    meta = " · ".join(x for x in (entry["category"], fmt_date(entry["updated"])) if x)
    lines = [("🧪 TEST — " if test else "🍎 ") + f"<b>{esc(entry['title'])}</b>"]
    if meta:
        lines.append(f"<i>{esc(meta)}</i>")
    if entry["summary"]:
        lines += ["", esc(entry["summary"])]
    lines += ["", f"🔗 {esc(entry['link'])}"]
    send_text("\n".join(lines))


def send_media(entry):
    media = None
    for attempt in range(6):  # le foto possono comparire qualche secondo dopo
        try:
            page = http_get(entry["link"]).decode("utf-8", "replace")
            media = extract_media(page, entry["link"])
            if media["images"]:
                break
        except Exception as e:  # noqa: BLE001
            log("pagina articolo non leggibile:", e)
        time.sleep(5)

    if not media or not media["images"]:
        send_text("⚠️ Non ho trovato foto nella pagina (potrebbe non essere completa). "
                  "Apri il link sopra.")
        return

    def get(img):
        for u in [img["url"]] + img["fallbacks"]:
            try:
                data = download(u)
                img["url"] = u
                return img, data
            except Exception as e:  # noqa: BLE001
                log("download fallito:", u, e)
        return img, None

    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(get, media["images"]))

    items, failed = [], 0
    for img, data in results:
        if data is None:
            failed += 1
            continue
        ext = os.path.splitext(img["url"])[1] or ".jpg"
        items.append((img["key"] + ext, data, esc(img["alt"])[:1000]))
    if items:
        try:  # collage 9:16 per i social: va per primo, è la cosa pronta da postare
            col = make_collage([d for _, d in results if d is not None])
            if col:
                tg("sendDocument", {"caption": "🧩 Collage 9:16"},
                   [("document", "collage_9x16.jpg", col, "image/jpeg")])
            elif Image is None and COLLAGE_ON:
                log("Pillow non installato: collage saltato (pip3 install pillow)")
        except Exception as e:  # noqa: BLE001
            log("collage fallito:", e)
        send_images(items)
    if failed:
        send_text(f"⚠️ {failed} foto non scaricate: apri il link dell'articolo.")

    send_videos(media["clips"])

    extra = []
    if media["all_media"]:
        extra.append(f"📦 Tutti i media (zip): {esc(media['all_media'])}")
    for v in media["videos"][:5]:
        extra.append(f"🎬 Video (zip): {esc(v)}")
    if extra:
        send_text("\n".join(extra))


# -------------------------------------------------------------------- video
TG_MAX = 49 * 1024 * 1024  # limite upload della Bot API: 50 MB


def m3u8_attrs(line):
    return {k: v.strip('"') for k, v in re.findall(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', line)}


def hls_to_mp4(url):
    """Variante H.264 più alta che sta sotto i 50 MB, rimuxata in mp4 (niente ricodifica)."""
    master = http_get(url).decode("utf-8", "replace")
    lines = master.splitlines()
    audio, variants = {}, []
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-MEDIA:TYPE=AUDIO") and "DEFAULT=YES" in line:
            a = m3u8_attrs(line)
            audio.setdefault(a.get("GROUP-ID"), urllib.parse.urljoin(url, a["URI"]))
        elif line.startswith("#EXT-X-STREAM-INF:") and i + 1 < len(lines):
            a = m3u8_attrs(line)
            bw = int(a.get("AVERAGE-BANDWIDTH") or a.get("BANDWIDTH") or 0)
            variants.append((bw, a, urllib.parse.urljoin(url, lines[i + 1].strip())))
    # H.264 si vede inline ovunque; HEVC solo se non c'è altro
    variants = [v for v in variants if v[1].get("CODECS", "").startswith("avc1")] or variants
    variants.sort(key=lambda v: v[0], reverse=True)
    if not variants:
        return None
    pl = http_get(variants[-1][2]).decode("utf-8", "replace")
    dur = sum(float(x) for x in re.findall(r"#EXTINF:([\d.]+)", pl))
    for bw, a, vurl in variants:
        if bw * dur / 8 > TG_MAX * 0.95:
            continue
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "v.mp4")
            cmd = [FFMPEG, "-v", "error", "-y", "-user_agent", UA, "-i", vurl]
            aurl = audio.get(a.get("AUDIO"))
            if aurl:
                cmd += ["-user_agent", UA, "-i", aurl, "-map", "0:v:0", "-map", "1:a:0"]
            cmd += ["-c", "copy", "-movflags", "+faststart", out]
            subprocess.run(cmd, check=True, timeout=900)
            data = Path(out).read_bytes()
        if len(data) <= TG_MAX:
            return data
    return None


def video_meta(data):
    """width/height/duration per sendVideo: senza, Telegram mostra il video quadrato."""
    if not FFPROBE:
        return {}
    with tempfile.NamedTemporaryFile(suffix=".mp4") as f:
        f.write(data)
        f.flush()
        out = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0",
                              "-show_entries", "stream=width,height:format=duration",
                              "-of", "json", f.name],
                             capture_output=True, text=True, timeout=60).stdout
    j = json.loads(out)
    st = j["streams"][0]
    return {"width": st["width"], "height": st["height"],
            "duration": int(float(j["format"].get("duration", 0)))}


def send_videos(urls):
    failed = 0
    for u in urls:
        name = u.rsplit("/", 1)[-1]
        if name.endswith(".m3u8"):
            name = re.sub(r"_\d+x\d+$", "", name[:-5])
        else:  # autoplay: .../<nome-video>/large_2x.mp4
            name = u.rsplit("/", 2)[-2]
        try:
            if u.endswith(".m3u8"):
                if not FFMPEG:
                    log("ffmpeg non installato: video HLS saltato", u)
                    failed += 1
                    continue
                data = hls_to_mp4(u)
            else:
                data = download(u)
                data = data if len(data) <= TG_MAX else None
            if not data:
                log("video oltre i 50 MB di Telegram:", u)
                failed += 1
                continue
            tg("sendVideo", {"caption": f"🎬 {esc(name)}", "parse_mode": "HTML",
                             "supports_streaming": "true", **video_meta(data)},
               [("video", name + ".mp4", data, "video/mp4")])
        except Exception as e:  # noqa: BLE001
            log("video fallito:", u, e)
            failed += 1
    if failed:
        send_text(f"⚠️ {failed} video non inviati (troppo grandi o non scaricabili): "
                  "li trovi nell'articolo.")


# -------------------------------------------------------------------- state
def load_seen():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return None


def save_seen(seen):
    STATE_FILE.write_text(json.dumps(seen[-500:]), encoding="utf-8")


try:
    SETTINGS = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
except Exception:  # noqa: BLE001
    SETTINGS = {"sep": None}


# ------------------------------------------------------------------ /config
HEX_RE = re.compile(r"^#?([0-9a-f]{6})$", re.I)
UPD = {"offset": 0, "await_hex": False}


def sep_label():
    sep = SETTINGS.get("sep")
    return "nessuno" if not sep else ("nero" if sep == "#000000" else sep)


def parse_sep(s):
    s = s.strip().lower()
    if s in ("nessuno", "no", "none", "off"):
        return None
    if s in ("nero", "black"):
        return "#000000"
    m = HEX_RE.match(s)
    if m:
        return "#" + m.group(1).upper()
    raise ValueError(s)


def set_sep(v):
    SETTINGS["sep"] = v
    SETTINGS_FILE.write_text(json.dumps(SETTINGS), encoding="utf-8")
    send_text(f"✅ Separatore del collage: <b>{esc(sep_label())}</b>")


def config_menu():
    kb = {"inline_keyboard": [[
        {"text": "🚫 Nessuno", "callback_data": "sep:none"},
        {"text": "⬛ Nero", "callback_data": "sep:#000000"},
        {"text": "🎨 Colore…", "callback_data": "sep:custom"},
    ]]}
    send_text(f"⚙️ <b>Collage</b>\nSeparatore tra le foto: <b>{esc(sep_label())}</b>\n\n"
              "Scegli qui sotto, oppure scrivi <code>/config #FF3B30</code>.",
              reply_markup=json.dumps(kb))


def handle_update(u):
    q = u.get("callback_query")
    if q:
        tg("answerCallbackQuery", {"callback_query_id": q["id"]}, need_chat=False)
        if str(q.get("message", {}).get("chat", {}).get("id")) != CHAT_ID:
            return
        data = q.get("data", "")
        if data == "sep:custom":
            UPD["await_hex"] = True
            send_text("Mandami il colore in hex, es. <code>#FF3B30</code>")
        elif data.startswith("sep:"):
            UPD["await_hex"] = False
            set_sep(None if data == "sep:none" else data[4:])
        return
    m = u.get("message") or {}
    if str(m.get("chat", {}).get("id")) != CHAT_ID:  # risponde solo a te
        return
    words = (m.get("text") or "").split()
    if words and words[0].split("@")[0] == "/config":
        UPD["await_hex"] = False
        if len(words) == 1:
            config_menu()
            return
        arg = words[-1]  # "/config nero", "/config separatore #FF3B30", ...
    elif UPD["await_hex"] and words:
        arg = words[0]
    else:
        return
    try:
        v = parse_sep(arg)
    except ValueError:
        send_text("Non ho capito. Scrivi <code>nero</code>, <code>nessuno</code> "
                  "oppure un hex come <code>#FF3B30</code>.")
        return
    UPD["await_hex"] = False
    set_sep(v)


def wait_updates(until):
    """Al posto della sleep: long polling dei comandi fino a `until`."""
    while (left := until - time.time()) > 0:
        try:
            res = tg("getUpdates", {"offset": UPD["offset"], "timeout": max(1, int(left)),
                                    "allowed_updates": '["message","callback_query"]'},
                     tries=1, need_chat=False)
        except Exception as e:  # noqa: BLE001
            log("getUpdates:", e)
            time.sleep(min(left, 5))
            continue
        for u in res.get("result", []):
            UPD["offset"] = u["update_id"] + 1
            try:
                handle_update(u)
            except Exception as e:  # noqa: BLE001
                log("comando fallito:", e)


# --------------------------------------------------------------------- main
def need_config(chat=True):
    if not TOKEN or (chat and not CHAT_ID):
        sys.exit("Manca TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID: compila il file .env "
                 "(vedi .env.example).")


def cmd_chat_id():
    need_config(chat=False)
    res = tg("getUpdates", need_chat=False)
    seen = {}
    for u in res.get("result", []):
        m = u.get("message") or u.get("channel_post") or u.get("my_chat_member", {})
        c = m.get("chat") if m else None
        if c:
            seen[c["id"]] = c.get("username") or c.get("title") or c.get("first_name")
    if not seen:
        print("Nessun messaggio trovato: scrivi qualcosa al tuo bot su Telegram "
              "(es. /start) e rilancia questo comando.")
    for cid, name in seen.items():
        print(f"TELEGRAM_CHAT_ID={cid}   ({name})")


def cmd_test():
    need_config()
    entries = fetch_feed()
    if not entries:
        sys.exit("Feed vuoto.")
    e = entries[0]
    log("test su:", e["title"])
    announce(e, test=True)
    send_media(e)
    log("fatto.")


def run():
    need_config()
    try:
        tg("setMyCommands", {"commands": json.dumps(
            [{"command": "config", "description": "Personalizza il collage"}])}, need_chat=False)
    except Exception as e:  # noqa: BLE001
        log("setMyCommands:", e)
    seen = load_seen()
    entries = None
    while entries is None:
        try:
            entries = fetch_feed()
        except Exception as e:  # noqa: BLE001
            log("feed non raggiungibile all'avvio:", e)
            time.sleep(10)

    if seen is None:  # primo avvio: tutto ciò che c'è già non va notificato
        seen = [e["id"] for e in entries]
        save_seen(seen)
        last = entries[0]["title"] if entries else "—"
        send_text(f"✅ Monitor Apple Newsroom attivo (controllo ogni {int(INTERVAL)}s).\n"
                  f"Ultimo articolo già uscito: {esc(last)}")
    elif not os.environ.get("GITHUB_ACTIONS"):  # su Actions riparte ogni ~6h: niente spam
        send_text(f"✅ Monitor Apple Newsroom riavviato (controllo ogni {int(INTERVAL)}s).")
    log(f"Monitoraggio attivo, {len(seen)} articoli già noti. Ctrl+C per fermare.")

    fails, alerted = 0, False
    while True:
        t0 = time.time()
        try:
            entries = fetch_feed()
            if alerted:
                send_text("✅ Il feed di Apple Newsroom è di nuovo raggiungibile.")
            fails, alerted = 0, False
            new = [e for e in entries if e["id"] not in seen]
            new.sort(key=lambda e: e["updated"])  # dal più vecchio al più recente
            for e in new:
                log("NUOVO:", e["title"])
                try:
                    announce(e)
                except Exception as ex:  # noqa: BLE001
                    log("invio Telegram fallito, riprovo al prossimo giro:", ex)
                    break
                seen.append(e["id"])
                save_seen(seen)
                try:
                    send_media(e)
                except Exception as ex:  # noqa: BLE001
                    log("errore invio foto:", ex)
                    try:
                        send_text("⚠️ Errore nell'invio delle foto: usa il link sopra.")
                    except Exception:  # noqa: BLE001
                        pass
        except Exception as ex:  # noqa: BLE001
            fails += 1
            log(f"errore lettura feed ({fails}):", ex)
            if fails >= 10 and not alerted:
                try:
                    send_text("⚠️ Da qualche minuto non riesco a leggere il feed di "
                              "Apple Newsroom. Controlla la connessione del Mac.")
                    alerted = True
                except Exception:  # noqa: BLE001
                    pass
        wait_updates(t0 + INTERVAL)


if __name__ == "__main__":
    try:
        if "--chat-id" in sys.argv:
            cmd_chat_id()
        elif "--test" in sys.argv:
            cmd_test()
        else:
            run()
    except KeyboardInterrupt:
        print("\nFermato.")
