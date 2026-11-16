import asyncio
import logging
import html
from datetime import datetime, timezone
import re
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import CommandStart, Command, CommandObject
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


logging.Formatter.converter = lambda timestamp: datetime.fromtimestamp(timestamp, IST).timetuple()
logging.basicConfig(
    level=getattr(logging, settings.log_level, logging.INFO),
    format="%(asctime)s IST %(levelname)s %(name)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("premium_manager")
IST = ZoneInfo(settings.timezone)
db: Database
scheduler: AsyncIOScheduler | None = None
authorized_admin_ids: set[int] = set()
bot_instance: Bot | None = None


class AdminFlow(StatesGroup):
    channel_id = State()
    admin_id = State()


def fmt_time(value: datetime) -> str:
    return value.astimezone(IST).strftime("%d %b %Y, %I:%M %p IST")


def main_keyboard(is_admin: bool) -> InlineKeyboardMarkup:
    if is_admin:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🛠 Admin Panel", callback_data="admin:home")]
        ])
    rows = [
        [InlineKeyboardButton(text="ℹ️ Membership Info", callback_data="user:membership_info")],
        [InlineKeyboardButton(text="💎 Membership Plans", callback_data="user:plans")],
        [InlineKeyboardButton(text="👑 My Membership", callback_data="user:status")],
        [InlineKeyboardButton(text="💳 Buy Premium", callback_data="user:buy")],
        [InlineKeyboardButton(text="🔗 My Premium Channel Links", callback_data="user:links")],
    ]
    if settings.owner_ids:
        rows.append([InlineKeyboardButton(text="📩 Contact Admin", url=f"tg://user?id={min(settings.owner_ids)}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def user_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Buy Premium", callback_data="user:buy")],
        [InlineKeyboardButton(text="⬅️ Main Menu", callback_data="menu:home")],
    ])


async def user_plans_handler(callback: CallbackQuery):
    plans = [
        ("1 Month", "30 days", "₹99"),
        ("3 Months", "90 days", "₹249"),
        ("6 Months", "180 days", "₹449"),
        ("12 Months", "365 days", "₹799"),
    ]
    plan_lines = "\n".join(f"• <b>{name}</b> — {duration} — <b>{price}</b>" for name, duration, price in plans)
    text = (
        "💎 <b>Premium Membership Plans</b>\n\n"
        "Choose the membership duration that suits you. Premium access unlocks our private channels "
        "and exclusive member content.\n\n"
        f"{plan_lines}\n\n"
        "🧪 <b>TEST PRICES</b> — demo amounts only; final pricing may change.\n"
        "Tap <b>Buy Premium</b> to view test payment details."
    )
    await callback.message.edit_text(text, reply_markup=user_back_keyboard())
    await callback.answer()


async def user_buy_handler(callback: CallbackQuery):
    # Deliberately use an invalid placeholder while payment is in test mode.
    upi_id = "premium-test@invalid"
    if upi_id:
        text = (
            "🧪 <b>TEST MODE — DO NOT SEND MONEY</b>\n\n"
            f"Demo UPI ID: <code>{html.escape(upi_id)}</code>\n"
            "This is a placeholder for testing the menu only, not a real payment address.\n\n"
            "⚠️ Payment submission and verification are not enabled yet."
        )
    else:
        text = (
            "💳 <b>Buy Premium Membership</b>\n\n"
            "UPI payment details are being configured. Please check back soon or contact an admin."
        )
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 View Plans", callback_data="user:plans")],
        [InlineKeyboardButton(text="⬅️ Main Menu", callback_data="menu:home")],
    ]))
    await callback.answer()


async def user_public_channel_handler(callback: CallbackQuery):
    if settings.public_channel_url.startswith(("https://t.me/", "https://telegram.me/")):
        await callback.answer("Tap the community button to open our channel.")
    else:
        await callback.answer("Public channel link has not been configured yet.", show_alert=True)


async def user_membership_info_handler(callback: CallbackQuery):
    await callback.message.edit_text(
        "✨ <b>Welcome to our Premium Community!</b>\n\n"
        "🚀 Get closer to exclusive content, valuable updates and a community built for people "
        "who want to learn, grow and stay ahead.\n\n"
        "💎 Choose a membership plan to unlock private-channel access. "
        "Your access remains active for the plan duration.",
        reply_markup=user_back_keyboard(),
    )
    await callback.answer()


