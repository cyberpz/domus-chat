# DomusChat

Chat GUI leggera per **LLM locali** — zero dipendenze (solo Python stdlib + SQLite), un container da ~256 MB di RAM.

Un clone spirituale di Open WebUI, ma essenziale: niente RAG, niente pipeline, niente framework. Pensata per parlare diretto con llama.cpp / local-llm-manager / qualsiasi endpoint OpenAI-compatible, con un tema CRT-verde fisso e metriche reali in ogni risposta.

## Funzionalità

- **Modello live dal backend** — il selettore riflette l'endpoint: modelli cancellati o non caricabili non compaiono (polling 30 s, cache 15 s)
- **Speech-to-speech via microfono** 🎤 — con text bar vuota, "Invia" diventa il pulsante mic: registra, trascrive (Whisper via `STT_URL`), invia e **legge la risposta ad alta voce** (edge-tts, 40+ voci). ▶ per riascoltare ogni risposta, barge-in durante la lettura
- **Streaming SSE** — token in tempo reale, `reasoning_content` collassabile separato
- **Stato reale del backend** — lo skeleton mostra cosa sta facendo davvero il manager (download/caricamento/in coda), non frasi casuali
- **Metriche per risposta** — chip `modello · ⚡ t/s prefill · 📝 t/s gen` (dai timing nativi llama.cpp), persistite nel DB
- **Ctx meter** — % di finestra contesto usata, verde→ambra→rosso
- **Compressione automatica** — oltre ~90% della window riassunto dal modello stesso; i messaggi vecchi restano nel DB, marcati `hidden`
- **Azioni sui messaggi** — Copia · Rigenera · Elimina (assistente), Copia · Rimedia · Elimina (utente)
- **Multi-utente leggero** — chiave di sessione a 6 cifri condivisa in famiglia, proprietà chat, link `?conv=id` di sola lettura senza login
- **PWA installabile** — manifest + service worker, funziona su mobile (HTTPS consigliato: il microfono su Chrome/Android lo richiede)
- **File temporanei** — upload di testo in RAM con TTL 6 h, mai su disco
- **System prompt globale** — overlay ⚙, persistito

## Avvio rapido

```bash
cp .env.example .env      # punta UPSTREAM al tuo backend
docker compose up -d --build
# GUI su http://<host>:3003
```

Backend testati: [local-llm-manager](https://github.com/cyberpz/local-llm-manager) (load-on-demand llama.cpp), llama-server nudo, Ollama in modalità `/v1`.

### Configurazione (`.env`)

| Var | Default | Descrizione |
|---|---|---|
| `UPSTREAM` | `http://127.0.0.1:1234` | Base URL OpenAI-compatible |
| `UPSTREAM_AUTH` | vuoto | Token backend (query param `?api_key=`, vedi note) |
| `STT_URL` | vuoto (off) | Server Whisper/OpenAI-style per il microfono |
| `TTS_VOICE` | `en-US-AriaNeural` | Voce edge-tts (serve internet; `it-IT-DiegoNeural` per l'italiano) |
| `DB_PATH` | `/data/chat.db` | SQLite nel container |
| `PORT` | `8080` | Porta interna |

La chiave di sessione è un token a 6 cifre emesso da `/api/session` e salvato in `data/tokens.json` (chiaro, editabile a mano: è il "database utenti"). Chi ha la chiave vede le stesse chat su più dispositivi: pulsante 🔑.

## Architettura

```
Browser ──SSE──> DomusChat (HTTP server stdlib, threading)
                   ├─ SQLite: conversations, messages, settings
                   ├─ RAM: file temporanei (TTL 6 h)
                   ├─ proxy ──> backend OpenAI-compat (:1234)
                   ├─ /api/stt ──> Whisper server (opzionale)
                   └─ /api/tts ──> edge-tts (opzionale, via internet)
```

Due dettagli che hanno morso in sviluppo:

1. **Auth via query param** — verso alcuni backend l'header `Authorization: Bearer` non arriva; `local-llm-manager` accetta `?api_key=*** e il proxy usa quello.
2. **SSE + keep-alive** — senza `Connection: close` a fine stream i client restano appesi dopo l'evento `done` (niente Content-Length = EOF sulla chiusura).

## File

```
app.py              server + proxy + DB + auth + voce + compressione (stdlib only)
index.html          GUI single-page (tema fisso, zero framework)
static/             manifest, service worker, icone PWA
Dockerfile          python:3.12-slim + edge-tts
docker-compose.yml  porta 3003, volume ./data
```

## Licenza

MIT.