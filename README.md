# Premium Manager Bot

Telegram premium-membership manager using Python, Aiogram 3, PostgreSQL and APScheduler.

## Features

- User registration and membership status.
- Multiple authorized admins, configured by numeric Telegram IDs.
- Inline admin panel.
- Manual premium validation with custom duration in days.
- Multiple channel registration and permission checks.
- Per-member join-request invite links and identity verification.
- 48-hour and 24-hour expiry warnings.
- Expiry reconciliation after restarts.
- Removal from all configured channels on expiry.
- Renewal with extend or reset semantics.
- Persistent audit events and readable application logs.

## Audit events

The audit_logs table and application logger record:
- NEW_USER: first-time user registration.
- ADMIN_AUTHORIZED: authorized admin accessed an admin workflow.
- USER_VALIDATED: admin activated a member, including duration and expiry.
- WARNING_SENT / WARNING_FAILED: 48h and 24h warning delivery results.
- MEMBER_EXPIRED: membership expiry processed.
- MEMBER_REMOVED: per-channel removal result.
- ADMIN_UNAUTHORIZED: unauthorized user attempted an admin action.

Each audit record includes an IST timestamp, actor ID where applicable, target user ID where applicable, and structured details. Secrets are never logged.

## Requirements

- Python 3.12+ or Docker.
- Telegram bot token from BotFather.
- PostgreSQL 16+.
- At least one owner Telegram numeric ID.
- Add the bot as administrator in every premium channel with invite/join-request and ban permissions.

## Run with Docker

1. Copy .env.example to .env.
2. Set BOT_TOKEN, OWNER_IDS, and a strong POSTGRES_PASSWORD.
3. Start: docker compose up -d --build
4. Inspect logs: docker compose logs -f bot

## Run locally

Install requirements.txt, start PostgreSQL, set environment variables, then run python app.py.

## Admin workflow

- Open the bot and use Admin Panel.
- Choose Add / Validate Premium, enter the member Telegram numeric ID, then enter validity in days.
- Confirm the displayed activation and expiry details.
- Use Manage Channels to register private channel IDs.
- Use Members to inspect status and renew/revoke subscriptions.
- Use Audit Logs to inspect recent security and membership events.

## Important Telegram behavior

- A user must start the bot before it can receive private messages.
- Invite links are join-request links. The bot approves a request only when the requester ID has an active membership and a valid invite record.
- Telegram must deliver channel join-request updates to the bot.
- Expired users are banned/removed per channel. Renewal unbans them where needed and issues fresh invite links.
- Removal failures caused by lost admin permissions are logged and retried by reconciliation.

## Security

- Never commit .env or share the bot token.
- Configure owner/admin IDs as numeric Telegram IDs, not usernames.
- Use a strong PostgreSQL password and restrict database network access.
- Back up PostgreSQL regularly.
- Use minimum required bot permissions.

## Scope

Manual activation only; no payment gateway or web dashboard. Test in private test channels before production use.
