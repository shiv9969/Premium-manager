import asyncio
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ChatJoinRequest
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from config import settings
from database import Database


logging.basicConfig(
    level=getattr(logging, settings.log_level, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("premium_manager")
IST = ZoneInfo(settings.timezone)
db: Database
scheduler: AsyncIOScheduler | None = None


class AdminFlow(StatesGroup):
    member_id = State()
    duration = State()
    channel_id = State()


def fmt_time(value: datetime) -> str:
    return value.astimezone(IST).strftime("%d %b %Y, %I:%M %p IST")


def main_keyboard(is_admin: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="👑 My Membership", callback_data="user:status")],
        [InlineKeyboardButton(text="🔗 Get / Refresh Channel Links", callback_data="user:links")],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton(text="🛠 Admin Panel", callback_data="admin:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Add / Validate Premium", callback_data="admin:add")],
        [InlineKeyboardButton(text="👥 Active Members", callback_data="admin:members"),
         InlineKeyboardButton(text="📜 Audit Logs", callback_data="admin:logs")],
        [InlineKeyboardButton(text="📢 Manage Channels", callback_data="admin:channels")],
        [InlineKeyboardButton(text="⬅️ Main Menu", callback_data="menu:home")],
    ])


async def audit(event: str, actor: int | None = None, target: int | None = None, **details):
    try:
        await db.audit(event, actor, target, details)
    except Exception:
        log.exception("AUDIT_PERSIST_FAILED event=%s actor=%s target=%s", event, actor, target)
        raise
    log.info("AUDIT event=%s actor_id=%s target_user_id=%s details=%s",
             event, actor, target, details)


def is_admin(user_id: int) -> bool:
    return user_id in settings.admin_ids


async def require_admin(callback: CallbackQuery) -> bool:
    uid = callback.from_user.id
    if not is_admin(uid):
        await audit("ADMIN_UNAUTHORIZED", actor=uid, action="callback", callback=callback.data)
        await callback.answer("Not authorized.", show_alert=True)
        return False
    await audit("ADMIN_AUTHORIZED", actor=uid, action=callback.data or "admin_panel")
    return True


async def issue_invites(bot: Bot, user_id: int) -> int:
    channels = await db.channels()
    sent = []
    previous_access = await db.access_for_user(user_id)
    for channel in channels:
        chat_id = channel["chat_id"]
        for old in previous_access:
            if old["chat_id"] == chat_id and old["invite_link"]:
                try:
                    await bot.revoke_chat_invite_link(chat_id, old["invite_link"])
                except (TelegramBadRequest, TelegramForbiddenError):
                    pass
        try:
            await bot.unban_chat_member(chat_id=chat_id, user_id=user_id, only_if_banned=True)
        except TelegramBadRequest:
            pass
        link = await bot.create_chat_invite_link(
            chat_id=chat_id,
            creates_join_request=True,
            name=f"premium-{user_id}",
        )
        await db.save_invite(user_id, chat_id, link.invite_link)
        sent.append(f"• {channel['title']}: {link.invite_link}")
    if sent:
        await bot.send_message(
            user_id,
            "✅ Premium access is ready. Request to join using your personal links:\n\n"
            + "\n".join(sent),
        )
    else:
        await bot.send_message(
            user_id,
            "✅ Premium activated. No premium channels are configured yet; please contact an admin.",
        )
    return len(sent)


async def start_handler(message: Message):
    user = message.from_user
    is_new = await db.register_user(user.id, user.username, user.first_name)
    if is_new:
        await audit("NEW_USER", actor=user.id, target=user.id,
                    username=user.username, first_name=user.first_name)
    if is_admin(user.id):
        await audit("ADMIN_AUTHORIZED", actor=user.id, action="start")
    await message.answer(
        "Welcome to Premium Membership Manager!\n\n"
        "Use the buttons below to check your membership or get your access links.",
        reply_markup=main_keyboard(is_admin(user.id)),
    )


async def status_handler(callback: CallbackQuery):
    membership = await db.get_membership(callback.from_user.id)
    if membership and membership["expires_at"] > datetime.now(timezone.utc):
        remaining = membership["expires_at"] - datetime.now(timezone.utc)
        days = max(0, remaining.days)
        text = (
            f"🟢 <b>Premium Active</b>\n"
            f"Valid until: {fmt_time(membership['expires_at'])}\n"
            f"Remaining: {days} day(s)"
        )
    else:
        text = "⚪ <b>No active premium membership.</b>\nPlease contact an administrator."
    await callback.message.edit_text(
        text, reply_markup=main_keyboard(is_admin(callback.from_user.id))
    )
    await callback.answer()


