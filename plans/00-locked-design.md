# Wardrobe AI (closet-AI) — Design Plan

**Status:** Locked after grilling (shared understanding confirmed)  
**Scope:** v1 build; voice + outfit pairing deferred to v2  
**Session root:** `/home/arch/programs/extra/closet-AI`

---

## 1. One-liner

Local-only web app: photograph a garment held up → confirm identity → undress-only wear log with dirt ratings → composite **due** list cleared by marking washed.

---

## 2. Environment (facts)

| Fact | Value |
|------|--------|
| Project dir | Fresh; only `.mimocode/` + `plans/` |
| Python | 3.14.10 |
| GPU | RTX 3050 Mobile, **4GB VRAM** (CUDA driver present) |
| CPU / RAM | i5-12500H 16 threads; ~8.4GB RAM free |
| Disk | Home cleared **50GB+** — project-local data OK |
| Mic / cam | Internal DMIC; **ACER HD webcam** `/dev/video0` |
| Node / ffmpeg | Node 24; ffmpeg n9 (optional) |
| Ollama | Not installed; **not required for v1** |

---

## 3. Locked product decisions (v1)

### 3.1 Compute & delivery
- **Fully local** — no cloud vision, voice, or LLM for v1.
- **Stack:** FastAPI + SQLite + htmx/vanilla JS, one Python process via **uvicorn**.
- **Network:** Bind LAN only (`0.0.0.0` on home Wi‑Fi); **PIN auth**; no port-forward, no public exposure.
- **Run mode:** Bare `uv`/`pip` + uvicorn (systemd user unit or script). No Docker for v1.
- **Language:** English UI.

### 3.2 Capture
- **Mode:** Active snap — one garment **held up** to the camera (not passive always-on).
- **Devices:** Laptop webcam **and** phone browser on same LAN are **both first-class** (same responsive capture page).
- **Bootstrap:** **Photo-only** loop (no bulk form in v1).
- On photo: detect + attribute → user confirms before item enters catalog.

### 3.3 Catalog & identity
**Required fields at confirm:** name, type, color, pattern, sleeve length, **quantity** (default 1).

**Item types (fixed enum):**  
`shirt`, `tshirt`, `full_sleeve_tshirt`, `pants`, `shorts`, `three_fourths`, `trackpants`, `jacket`, `socks`, `underwear`

**Identity model:**
- First confident sighting **creates** the item (after human confirm).
- Later sightings: **embedding + attribute match**; if ambiguous → **“Same as …?”** picker or “New item”.
- Low detector confidence → **full-frame fallback** classifier path; else **retake** prompt.
- Attributes editable anytime; primary photo kept on record for re-ID.
- **Qty pools:** socks and identical multipacks are one record (`quantity = N`); most underwear are **unique** entries (qty only for identical multiples).

### 3.4 Recognition stack (local)
- **Detector:** YOLOv8n/s (DeepFashion2-pretrained or fine-tunable) → boxes for multi-object frames; primary path assumes **one garment in frame**.
- **Attributes:** small head / crop classifier: type, color, pattern, sleeve (feeds confirm UI).
- **Fallback:** full-frame attribute classifier when box confidence low.
- **VRAM budget:** keep model footprint well under 4GB; no large local VLM in v1.
- Store weights under project (or `models/` in repo); not on a full disk path.

### 3.5 Wear model — undress-only
- **No** “I’m wearing X” check-in.
- **Each undress event = +1 wear** on the item (or pool) **and** one dirt rating.
- **Qty pool:** one undress of “socks” → `pool.wears += 1`, rating applied to pool (not which physical pair).
- **Unique underwear:** rating and wears on that specific item.
- **Wear count** resets on **mark washed**; **history** of past wears is archived, not deleted.

### 3.6 Dirt rating UX
- Scale **1–5** at undress time.
- **v1:** UI only (voice is v2).
- **Picker:** **Smart candidates** — multi-select from items *not recently removed*, *not due*, *not laundry*, plus **search** fallback; category-first optional if needed for speed.
- **Soft backlog:** on next app open, gentle “rate what you took off?” with **Skip**.
- **Undo:** last rating only (plus normal edit flows).
- Immediate feedback if rating **trips due** on that event.

### 3.7 Wash logic (composite → due)

**Due if ANY of:**
1. `wears_since_wash >= max_wears[type]`
2. `latest_dirt >= 4`
3. `avg_dirt_since_wash >= 3`
4. `days_since_wash >= type_cap_days[type]`

