# AI Chef — Backend API

Async FastAPI rewrite of the original n8n "ИИ-Повар" Telegram workflow.
Same business logic (language-locked chef personas, allergy/appliance-aware
recipes, daily free-generation limit, referral bonuses, trial activation,
dish-photo generation), rebuilt as a stateless, horizontally-scalable API
backed by Supabase (Postgres).

## 1. Setup

```bash
cp .env.example .env
# fill in SUPABASE_URL / SUPABASE_SECRET_KEY / GEMINI_API_KEY, etc.
```

Run the schema against your Supabase project (SQL editor, or `psql`/`supabase db push`):

```bash
sql/schema.sql
```

## 2. Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload
```

Interactive docs: http://localhost:8000/docs

## 3. Deploy on a VPS with Docker

```bash
docker compose up -d --build
```

That builds the image, starts the container on port 8000, and restarts it
automatically on reboot/crash (`restart: unless-stopped`). Put a reverse
proxy (nginx/Caddy) with TLS in front of it for public access.

## 4. API overview

| Method | Path                                   | Purpose |
|--------|-----------------------------------------|---------|
| POST   | `/api/v1/users/register`                | Get-or-create a user profile (handles referral bonus) |
| GET    | `/api/v1/users/{user_id}`               | Fetch profile |
| PATCH  | `/api/v1/users/{user_id}/settings`      | Update language / allergies / appliances / chef persona |
| POST   | `/api/v1/users/{user_id}/trial`         | Activate the one-time trial bonus |
| GET    | `/api/v1/users/{user_id}/usage`         | Remaining free generations today |
| POST   | `/api/v1/recipes/generate`              | Text-based recipe generation |
| POST   | `/api/v1/recipes/generate-from-photo`   | Photo-based recipe generation (multipart upload) |
| POST   | `/api/v1/recipes/generate-image`        | Imagen 3 photo of the finished dish |
| GET    | `/api/v1/links`                         | Wheel of Fortune (GitHub Pages WebApp) + feedback form URLs |
| GET    | `/health`                                | Liveness probe |

## 5. Design notes / mapping from the original workflow

- **Daily limit reset at 00:00** — `daily_usage` is keyed by `(user_id, usage_date)`.
  Once the calendar date rolls over there's simply no row yet for "today", so
  the counter starts back at 0 with no cron job needed.
- **Chef personas & strict language lock** — ported verbatim from the
  `Шеф-ИИ` agent's system prompt into `services/gemini_service.py`
  (`_PERSONA_VOICE`, `_language_rule`).
- **Allergy safety check** — simplified, code-based port of the
  `Проверка безопасности` node: ingredient names are canonicalized via an
  alias table and cross-checked against the user's declared allergies before
  a recipe is returned.
- **Referral bonus / trial** — `services/user_service.py` grants bonus
  requests on top of the daily free limit, mirroring `Начислить бонус` /
  `Активировать триал`.
- **Wheel of Fortune / feedback form** — these were external links opened
  from Telegram buttons in the original bot; `/api/v1/links` just serves the
  configured URLs so any client (mobile app or bot) can open them.
- **Telegram-specific mechanics not carried over 1:1**: Telegram Stars
  payments, the roulette mini-game's own AI agent, and the 18+ gate were
  bot-channel-specific UI flows. The core recipe/allergy/limit logic they all
  ultimately called into is fully implemented here; wire up a thin Telegram
  webhook handler on top of this API if you want to keep serving the bot
  channel as well as a mobile app.

## 6. Security notes

- `SUPABASE_SECRET_KEY` is a service-role key with full database access —
  keep it only in the backend's `.env`, never ship it to a mobile client.
  Mobile/web clients should only ever see `SUPABASE_PUBLISHABLE_KEY`.
- Rotate the Supabase secret key and Gemini API key if they were ever shared
  outside this project's private configuration.