async def refresh_links_handler(callback: CallbackQuery, bot: Bot):
    membership = await db.get_membership(callback.from_user.id)
    if not membership or membership["expires_at"] <= datetime.now(timezone.utc):
        await callback.answer("No active premium membership.", show_alert=True)
        return
    try:
        count = await issue_invites(bot, callback.from_user.id)
        await audit("ACCESS_LINKS_REFRESHED", actor=callback.from_user.id,
                    target=callback.from_user.id, channel_count=count)
        await callback.answer("Fresh access links sent.")
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        await audit("ACCESS_DELIVERY_FAILED", target=callback.from_user.id,
                    error=type(exc).__name__, message=str(exc)[:250])
        await callback.answer("Could not create links. Please contact an admin.", show_alert=True)


async def admin_home(callback: CallbackQuery):
    if not await require_admin(callback):
        return
    await callback.message.edit_text(
        "🛠 <b>Premium Manager Admin Panel</b>", reply_markup=admin_keyboard()
    )
    await callback.answer()


async def begin_add(callback: CallbackQuery, state: FSMContext):
    if not await require_admin(callback):
        return
    await state.set_state(AdminFlow.member_id)
    await callback.message.answer("Send the member's Telegram numeric ID.")
    await callback.answer()


async def receive_member_id(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await audit("ADMIN_UNAUTHORIZED", actor=message.from_user.id, action="member_id_input")
        return
    await audit("ADMIN_AUTHORIZED", actor=message.from_user.id, action="member_id_input")
    try:
        target_id = int(message.text.strip())
        if target_id <= 0:
            raise ValueError
    except (ValueError, AttributeError):
        await message.answer("Please send a valid positive Telegram numeric ID.")
        return
    user = await db.get_user(target_id)
    if not user:
        await message.answer("This user has not started the bot yet. Ask them to send /start first.")
        return
    await state.update_data(target_id=target_id)
    await state.set_state(AdminFlow.duration)
    await message.answer("Enter premium validity in days (1–3650).")


async def receive_duration(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await audit("ADMIN_UNAUTHORIZED", actor=message.from_user.id, action="duration_input")
        return
    await audit("ADMIN_AUTHORIZED", actor=message.from_user.id, action="duration_input")
    try:
        days = int(message.text.strip())
        if not 1 <= days <= 3650:
            raise ValueError
    except (ValueError, AttributeError):
        await message.answer("Enter a whole number from 1 to 3650.")
        return
    data = await state.get_data()
    target_id = data["target_id"]
    await state.update_data(days=days)
    active = await db.get_membership(target_id)
    buttons = [
        [InlineKeyboardButton(text="✅ Confirm — Extend", callback_data="validate:extend")],
        [InlineKeyboardButton(text="🔄 Confirm — Reset from now", callback_data="validate:reset")],
        [InlineKeyboardButton(text="Cancel", callback_data="validate:cancel")],
    ] if active and active["expires_at"] > datetime.now(timezone.utc) else [
        [InlineKeyboardButton(text="✅ Confirm Activation", callback_data="validate:reset")],
        [InlineKeyboardButton(text="Cancel", callback_data="validate:cancel")],
    ]
    await message.answer(
        f"Confirm premium validation?\n\nUser: <code>{target_id}</code>\n"
        f"Duration: <b>{days} days</b>\n"
        f"Current expiry: {fmt_time(active['expires_at']) if active else 'Not active'}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


async def validate_callback(callback: CallbackQuery, state: FSMContext, bot: Bot):
    if not await require_admin(callback):
        return
    if callback.data == "validate:cancel":
        await state.clear()
        await callback.message.edit_text("Validation cancelled.")
        await callback.answer()
        return
    data = await state.get_data()
    if not data.get("target_id") or not data.get("days"):
        await callback.answer("Validation session expired. Start again.", show_alert=True)
        return
    target_id, days = int(data["target_id"]), int(data["days"])
    reset = callback.data.endswith("reset")
    starts, expiry = await db.activate(target_id, days, callback.from_user.id, reset)
    await audit(
        "USER_VALIDATED", actor=callback.from_user.id, target=target_id,
        duration_days=days, renewal_mode="reset" if reset else "extend",
        starts_at=starts.isoformat(), expires_at=expiry.isoformat(),
    )
    await state.clear()
    try:
        invite_count = await issue_invites(bot, target_id)
    except (TelegramForbiddenError, TelegramBadRequest) as exc:
        await audit(
            "ACCESS_DELIVERY_FAILED", actor=callback.from_user.id, target=target_id,
            error=type(exc).__name__, message=str(exc)[:300],
        )
        await callback.message.edit_text(
            f"✅ Membership saved until {fmt_time(expiry)}, but access delivery failed. "
            "Check bot permissions and whether the user has started the bot."
        )
        await callback.answer("Saved; access delivery needs attention.", show_alert=True)
        return
    await callback.message.edit_text(
        f"✅ User <code>{target_id}</code> validated.\n"
        f"Duration: {days} days\nExpiry: {fmt_time(expiry)}\n"
        f"Channel links created: {invite_count}"
    )
    await callback.answer("Membership activated.")


async def members_handler(callback: CallbackQuery):
    if not await require_admin(callback):
        return
    rows = await db.active_members()
    if not rows:
        text = "No active members found."
    else:
        lines = [
            f"• <code>{r['telegram_id']}</code> — {fmt_time(r['expires_at'])}"
            for r in rows
        ]
        text = "<b>Active members</b>\n" + "\n".join(lines)
    await callback.message.edit_text(text, reply_markup=admin_keyboard())
    await callback.answer()


async def logs_handler(callback: CallbackQuery):
    if not await require_admin(callback):
        return
    rows = await db.latest_logs(15)
    if not rows:
        text = "No audit events recorded yet."
    else:
        lines = []
        for row in rows:
            when = fmt_time(row["created_at"])
            details = str(row["details"])[:110]
            lines.append(
                f"• <b>{row['event']}</b> — {when}\n"
                f"  actor={row['actor_id']} target={row['target_id']} {details}"
            )
        text = "<b>Recent audit logs</b>\n\n" + "\n\n".join(lines)
    await callback.message.edit_text(text[:4000], reply_markup=admin_keyboard())
    await callback.answer()


async def channels_handler(callback: CallbackQuery):
    if not await require_admin(callback):
        return
    rows = await db.channels()
    listing = "\n".join(
        f"• {r['title']} — <code>{r['chat_id']}</code>" for r in rows
    ) or "No channels configured."
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Add Channel", callback_data="channel:add")],
        [InlineKeyboardButton(text="⬅️ Back", callback_data="admin:home")],
    ])
    await callback.message.edit_text(
        "<b>Premium Channels</b>\n" + listing, reply_markup=keyboard
    )
    await callback.answer()


async def begin_channel_add(callback: CallbackQuery, state: FSMContext):
    if not await require_admin(callback):
        return
    await state.set_state(AdminFlow.channel_id)
    await callback.message.answer(
        "Send the private channel numeric chat ID (usually starts with -100)."
    )
    await callback.answer()


async def receive_channel_id(message: Message, state: FSMContext, bot: Bot):
    if not is_admin(message.from_user.id):
        await audit("ADMIN_UNAUTHORIZED", actor=message.from_user.id, action="channel_add_input")
        return
    await audit("ADMIN_AUTHORIZED", actor=message.from_user.id, action="channel_add_input")
    try:
        chat_id = int(message.text.strip())
        chat = await bot.get_chat(chat_id)
        me = await bot.get_me()
        member = await bot.get_chat_member(chat_id, me.id)
        if member.status != "administrator":
            raise ValueError("Bot is not an administrator in this channel")
        if not getattr(member, "can_invite_users", False) or not getattr(
            member, "can_restrict_members", False
        ):
            raise ValueError("Bot needs invite-users and restrict-members permissions")
    except Exception as exc:
        await message.answer(
            f"Could not add channel: {str(exc)[:250]}\n"
            "Check the chat ID and bot admin permissions."
        )
        return
    await db.add_channel(chat_id, chat.title or str(chat_id), message.from_user.id)
    await audit(
        "CHANNEL_ADDED", actor=message.from_user.id,
        chat_id=chat_id, title=chat.title,
    )
    await state.clear()
    await message.answer(f"✅ Added channel: {chat.title}")


async def join_request_handler(request: ChatJoinRequest, bot: Bot):
    invite = request.invite_link.invite_link if request.invite_link else None
    record = await db.get_access_by_invite(request.chat.id, invite) if invite else None
    membership = await db.get_membership(request.from_user.id)
    valid = bool(
        record
        and record["user_id"] == request.from_user.id
        and record["status"] == "pending"
        and membership
        and membership["expires_at"] > datetime.now(timezone.utc)
    )
    if valid:
        await bot.approve_chat_join_request(request.chat.id, request.from_user.id)
        await db.set_access_status(request.from_user.id, request.chat.id, "joined")
        try:
            await bot.revoke_chat_invite_link(request.chat.id, invite)
        except (TelegramBadRequest, TelegramForbiddenError):
            log.warning("Could not revoke used invite for user_id=%s chat_id=%s",
                        request.from_user.id, request.chat.id)
        await audit(
            "JOIN_APPROVED", target=request.from_user.id, chat_id=request.chat.id
        )
    else:
        await bot.decline_chat_join_request(request.chat.id, request.from_user.id)
        await audit(
            "JOIN_REJECTED", target=request.from_user.id,
            chat_id=request.chat.id, reason="no matching active membership/invite",
        )


async def process_warnings(bot: Bot, hours: int):
    rows = await db.warning_candidates(hours)
    for row in rows:
        try:
            await bot.send_message(
                row["user_id"],
                f"⏰ Premium expiry reminder: your membership expires in {hours} hours, "
                f"at {fmt_time(row['expires_at'])}. Please contact an admin to renew.",
            )
            await audit(
                "WARNING_SENT", target=row["user_id"], hours=hours,
                membership_id=row["id"], expires_at=row["expires_at"].isoformat(),
            )
        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            await audit(
                "WARNING_FAILED", target=row["user_id"], hours=hours,
                membership_id=row["id"], error=type(exc).__name__,
                message=str(exc)[:200],
            )


async def process_expiries(bot: Bot):
    due = await db.due_memberships()
    for row in due:
        await db.mark_expired(row["id"])
        await audit(
            "MEMBER_EXPIRED", target=row["user_id"], membership_id=row["id"],
            expires_at=row["expires_at"].isoformat(),
        )

    # Retry all outstanding channel removals for expired memberships on every pass.
    pending = await db.expired_access()
    for access in pending:
        user_id, chat_id = access["user_id"], access["chat_id"]
        if access["invite_link"]:
            try:
                await bot.revoke_chat_invite_link(chat_id, access["invite_link"])
            except (TelegramBadRequest, TelegramForbiddenError):
                pass
        try:
            await bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
            await db.set_access_status(user_id, chat_id, "removed")
            await audit("MEMBER_REMOVED", target=user_id, chat_id=chat_id, result="success")
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            await db.set_access_status(user_id, chat_id, "failed")
            await audit(
                "MEMBER_REMOVED", target=user_id, chat_id=chat_id,
                result="failed", error=type(exc).__name__, message=str(exc)[:200],
            )


async def scheduler_tick(bot: Bot):
    # Reconciliation runs repeatedly, so missed warnings/removals are recovered after downtime.
    try:
        await process_warnings(bot, 48)
        await process_warnings(bot, 24)
        await process_expiries(bot)
    except Exception:
        log.exception("SCHEDULER_RECONCILIATION_FAILED")


async def on_startup(bot: Bot):
    global db, scheduler
    db = await Database.connect(settings.database_url)
    scheduler = AsyncIOScheduler(timezone=IST)
    scheduler.add_job(
        scheduler_tick, IntervalTrigger(minutes=10), args=[bot],
        id="membership_reconciliation", replace_existing=True,
        max_instances=1, coalesce=True,
    )
    scheduler.start()
    log.info(
        "Premium Manager started; timezone=%s configured_admins=%d",
        settings.timezone, len(settings.admin_ids),
    )
    await scheduler_tick(bot)


async def on_shutdown(bot: Bot):
    global scheduler
    if scheduler:
        scheduler.shutdown(wait=False)
        scheduler = None
    await db.close()


async def main():
    bot = Bot(
        settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    dp.message.register(start_handler, CommandStart())
    dp.message.register(
        lambda m: m.answer("Use /start to open the membership menu."),
        Command("help"),
    )
    dp.callback_query.register(admin_home, F.data == "admin:home")
    dp.callback_query.register(status_handler, F.data == "user:status")
    dp.callback_query.register(refresh_links_handler, F.data == "user:links")
    dp.callback_query.register(
        lambda c: c.message.edit_text(
            "Main menu", reply_markup=main_keyboard(is_admin(c.from_user.id))
        ),
        F.data == "menu:home",
    )
    dp.callback_query.register(begin_add, F.data == "admin:add")
    dp.callback_query.register(validate_callback, F.data.startswith("validate:"))
    dp.callback_query.register(members_handler, F.data == "admin:members")
    dp.callback_query.register(logs_handler, F.data == "admin:logs")
    dp.callback_query.register(channels_handler, F.data == "admin:channels")
    dp.callback_query.register(begin_channel_add, F.data == "channel:add")
    dp.message.register(receive_member_id, AdminFlow.member_id)
    dp.message.register(receive_duration, AdminFlow.duration)
    dp.message.register(receive_channel_id, AdminFlow.channel_id)
    dp.chat_join_request.register(join_request_handler)

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