**Approved defaults:**

| Type | Max wears since wash | Dirt weight | Days cap (since wash) |
|------|---------------------|-------------|------------------------|
| underwear | 3 | high | 7 |
| socks | 5 | high | 14 |
| tshirt | 5 | medium | 21 |
| full_sleeve_tshirt | 5 | medium | 21 |
| three_fourths | 5 | medium | 21 |
| shirt | 4 | medium | 21 |
| shorts | 6 | medium | 30 |
| trackpants | 6 | medium | 30 |
| pants | 10 | low | 60 |
| jacket | 15 | low | 90 |

- Thresholds **user-tunable later** in settings (schema supports it; UI can be v1.1).
- High dirt **always** wins immediately (`latest_dirt >= 4`).
- “Dirt weight” reserved for future refined scoring; **v1 uses the four OR rules above** (weights optional multiplier — implement as pure OR rules first for explainability).

### 3.8 Laundry lifecycle
**Two states only:** `available` ⇄ `due`

- Auto → **due** when composite trips (after rating, or batch recompute).
- User **mark washed** (single item or **batch on due list**) → **available**, counters reset, history archived.
- **No** `in laundry` state.
- Due items **out of fresh rotation** (not offered as clean candidates for logging as “just wore” until washed — they’re already worn/due).

### 3.9 Surfaces
- **Due list** always visible in app (the “wash queue”).
- **No** required desktop notifications or proactive voice in v1.
- Post-rating toast/inline: “Added to wash list” when threshold trips.

### 3.10 Data & ops
- **SQLite** single DB file in project (e.g. `data/wardrobe.db`).
- **Daily file copy** backup into `data/backups/` (simple cron or on-startup copy).
- Photos: filesystem under `data/images/<item_id>/…`, compressed (webp/jpeg).
- PIN stored hashed; session cookie or header token after unlock.

---

## 4. Explicitly deferred (v2 backlog)

### 4.1 Voice assistant
- **Wake word + push-to-talk** (both), local STT (e.g. whisper.cpp / faster-whisper) + local TTS.
- Short commands: `scan`, `taking off <item>, dirty <n>`, `what’s due`, `mark washed <item>`.
- Feedback spoken on next interaction / after command.
- Design hook: API already event-based (rating, due) so voice can subscribe later.

### 4.2 Outfit pairing
- **Weighted score** over **clean/available** items only:
  - Color harmony (safe neutrals + simple complementary rules)
  - Context/formality (casual vs shirt; gym vs out)
  - Season (calendar season + **manual climate toggle**; column `climate_hint` reserved)
  - **Rotation fairness** (prefer least-worn clean when equal)
- **Outfit unit:** top + bottom + **jacket when relevant** (cold).
- Rules-first + explainable reasons; optional local LLM ranker later (not source of truth).
- Shoes not tracked; socks/underwear are hygiene/rotation only, not style pairs.

### 4.3 Explicit non-goals
Cloud sync, multi-user, passive always-on camera, public internet, Docker (v1), weather API (unless opt-in later), individual-sock tracking, bulk form bootstrap (v1).

---

## 5. Architecture (v1 target)

```
Phone browser ──┐
               ├── HTTPS-less LAN ── PIN ── FastAPI
Laptop browser ─┘                            │
                                             ├─ routes: auth, items, capture, log, due, settings
                                             ├─ services: detect, attributes, embed, wash_rules
                                             ├─ SQLite (wardrobe.db)
                                             ├─ filesystem: images/, models/, data/backups/
                                             └─ YOLOv8n/s + attribute head (torch, CUDA if available)
```

**Suggested repo layout:**
```
closet-AI/
  plans/
    00-locked-design.md          # this file (or index)
  app/
    main.py                      # FastAPI entry
    auth.py
    db.py                        # schema + migrations
    routes/                      # items, capture, log, due, settings
    services/
      detect.py                  # YOLO + attribute head
      identity.py                # match / confirm
      wash_rules.py              # composite due logic
      candidates.py              # smart undress candidates
    templates/                   # htmx pages
    static/
  models/                        # weights (gitignored if large)
  data/                          # db, images, backups (gitignored)
  pyproject.toml
  README.md
  AGENTS.md                      # optional, project rules
```

---

## 6. SQLite schema (v1 core)

