# Closet AI

Local-only wardrobe tracker: catalog garments, log wears with dirt ratings, see what needs washing.

**Design:** locked in [`plans/00-locked-design.md`](plans/00-locked-design.md). Voice + outfit pairing are v2.

## Phase 0 (current)

- FastAPI + SQLite + htmx, LAN bind + PIN
- Item CRUD (manual; photo capture is Phase 1)
- Wash **due** rules (composite thresholds) and wash-list on home page

## Run

```bash
cd closet-AI
uv sync
uv run closet-ai
```

Open `http://<machine-ip>:8420/` from laptop or phone on the same LAN. First visit asks you to **create a PIN**.

Optional env:

| Variable | Default |
|----------|---------|
| `CLOSET_AI_HOST` | `0.0.0.0` |
| `CLOSET_AI_PORT` | `8420` |
| `CLOSET_AI_DATA` | `./data` |
| `CLOSET_AI_ROOT` | repo root |

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
  routes/           # auth, items
  services/         # catalog, wash_rules
  templates/ static/
plans/              # locked design
data/               # db, images, backups (gitignored)
models/             # weights (Phase 1+, gitignored)
```