def admin_keyboard(is_owner: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="📢 Manage Channels", callback_data="admin:channels")],
    ]
    if is_owner:
        rows.append([InlineKeyboardButton(text="🛡 Manage Admins", callback_data="owner:admins")])
    rows.append([InlineKeyboardButton(text="⬅️ Main Menu", callback_data="menu:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def audit(event: str, actor: int | None = None, target: int | None = None, **details):
    # Audit logging disabled as requested.
    return


async def channels_view():
    rows = await db.channels()
    keyboard_rows = []
    for channel in rows:
        title = channel["title"]
        if len(title) > 32:
            title = title[:29] + "..."
        keyboard_rows.append([
            InlineKeyboardButton(text=f"📢 {title}", callback_data=f"channel:show:{channel['chat_id']}"),
            InlineKeyboardButton(text="❌", callback_data=f"channel:remove:{channel['chat_id']}"),
        ])
    keyboard_rows.append([InlineKeyboardButton(text="➕ Add Channel", callback_data="channel:add")])
    keyboard_rows.append([InlineKeyboardButton(text="⬅️ Admin Panel", callback_data="admin:home")])
    return (
        "<b>📢 Channel Management</b>\nAdd premium channels here. Members receive personal join-request links for every active channel.\n\n"
        f"<b>Configured channels:</b> {len(rows)}",
        InlineKeyboardMarkup(inline_keyboard=keyboard_rows),
    )


async def admins_view():
    rows = await db.list_admins()
    keyboard_rows = []
    for admin in rows:
        uid = int(admin["telegram_id"])
        role = admin["role"]
        row = [InlineKeyboardButton(text=f"👤 {uid} ({role})", callback_data=f"owner:admininfo:{uid}")]
        if role != "owner":
            row.append(InlineKeyboardButton(text="❌", callback_data=f"owner:remove:{uid}"))
        keyboard_rows.append(row)
    keyboard_rows.extend([
        [InlineKeyboardButton(text="➕ Add Admin", callback_data="owner:add_admin")],
        [InlineKeyboardButton(text="⬅️ Admin Panel", callback_data="admin:home")],
    ])
    return (
        "<b>🛡 Admin Management</b>\nManage who can access the admin panel. Premium is assigned using /addpremium.\n\n"
        f"<b>Assigned admins:</b> {len(rows)}",
        InlineKeyboardMarkup(inline_keyboard=keyboard_rows),
    )


async def edit_saved_panel(message: Message, state: FSMContext, text: str, markup=None):
    data = await state.get_data()
    chat_id, message_id = data.get("panel_chat_id"), data.get("panel_message_id")
    if chat_id and message_id:
        await message.bot.edit_message_text(
            chat_id=chat_id, message_id=message_id, text=text, reply_markup=markup
        )


def is_admin(user_id: int) -> bool:
    return user_id in authorized_admin_ids or user_id in settings.owner_ids


def is_owner(user_id: int) -> bool:
    return user_id in settings.owner_ids


async def require_admin(callback: CallbackQuery) -> bool:
    uid = callback.from_user.id
    if not is_admin(uid):
        await audit("ADMIN_UNAUTHORIZED", actor=uid, action="callback", callback=callback.data)
        await callback.answer("Not authorized.", show_alert=True)
        return False
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


DURATION_UNITS = {
    "minute": 60, "minutes": 60, "min": 60, "mins": 60, "m": 60,
    "hour": 3600, "hours": 3600, "hr": 3600, "hrs": 3600, "h": 3600,
    "day": 86400, "days": 86400, "d": 86400,
    "week": 604800, "weeks": 604800, "w": 604800,
    "month": 2592000, "months": 2592000, "mo": 2592000,
    "year": 31536000, "years": 31536000, "y": 31536000,
}

def parse_premium_duration(value: str):
    raw = value.strip().lower()
    if raw in {"unlimited", "forever", "permanent", "lifetime", "life"}:
        return None, "Unlimited"
    parts = raw.split()
    if len(parts) != 2 or not parts[0].isdigit():
        raise ValueError("Use 1 minute, 12 hours, 1 month, 365 days, or unlimited.")
    amount, unit = int(parts[0]), parts[1]
    if amount < 1 or unit not in DURATION_UNITS:
        raise ValueError("Unsupported duration. Try 1 minute, 1 month, or unlimited.")
    seconds = amount * DURATION_UNITS[unit]
    if seconds < 60:
        raise ValueError("Minimum premium duration is 1 minute.")
    if seconds > 100 * 365 * 86400:
        raise ValueError("Maximum timed duration is 100 years. Use unlimited for permanent access.")
    return seconds, f"{amount} {unit}"

async def addpremium_command(message: Message, command: CommandObject, bot: Bot):
    admin_id = message.from_user.id
    if not is_admin(admin_id):
        await audit("ADMIN_UNAUTHORIZED", actor=admin_id, action="addpremium_command")
        await message.answer("You are not authorized to use this command.")
        return
    args = (command.args or "").strip()
    try:
        await message.delete()
    except (TelegramBadRequest, TelegramForbiddenError):
        pass
    target_id = None
    duration_text = args
    if message.reply_to_message and message.reply_to_message.from_user:
        target = message.reply_to_message.from_user
        target_id = target.id
        if not await db.get_user(target_id):
            await db.register_user(target_id, target.username, target.first_name)
            await audit("NEW_USER", actor=target_id, target=target_id, username=target.username, first_name=target.first_name, source="admin_reply_addpremium")
    else:
        parts = args.split(maxsplit=1)
        if len(parts) == 2 and parts[0].isdigit():
            target_id = int(parts[0])
            duration_text = parts[1]
    if not target_id or not duration_text:
        await message.answer("Usage: /addpremium USER_ID 1 month\nOr reply to a user message with /addpremium 1 month.\nExamples: 1 minute, 12 hours, 1 month, 365 days, unlimited.")
        return
    if not await db.get_user(target_id):
        await message.answer("User has not started the bot yet. Ask them to send /start, then try again.")
        return
    try:
        seconds, duration_label = parse_premium_duration(duration_text)
    except ValueError as exc:
        await message.answer(str(exc))
        return
    starts, expiry = await db.grant_premium(target_id, seconds, admin_id)
    await audit("PREMIUM_ADDED_COMMAND", actor=admin_id, target=target_id, duration=duration_label, duration_seconds=seconds, starts_at=starts.isoformat(), expires_at=expiry.isoformat() if expiry else None, unlimited=expiry is None)
    try:
        links = await issue_invites(bot, target_id)
        delivery = f"Channel links created: {links}"
    except (TelegramForbiddenError, TelegramBadRequest) as exc:
        await audit("ACCESS_DELIVERY_FAILED", actor=admin_id, target=target_id, error=type(exc).__name__, message=str(exc)[:250])
        delivery = "Premium saved, but channel links could not be delivered; check bot permissions."
    expiry_text = fmt_time(expiry) if expiry else "Unlimited"
    await message.answer(f"Premium added for {target_id}.\nDuration: {duration_label}\nExpiry: {expiry_text}\n{delivery}")

async def start_handler(message: Message):
    user = message.from_user
    is_new = await db.register_user(user.id, user.username, user.first_name)
    if is_new:
        await audit("NEW_USER", actor=user.id, target=user.id,
                    username=user.username, first_name=user.first_name)
    if is_admin(user.id):
    if is_admin(user.id):
        welcome = (
            "🛠 <b>Welcome, Admin!</b>\n\n"
            "Manage active members, premium channels and admin access from your control panel."
        )
    else:
        welcome = (
            "🌟 <b>Welcome to Our Premium Community!</b> 🌟\n\n"
            "🚀 Be part of a space built for growth, exclusive updates and premium experiences.\n"
            "💎 Unlock members-only content, stay connected and get more value from every update.\n\n"
            "✨ Your next level starts here — explore our community and choose your membership plan below!"
        )
    await message.answer(welcome, reply_markup=main_keyboard(is_admin(user.id)))


async def status_handler(callback: CallbackQuery):
    membership = await db.get_membership(callback.from_user.id)
    if membership and (membership["expires_at"] is None or membership["expires_at"] > datetime.now(timezone.utc)):
        if membership["expires_at"] is None:
            text = "🟢 <b>Premium Active</b>\nValid until: <b>Unlimited</b>"
        else:
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
    if not membership or (membership["expires_at"] is not None and membership["expires_at"] <= datetime.now(timezone.utc)):
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
        "🛠 <b>Premium Manager Admin Panel</b>", reply_markup=admin_keyboard(is_owner(callback.from_user.id))
    )
    await callback.answer()


async def begin_add(callback: CallbackQuery, state: FSMContext):
    if not await require_admin(callback):
        return
    await callback.answer("Use /addpremium USER_ID 1 month")


async def channels_handler(callback: CallbackQuery):
    if not await require_admin(callback):
        return
    text, keyboard = await channels_view()
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


async def show_channel_callback(callback: CallbackQuery):
    if not await require_admin(callback):
        return
    chat_id = int(callback.data.rsplit(":", 1)[1])
    channel = next((c for c in await db.channels() if int(c["chat_id"]) == chat_id), None)
    if not channel:
        await callback.answer("Channel not found.", show_alert=True)
        return
    await callback.answer(f"{channel['title']} • ID {chat_id}", show_alert=True)


async def remove_channel_callback(callback: CallbackQuery):
    if not await require_admin(callback):
        return
    chat_id = int(callback.data.rsplit(":", 1)[1])
    channel = next((c for c in await db.channels() if int(c["chat_id"]) == chat_id), None)
    if not channel:
        await callback.answer("Channel already removed.", show_alert=True)
        return
    await db.remove_channel(chat_id)
    await audit("CHANNEL_REMOVED", actor=callback.from_user.id, chat_id=chat_id, title=channel["title"])
    text, keyboard = await channels_view()
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer("Channel removed.")


async def begin_channel_add(callback: CallbackQuery, state: FSMContext):
    if not await require_admin(callback):
        return
    await state.update_data(panel_chat_id=callback.message.chat.id, panel_message_id=callback.message.message_id)
    await state.set_state(AdminFlow.channel_id)
    await callback.message.edit_text(
        "<b>➕ Add Premium Channel</b>\nSend the channel numeric ID (usually starts with -100).\n\n"
        "First add the bot as channel admin with invite and restrict permissions.\n"
        "Send /cancel to stop."
    )
    await callback.answer()


async def receive_channel_id(message: Message, state: FSMContext, bot: Bot):
    if not is_admin(message.from_user.id):
        await audit("ADMIN_UNAUTHORIZED", actor=message.from_user.id, action="channel_add_input")
        return
    await audit("ADMIN_AUTHORIZED", actor=message.from_user.id, action="channel_add_input")
    try:
        chat_id = int(message.text.strip())
        try:
            await message.delete()
        except (TelegramBadRequest, TelegramForbiddenError):
            pass
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
        await edit_saved_panel(
            message, state,
            f"<b>Could not add channel</b>\n{html.escape(str(exc)[:250])}\n\n"
            "Check the ID and bot admin permissions. Send another ID or /cancel."
        )
        return
    await db.add_channel(chat_id, chat.title or str(chat_id), message.from_user.id)
    await audit("CHANNEL_ADDED", actor=message.from_user.id, chat_id=chat_id, title=chat.title)
    text, markup = await channels_view()
    await edit_saved_panel(message, state, text, markup)
    await state.clear()



async def owner_admins_handler(callback: CallbackQuery):
    if not is_owner(callback.from_user.id):
        await audit("OWNER_UNAUTHORIZED", actor=callback.from_user.id, action="manage_admins")
        await callback.answer("Only the bot owner can manage admins.", show_alert=True)
        return
    text, keyboard = await admins_view()
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


async def owner_admin_info_callback(callback: CallbackQuery):
    if not is_owner(callback.from_user.id):
        await callback.answer("Only the owner can view admin details.", show_alert=True)
        return
    target_id = int(callback.data.rsplit(":", 1)[1])
    row = next((r for r in await db.list_admins() if int(r["telegram_id"]) == target_id), None)
    if not row:
        await callback.answer("Admin not found.", show_alert=True)
        return
    await callback.answer(f"Telegram ID: {target_id} • Role: {row['role']}", show_alert=True)


async def remove_admin_callback(callback: CallbackQuery):
    if not is_owner(callback.from_user.id):
        await callback.answer("Only the owner can remove admins.", show_alert=True)
        return
    target_id = int(callback.data.rsplit(":", 1)[1])
    if target_id in settings.owner_ids:
        await callback.answer("Owner access cannot be removed here.", show_alert=True)
        return
    removed = await db.remove_admin(target_id)
    authorized_admin_ids.discard(target_id)
    await audit("ADMIN_REMOVED", actor=callback.from_user.id, target=target_id, removed=removed)
    text, keyboard = await admins_view()
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer("Admin removed." if removed else "Admin not found.")


async def owner_begin_admin_change(callback: CallbackQuery, state: FSMContext):
    if not is_owner(callback.from_user.id):
        await callback.answer("Only the owner can manage admins.", show_alert=True)
        return
    action = "add"
    await state.update_data(admin_action=action, panel_chat_id=callback.message.chat.id, panel_message_id=callback.message.message_id)
    await state.set_state(AdminFlow.admin_id)
    await callback.message.edit_text(
        "<b>➕ Add Admin</b>\nSend the Telegram numeric ID of the person to assign as admin.\n\n"
        "Admins can assign premium with /addpremium and manage channels. Only the owner can manage admins.\n\n"
        "Send /cancel to stop."
    )
    await callback.answer()


async def owner_receive_admin_id(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id):
        await audit("OWNER_UNAUTHORIZED", actor=message.from_user.id, action="admin_id_input")
        return
    try:
        target_id = int(message.text.strip())
        if target_id <= 0:
            raise ValueError
    except (ValueError, AttributeError):
        await edit_saved_panel(message, state, "Please send a valid positive Telegram numeric ID. Try again or send /cancel.")
        return
    try:
        await message.delete()
    except (TelegramBadRequest, TelegramForbiddenError):
        pass
    data = await state.get_data()
    action = data.get("admin_action")
    if action == "add":
        if target_id in settings.owner_ids:
            await edit_saved_panel(message, state, "This user is already an owner.")
        else:
            await db.add_admin(target_id, message.from_user.id)
            authorized_admin_ids.add(target_id)
            await audit("ADMIN_ASSIGNED", actor=message.from_user.id, target=target_id)
            text, markup = await admins_view()
            await edit_saved_panel(message, state, text, markup)
    elif action == "remove":
        if target_id in settings.owner_ids:
            await edit_saved_panel(message, state, "Owner access cannot be removed here.")
        else:
            removed = await db.remove_admin(target_id)
            authorized_admin_ids.discard(target_id)
            await audit("ADMIN_REMOVED", actor=message.from_user.id, target=target_id, removed=removed)
            text, markup = await admins_view()
            await edit_saved_panel(message, state, text, markup)
    else:
        await edit_saved_panel(message, state, "Admin action expired. Open Manage Admins again.")
    await state.clear()


async def join_request_handler(request: ChatJoinRequest, bot: Bot):
    invite = request.invite_link.invite_link if request.invite_link else None
    record = await db.get_access_by_invite(request.chat.id, invite) if invite else None
    membership = await db.get_membership(request.from_user.id)
    valid = bool(
        record
        and record["user_id"] == request.from_user.id
        and record["status"] == "pending"
        and membership
        and (membership["expires_at"] is None or membership["expires_at"] > datetime.now(timezone.utc))
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
    users_to_notify = set()
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
            users_to_notify.add(user_id)
            await audit("MEMBER_REMOVED", target=user_id, chat_id=chat_id, result="success")
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            await db.set_access_status(user_id, chat_id, "failed")
            await audit(
                "MEMBER_REMOVED", target=user_id, chat_id=chat_id,
                result="failed", error=type(exc).__name__, message=str(exc)[:200],
            )

    for user_id in users_to_notify:
        access_rows = await db.access_for_user(user_id)
        if access_rows and all(row["status"] == "removed" for row in access_rows):
            try:
                keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="📩 Contact Admin", url=f"tg://user?id={min(settings.owner_ids)}")]
                ]) if settings.owner_ids else None
                await bot.send_message(
                    user_id,
                    "⛔ <b>Premium over!</b> You have been removed from all premium channels.\n\n"
                    "Firse premium chahiye toh Contact Admin button par tap karke admin se baat karein.",
                    reply_markup=keyboard,
                )
            except (TelegramForbiddenError, TelegramBadRequest):
                log.info("Could not send expiry notice to user_id=%s", user_id)


