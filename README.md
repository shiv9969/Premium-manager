# Premium Manager Bot

Telegram premium-membership manager using Python, Aiogram 3, PostgreSQL and APScheduler.

## Features

- User registration and membership status.
- Owner-managed admins from inside the bot; admin assignments are stored in PostgreSQL.
- Inline admin panel.
- Manual premium validation with custom duration from 1 minute to unlimited.
- Admin `/addpremium` command with minute/hour/day/week/month/year or unlimited duration.
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
2. Set BOT_TOKEN, OWNER_IDS (the bootstrap owner Telegram ID), DATABASE_URL, PUBLIC_CHANNEL_URL, PREMIUM_UPI_ID and LOG_CHANNEL_ID. Keep LOG_LEVEL as INFO; for backward compatibility, a negative Telegram channel ID placed in LOG_LEVEL is also detected. Admins are assigned later from the bot's Owner → Manage Admins panel; no ADMIN_IDS list is needed.
3. Start: docker compose up -d --build
4. Inspect logs: docker compose logs -f bot

## Run locally

Install requirements.txt, start PostgreSQL, set environment variables, then run python app.py.

## Admin workflow

- Open the bot and use Admin Panel.
- Choose Add / Validate Premium for the guided flow, or use `/addpremium USER_ID 1 month`. You can also reply to a user's message with `/addpremium 1 month`.
- Supported examples: `1 minute`, `12 hours`, `7 days`, `1 month` (30 days), `1 year` (365 days), and `unlimited` / `forever` / `permanent`.
- Timed access has a minimum of 1 minute. Unlimited memberships have no expiry and are not removed by the expiry scheduler.
- Confirm the displayed activation and expiry details.
- Use Manage Channels to register private channel IDs.
- Use Members to inspect status and renew/revoke subscriptions.
- Use Audit Logs to inspect recent security and membership events.

## Premium duration command

`/addpremium USER_ID DURATION` grants or extends premium immediately. Alternatively, reply to a user message with `/addpremium DURATION`. The user must have started the bot before it can receive a private message. Timed durations accept minute, hour, day, week, month (30 days), and year (365 days). Use `unlimited` for permanent access.

## Important Telegram behavior

- A user must start the bot before it can receive private messages.
- Invite links are join-request links. The bot approves a request only when the requester ID has an active membership and a valid invite record.
- Telegram must deliver channel join-request updates to the bot.
- Expired users are banned/removed per channel. Renewal unbans them where needed and issues fresh invite links.
- Removal failures caused by lost admin permissions are logged and retried by reconciliation.

## Security

- Never commit .env or share the bot token.
- Configure only the bootstrap owner in OWNER_IDS as a numeric Telegram ID. The owner can assign/remove admins inside the bot; those assignments persist in PostgreSQL.
- Use a strong PostgreSQL password and restrict database network access.
- Back up PostgreSQL regularly.
- Use minimum required bot permissions.

## Scope

Manual activation only; no payment gateway or web dashboard. Test in private test channels before production use.


## Persistence and redeployment

All users, admin assignments, membership start/expiry timestamps, invite access records, channel configuration and audit logs live in PostgreSQL. Restarting the bot or redeploying it to another host does not reset these timestamps, provided the new deployment connects to the same persistent PostgreSQL database using the same DATABASE_URL. The Docker Compose database volume is persistent on the same host; for a move to another host, migrate/restore the PostgreSQL database and configure the new deployment with that database URL. Do not delete the database volume when redeploying. The reconciliation scheduler runs on startup and every 10 minutes to process overdue reminders and expired access after downtime.

OWNER_IDS is the bootstrap/root owner setting and remains in deployment environment variables for security. It is not duplicated as an ADMIN_IDS list; ordinary admin assignments are stored in the database and managed in the bot.


## Test pricing and private logs

The membership menu currently displays demo prices (₹99 / ₹249 / ₹449 / ₹799) and a deliberately invalid placeholder UPI ID. The Buy Premium screen is marked TEST MODE and explicitly tells users not to send money. Replace these only after confirming real prices and a real payment address. Set `LOG_CHANNEL_ID` to your private Telegram channel ID (usually starts with `-100`) and add the bot as an administrator with permission to post. For compatibility, a negative channel ID in `LOG_LEVEL` is also treated as the private log channel ID; normal Python logging remains at INFO. Audit notifications are sent only to the configured private log channel.


## Channel management

The fixed public-community button is removed from the normal user menu. Add channels from Admin Panel → Manage Channels using the Telegram numeric chat ID. Both public and private Telegram channels are supported, provided the bot is an administrator with invite-user and member-restriction permissions. All configured channels are treated as premium-access channels.
