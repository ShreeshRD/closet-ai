# Closet AI

Local-only wardrobe tracker: catalog garments, log wears with dirt ratings, see what needs washing.

**Design:** locked in [`plans/00-locked-design.md`](plans/00-locked-design.md).

## Phases

- **Phase 0:** FastAPI + SQLite + htmx, PIN auth, item CRUD, wash due rules
- **Phase 1:** Capture (webcam + phone stills), confirm UI, embedding “Same as…?”, hands-free **shoot / retake** voice
- **Phase 2:** Undress log + dirt ratings, smart candidates, due list, mark washed (single/batch), backlog, daily backup
- **Phase 3:** Settings (per-type wash thresholds + climate_hint), empty states / stats (wears since wash, last washed), run docs

## Run

```bash
cd closet-AI
uv sync
uv run closet-ai
```

First visit asks you to **create a PIN**.

### Localhost (webcam on this machine)

```bash
CLOSET_AI_HOST=127.0.0.1 CLOSET_AI_SSL=0 uv run closet-ai
```

Open `http://127.0.0.1:8420/` — `getUserMedia` works in a secure context (localhost).

### LAN (phone + laptop)

Default bind is `0.0.0.0`. For live camera from another device you need **HTTPS** (browsers block `getUserMedia` on plain HTTP LAN):

1. Put certs in `data/certs/cert.pem` + `data/certs/key.pem` (SAN must include the laptop’s LAN IP — e.g. `openssl req -x509 … -addext "subjectAltName=IP:…"`).
2. `CLOSET_AI_HOST=0.0.0.0 CLOSET_AI_SSL=1 uv run closet-ai` → prints `HTTPS on https://…`
3. Phone: open `https://<laptop-ip>:8420/`, accept the cert warning once, then Capture → Photo (file picker) or webcam.

Phone stills via `<input type="file" accept="image/*" capture="environment">` work on plain HTTP without HTTPS.

**Wi‑Fi note:** some home routers isolate wireless clients from each other (AP/client isolation). If the phone times out on `https://192.168.x.x:8420` while the server is healthy, put the **phone in hotspot mode**, connect the **laptop to that hotspot**, and use the laptop’s hotspot-subnet IP (still fully offline / no internet egress required for the loop).

Optional env:

| Variable | Default |
|----------|---------|
| `CLOSET_AI_HOST` | `0.0.0.0` |
| `CLOSET_AI_PORT` | `8420` |
| `CLOSET_AI_DATA` | `./data` |
| `CLOSET_AI_ROOT` | repo root |
| `CLOSET_AI_SSL` | `1` if certs exist under `data/certs/` |
| `CLOSET_AI_SSL_CERT` | `data/certs/cert.pem` |
| `CLOSET_AI_SSL_KEY` | `data/certs/key.pem` |

## Everyday loop

1. **Capture** (or **Add**) → confirm fields → item in catalog  
2. **Log** → select what you took off → dirt 1–5 → Save  
3. **Due** list when a rule trips → **Mark washed**  
4. **Settings** → tweak per-type max wears / days caps if needed  

## Tests

```bash
uv run pytest -q
```

## Layout

```
src/closet_ai/
  main.py           # app entry
  auth.py           # PIN + session
  db.py             # schema
  routes/           # auth, items, capture, log, settings
  services/         # catalog, wash_rules, candidates, logging, app_settings, voice, vision
  templates/ static/
plans/              # locked design
data/               # db, images, backups, certs (gitignored)
models/             # weights (gitignored)
```