async def scheduler_tick(bot: Bot):
    # Reconciliation runs repeatedly, so missed warnings/removals are recovered after downtime.
    try:
        await process_warnings(bot, 48)
        await process_warnings(bot, 24)
        await process_expiries(bot)
    except Exception as exc:
        log.exception("SCHEDULER_RECONCILIATION_FAILED")
        try:
            await audit("SCHEDULER_ERROR", error=type(exc).__name__, message=str(exc)[:500])
        except Exception:
            log.exception("SCHEDULER_ERROR_AUDIT_FAILED")


async def on_startup(bot: Bot):
    global db, scheduler, bot_instance
    bot_instance = bot
    db = await Database.connect(settings.database_url)
    await db.bootstrap_owners(settings.owner_ids)
    authorized_admin_ids.clear()
    authorized_admin_ids.update(await db.admin_ids())
    scheduler = AsyncIOScheduler(timezone=IST)
    scheduler.add_job(
        scheduler_tick, IntervalTrigger(minutes=10), args=[bot],
        id="membership_reconciliation", replace_existing=True,
        max_instances=1, coalesce=True,
    )
    scheduler.start()
    log.info(
        "Premium Manager started; timezone=%s configured_admins=%d",
        settings.timezone, len(authorized_admin_ids | settings.owner_ids),
    )
    await audit("BOT_STARTED", timezone=settings.timezone, admin_count=len(authorized_admin_ids | settings.owner_ids))
    await scheduler_tick(bot)


