# Apple Newsroom → Telegram

Controlla il feed ufficiale di Apple Newsroom ogni 10 secondi. Per ogni nuovo articolo ti manda su Telegram:

1. **subito** titolo, categoria, sommario e link
2. **collage 9:16** (1080×1920) con le prime 3 foto orizzontali dell'articolo
3. **tutte le foto** dell'articolo in alta qualità (come file, senza compressione di Telegram)
4. **i video** dell'articolo (H.264 sotto i 50 MB di Telegram; serve `ffmpeg`)
5. link allo zip con tutti i media

In chat, `/last` manda l'ultimo articolo uscito. `/config` sceglie il separatore del collage: nessuno (default), nero o un colore hex (`/config #FF3B30`). Risponde solo al `TELEGRAM_CHAT_ID`; la scelta sta in `settings.json`.

Online gira su GitHub Actions (`.github/workflows/bot.yml`, secret `TELEGRAM_BOT_TOKEN` e `TELEGRAM_CHAT_ID`): ogni run dura circa 5h40m, poi salva lo stato nel repo e parte il successivo.

## Setup (5 minuti, una volta sola)

1. Su Telegram apri **@BotFather** → `/newbot` → scegli nome → copia il **token**.
2. Apri il tuo nuovo bot e scrivigli `/start`.
3. Nella cartella del bot:
   ```
   pip3 install pillow          # serve solo per il collage
   cp .env.example .env         # poi incolla il token in .env
   python3 bot.py --chat-id     # stampa il tuo TELEGRAM_CHAT_ID -> incollalo in .env
   python3 bot.py --test        # manda l'ultimo articolo già uscito, per provare tutto
   ```
4. Avvio vero (tieni il Mac sveglio per tutto il pomeriggio):
   ```
   caffeinate -i python3 bot.py
   ```

Al primo avvio gli articoli già usciti vengono ignorati; ti arriva solo un messaggio "monitor attivo". Se riavvii, riparte da dove era (file `seen.json`).

## Opzioni (in `.env`)

| Variabile | Default | Cosa fa |
|---|---|---|
| `POLL_SECONDS` | 10 | ogni quanto controlla |
| `SEND_AS` | document | `document` = foto originali; `photo` = anteprime Telegram |
| `COLLAGE` | 1 | `0` per disattivarlo |
| `COLLAGE_GAP` | 8 | px del separatore quando è attivo (colore da `/config`) |
| `COLLAGE_WIDTH` | 1080 | larghezza del collage (altezza = ×16/9) |

## Note

- Il collage prende le prime 3 foto **orizzontali** nell'ordine in cui compaiono nell'articolo (le verticali verrebbero tagliate male); se ce ne sono meno di 3 completa con le altre. Ritaglio centrato, senza deformazioni.
- Se il Mac va in standby il bot si ferma: per questo `caffeinate`.
- Se il feed non è raggiungibile per ~5 minuti ti avvisa su Telegram, e ti avvisa quando torna.
