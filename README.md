# DreamCadd — Lead Management Application

A Django implementation of the architecture we designed:

```
MD -> Excel Upload -> Import/Dedup -> PostgreSQL (or SQLite) -> Auto-assign to Telecaller
                                                                        |
                                                                        v
                                                              WhatsApp Cloud API
                                                                        |
                                                              Student replies -> Telecaller notified
```

n8n is replaced by Django views + `leads/services.py` — same three workflows, no external
automation tool to host/maintain.

## 1. Setup

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -r requirements.txt

cp .env.example .env             # then edit .env with real values

python manage.py migrate
python manage.py createsuperuser # make your first MD login (set role=MD in admin after creating)
python manage.py runserver
```

Visit `http://127.0.0.1:8000/` — you'll be redirected to login, then to the MD or
Telecaller dashboard based on role.

## 2. Creating users

The first user you create with `createsuperuser` is Django-admin-only by default.
To make it an MD:

1. Log into `/admin/`
2. Open **Users** → your user → set **Role = MD**
3. Create telecaller accounts the same way (Role = Telecaller), or via `/admin/leads/user/add/`

## 3. Uploading leads

On the MD dashboard, upload an `.xlsx`/`.xls`/`.xlsm` file with these columns
(case-insensitive; `Course` and `Email` are optional):

| Name | Phone | Education | College | Course | Email |
|------|-------|-----------|---------|--------|-------|

- **New phone number** → lead created, auto-assigned round-robin to the telecaller with
  the fewest active leads, greeted via WhatsApp automatically.
- **Existing phone number, some fields changed** → lead updated. Assignment and follow-up
  history are never touched by a re-upload.
- **Existing phone number, nothing changed** → ignored as a duplicate.

## 4. WhatsApp Cloud API setup

You need a Meta developer app with WhatsApp product enabled:

1. Get an **access token** and **phone number ID** from
   [developers.facebook.com](https://developers.facebook.com/apps) → your app → WhatsApp → API Setup
2. Put them in `.env`:
   ```
   WHATSAPP_ACCESS_TOKEN=...
   WHATSAPP_PHONE_NUMBER_ID=...
   WHATSAPP_VERIFY_TOKEN=pick-any-string
   ```
3. In Meta's app dashboard, set the webhook URL to:
   `https://yourdomain.com/webhook/whatsapp/`
   and the verify token to the same value as `WHATSAPP_VERIFY_TOKEN`.
4. Without these set, outbound messages are still logged in the DB but marked `FAILED`
   instead of actually sending — so the rest of the app works fine while you're setting
   up WhatsApp access separately.

## 5. Switching to PostgreSQL

The included `dreamcadd_schema.sql` (from our earlier step) matches this Django schema
conceptually, but **let Django manage the actual tables** via migrations — don't run
both. To point Django at Postgres:

```
DB_ENGINE=postgres
DB_NAME=dreamcadd
DB_USER=postgres
DB_PASSWORD=yourpassword
DB_HOST=localhost
DB_PORT=5432
```

then:
```bash
pip install psycopg2-binary   # uncomment it in requirements.txt first
python manage.py migrate
```

## 6. What's already tested

Everything below was run against this exact codebase before handing it to you:

- Excel import: new / updated / duplicate-ignored paths, all correct
- Round-robin auto-assignment across telecallers
- WhatsApp send (fails gracefully to `FAILED` without live credentials — doesn't crash)
- WhatsApp webhook verification handshake + incoming message logging + auto lead-status update
- Login + role-based redirect (MD → `/md/`, Telecaller → `/telecaller/`)
- Permission boundary: a telecaller gets `403 Forbidden` on a lead not assigned to them
- Sending a WhatsApp message and logging a follow-up from the lead detail page
- Django admin: all models registered and browsable

## 7. Project layout

```
dreamcadd/          # Django project settings/urls
leads/
  models.py         # User, Course, Lead, LeadAssignment, Followup, WhatsAppMessage
  services.py        # Excel import, round-robin assignment, WhatsApp send/receive
  views.py            # dashboards, lead detail, upload, webhook
  forms.py
  admin.py
  templates/leads/
manage.py
requirements.txt
.env.example
dreamcadd_schema.sql  # reference Postgres schema (optional, Django migrations are authoritative)
```

## 8. Next steps you may want

- Manual reassignment UI (currently only round-robin on import; admin can reassign via `/admin/`)
- Bulk WhatsApp broadcast (e.g. course reminders to a filtered lead list)
- Lead scoring / prioritization (mentioned as a "later" item in the original plan)
- Export leads back to Excel for offline review
- Proper production deployment (gunicorn + nginx + Postgres + HTTPS for the WhatsApp webhook,
  which Meta requires to be HTTPS)