async def on_shutdown(bot: Bot):
    global scheduler, bot_instance
    if scheduler:
        scheduler.shutdown(wait=False)
        scheduler = None
    await db.close()
    bot_instance = None


async def cancel_flow(message: Message, state: FSMContext):
    current = await state.get_state()
    if current == AdminFlow.channel_id.state:
        text, markup = await channels_view()
        await edit_saved_panel(message, state, text, markup)
    elif current == AdminFlow.admin_id.state:
        text, markup = await admins_view()
        await edit_saved_panel(message, state, text, markup)
    else:
        await message.answer("Cancelled. Use the menu buttons to continue.")
    await state.clear()


async def main():
    bot = Bot(
        settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    dp.message.register(start_handler, CommandStart())
    dp.message.register(addpremium_command, Command("addpremium"))
    dp.message.register(cancel_flow, Command("cancel"))
    dp.message.register(
        lambda m: m.answer("Use /start to open the membership menu."),
        Command("help"),
    )
    dp.callback_query.register(admin_home, F.data == "admin:home")
    dp.callback_query.register(owner_admins_handler, F.data == "owner:admins")
    dp.callback_query.register(owner_begin_admin_change, F.data == "owner:add_admin")
    dp.callback_query.register(status_handler, F.data == "user:status")
    dp.callback_query.register(user_plans_handler, F.data == "user:plans")
    dp.callback_query.register(user_buy_handler, F.data == "user:buy")
    dp.callback_query.register(user_public_channel_handler, F.data == "user:public_channel")
    dp.callback_query.register(user_membership_info_handler, F.data == "user:membership_info")
    dp.callback_query.register(refresh_links_handler, F.data == "user:links")
    dp.callback_query.register(
        lambda c: c.message.edit_text(
            "Main menu", reply_markup=main_keyboard(is_admin(c.from_user.id))
        ),
        F.data == "menu:home",
    )
    dp.callback_query.register(channels_handler, F.data == "admin:channels")
    dp.callback_query.register(begin_channel_add, F.data == "channel:add")
    dp.callback_query.register(show_channel_callback, F.data.startswith("channel:show:"))
    dp.callback_query.register(remove_channel_callback, F.data.startswith("channel:remove:"))
    dp.callback_query.register(remove_admin_callback, F.data.startswith("owner:remove:"))
    dp.callback_query.register(owner_admin_info_callback, F.data.startswith("owner:admininfo:"))
    dp.message.register(receive_channel_id, AdminFlow.channel_id)
    dp.message.register(owner_receive_admin_id, AdminFlow.admin_id)
    dp.chat_join_request.register(join_request_handler)

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
