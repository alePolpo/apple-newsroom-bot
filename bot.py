#!/usr/bin/env python3
"""
Apple Newsroom -> Telegram

Controlla il feed ufficiale di Apple Newsroom ogni 30 secondi. Appena esce un
nuovo articolo ti manda su Telegram:
  1. subito: titolo + sommario + link
  2. un collage verticale 9:16 con le prime 3 foto orizzontali (serve Pillow)
  3. tutte le foto dell'articolo in alta qualità (come file, senza compressione)
  4. i link per scaricare tutti i media (zip) e gli eventuali video

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
import sys
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
INTERVAL = float(os.environ.get("POLL_SECONDS", "30"))
SEND_AS = os.environ.get("SEND_AS", "document").lower()  # document | photo
API = os.environ.get("TELEGRAM_API", "https://api.telegram.org")
FEED_URL = os.environ.get("FEED_URL", "https://www.apple.com/newsroom/rss-feed.rss")
SITE = "https://www.apple.com"
STATE_FILE = BASE / "seen.json"
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
    return {"images": images, "all_media": all_media, "videos": videos}


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
            with urllib.request.urlopen(req, timeout=120) as r:
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


def send_text(text):
    tg("sendMessage", {"text": text, "parse_mode": "HTML",
                       "disable_web_page_preview": "true"})


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
COLLAGE_GAP = int(os.environ.get("COLLAGE_GAP", "8"))          # px di bordo nero tra le foto
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
    usable = H - COLLAGE_GAP * (n - 1)
    heights = [usable // n + (1 if i < usable % n else 0) for i in range(n)]
    canvas = Image.new("RGB", (W, H), (0, 0, 0))
    y = 0
    for im, h in zip(chosen, heights):
        cell = ImageOps.fit(im, (W, h), method=Image.LANCZOS, centering=(0.5, 0.5))
        canvas.paste(cell, (0, y))
        y += h + COLLAGE_GAP
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
                tg("sendDocument", {"caption": "🧩 Collage 9:16 (prime foto orizzontali)"},
                   [("document", "collage_9x16.jpg", col, "image/jpeg")])
            elif Image is None and COLLAGE_ON:
                log("Pillow non installato: collage saltato (pip3 install pillow)")
        except Exception as e:  # noqa: BLE001
            log("collage fallito:", e)
        send_images(items)
    if failed:
        send_text(f"⚠️ {failed} foto non scaricate: apri il link dell'articolo.")

    extra = []
    if media["all_media"]:
        extra.append(f"📦 Tutti i media (zip): {esc(media['all_media'])}")
    for v in media["videos"][:5]:
        extra.append(f"🎬 Video (zip): {esc(v)}")
    if extra:
        send_text("\n".join(extra))


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
        time.sleep(max(1.0, INTERVAL - (time.time() - t0)))


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