```sql
items (
  id, name, type, color, pattern, sleeve,
  quantity,                 -- default 1; pools for socks / identical multipacks
  state,                    -- 'available' | 'due'
  wears_since_wash,         -- int
  created_at, primary_photo_path
)

ratings (
  id, item_id, dirt,        -- 1..5
  rated_at                  -- undress event time
)

wear_events (
  id, item_id, event_at     -- one row per undress; +1 to wears_since_wash
)

wash_events (
  id, item_id, washed_at    -- reset counters; archive snapshot
)

item_photos (
  id, item_id, path, embedding BLOB, created_at
)

settings (
  key, value                -- pin_hash, thresholds overrides, climate_hint, etc.
)

-- optional: wear_history_archive for post-wash snapshots
```

**Due evaluation:** pure function of item + ratings + wash_events (recompute on rate and on day boundary).

---

## 7. UX flows (v1)

1. **Bootstrap:** open app → PIN → Capture → hold garment → confirm fields (+ qty) → item created.  
2. **New sighting:** capture → match → “Same as X?” / “New item”.  
3. **Undress:** open Log → smart candidates multi-select → rating 1–5 each → if trips due → “On wash list”.  
4. **Forgot:** next open → backlog prompt → rate or skip.  
5. **Laundry:** Due list → “Mark washed” (one or all) → counters reset.  
6. **Dashboard:** available count, due list, recent wears, (empty states friendly).

---

## 8. Implementation phases

### Phase 0 — Skeleton
- [ ] `uv` project, FastAPI, PIN auth, LAN bind, base layout (htmx)
- [ ] SQLite schema + migrations
- [ ] CRUD items (manual fields only, no vision yet)

### Phase 1 — Capture + identity
- [ ] Capture page (webcam + phone getUserMedia upload)
- [ ] YOLOv8n/s inference + attribute head (or interim: full-frame classifier only)
- [ ] Confirm UI (all required fields + qty)
- [ ] Embedding store + “Same as…?” match flow
- [ ] Fallback / retake on low confidence

### Phase 2 — Logging + wash
- [ ] Smart candidates + multi-select undress log
- [ ] Dirt rating + undo + backlog
- [ ] Composite due rules + defaults table
- [ ] Due list + mark washed (single/batch) + history archive
- [ ] Post-rating due feedback
- [ ] Daily SQLite backup

### Phase 3 — Polish v1
- [ ] Settings: view/edit thresholds (optional UI), climate_hint toggle (schema only ok)
- [ ] Empty states, basic stats (wears since wash, last washed)
- [ ] README run instructions
- [ ] **Ship gate:** full loop works offline on LAN with webcam + phone

### Phase 4+ — v2 (explicitly out of v1)
- Voice (wake + PTT, local STT/TTS)
- Outfit pairing (weighted rules + explanations)
- Optional: LLM ranker, weather opt-in, Docker, tailnet

---

## 9. Risks & mitigations

| Risk | Mitigation |
|------|------------|
| DeepFashion2 labels ≠ your 10 types | Map/fine-tune head to **your enum**; confirm UI always human-in-the-loop |
| 4GB VRAM contention | YOLOv8n/s + small heads; batch size 1; CPU fallback |
| Home disk full again | Keep `data/` small; compress images; backups pruned (keep N days) |
| Identical socks mis-ID | **Qty pools** — identity doesn’t distinguish pairs |
| Forgot to rate | Soft backlog; accept undercount |
| LAN PIN only | Accept for home; no public bind; document risk |
| Python 3.14 vs torch wheels | Pin known-good versions in `pyproject`; verify CUDA wheel at Phase 0 |

---

## 10. Acceptance criteria (v1 done when)

1. From phone or laptop on LAN, PIN unlock, hold one garment, confirm → item in catalog with qty.  
2. Second photo of same item offers “Same as…?”.  
3. Undress flow: candidates → rate → wear count increments.  
4. Item goes **due** when any composite rule trips; appears on due list; feedback after rating.  
5. Mark washed → counters reset, history retained, item available again.  
6. Socks pool: one undress = +1 pool wear.  
7. Entire loop works **with network egress blocked** (except LAN).  
8. No voice, no pairing required for the above.

---

## 11. Open items (non-blocking)

- Exact YOLO checkpoint / whether to fine-tune on a few personal photos after Phase 1  
- PIN length / lockout policy (suggest 6-digit + simple throttle)  
- Whether settings threshold editor is v1 or v1.1  
- Git init + .gitignore for `models/`, `data/`

---

*End of locked plan. Implementation should not expand scope without re-opening the corresponding grilling branch.*
