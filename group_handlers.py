"""Per-group command cards and the Finance chase list.

/setup posts the commands that group actually uses and pins them, unpinning
whatever was there first. Each group sees only its own commands: a stock
controller does not need /outstanding, and a card listing everything is a card
nobody reads.

/pending gives the Finance manager the chase list -- who each PO is waiting on
and for how long -- because in practice the FM is the one chasing GM and Board
approvals and currently has no way to see what is outstanding.
"""
import asyncio
import logging
from datetime import datetime

from clock import local_now

from telegram import BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeChat
from telegram.constants import ParseMode
from telegram.ext import CommandHandler

import flow
from config import Config
from sheets import sheets

log = logging.getLogger("po_bot.groups")

# Which chat key gets which card. Ordering and receiving share the stock group
# today; splitting them later is a config change, not a code change.
GROUP_CARDS = {
    "stock": {
        "title": "Stock controller",
        "role": ("Check the request against what is on the shelf, then record "
                 "what arrives once the order has gone out."),
        "steps": [
            "Tap <b>Enter stock on hand</b> and fill in the file the bot sends. "
            "Enter <code>#N/A</code> where you cannot count. This is required "
            "before the PO can move on.",
            "Tap <b>Checked</b> to pass it to Bookkeeping, or <b>Reject</b> and "
            "reply with the reason.",
            "You are told when a PO is approved. When the goods arrive, send "
            "<code>/receive &lt;PO number&gt;</code>. "
            "The bot sends a file with only the lines still outstanding.",
            "Enter what you <b>counted</b>, not what the invoice says. One row "
            "per lot. Invoice number and date are required.",
        ],
        "cmds": [("/receive &lt;po&gt;", "start a goods receipt"),
                 ("/mypos", "POs you raised"),
                 ("/chatid", "this group's ID")],
    },
    "book": {
        "title": "Bookkeeping",
        "role": ("Confirm the price with the supplier and create the QBO "
                 "purchase order."),
        "steps": [
            "Tap <b>Confirm / update prices</b> and enter the price the "
            "supplier confirmed. Leave a line blank to keep the master price.",
            "Any change is flagged to Finance, GM and the Board, so they "
            "approve the real figure.",
            "Create the QBO purchase order, then tap <b>Booked</b>.",
        ],
        "cmds": [("/mypos", "POs you raised"), ("/chatid", "this group's ID")],
    },
    "fin": {
        "title": "Finance manager",
        "role": "Approve the spend and choose how it is paid.",
        "steps": [
            "Tap <b>Approve · Cash Advance</b> or <b>Approve · A/P</b>, or "
            "<b>Reject</b> and reply with the reason.",
            "Use <code>/pending</code> to see every PO waiting for approval "
            "and who it is waiting on.",
            "Once a month, send <code>/outstanding</code> to review lines that "
            "were never delivered and cancel what is no longer coming.",
        ],
        "cmds": [("/pending", "POs awaiting approval, and for how long"),
                 ("/outstanding", "monthly cancellation review"),
                 ("/mypos", "POs you raised"),
                 ("/chatid", "this group's ID")],
    },
    "gm": {
        "title": "General manager",
        "role": "Approve non-urgent POs after Finance.",
        "steps": [
            "Tap <b>Approve</b> or <b>Reject</b> (reply with the reason).",
            "Urgent POs are approved after Finance and arrive here for "
            "information only.",
            "The order table shows stock on hand next to the quantity "
            "requested, so you can judge whether the amount is reasonable.",
        ],
        "cmds": [("/mypos", "POs you raised"), ("/chatid", "this group's ID")],
    },
    "board": {
        "title": "Board of director",
        "role": "Final approval for non-urgent POs.",
        "steps": [
            "Tap <b>Approve</b> or <b>Reject</b> (reply with the reason).",
            "Urgent POs arrive here for information only.",
            "Page 1 shows stock on hand, and any price the supplier confirmed "
            "that differs from the master list. Page 2 is the sign-off trail.",
        ],
        "cmds": [("/mypos", "POs you raised"), ("/chatid", "this group's ID")],
    },
    "approved": {
        "title": "Approved POs \u00b7 ordering",
        "role": "Send approved orders to the supplier.",
        "steps": [
            "Each approved PO arrives as <b>two</b> PDFs.",
            "<code>PO_&lt;no&gt;.pdf</code> \u2014 the order. Supplier codes "
            "and the supplier's own item names, no approval trail. "
            "<b>This is the one you forward.</b>",
            "<code>PO_&lt;no&gt;_approval.pdf</code> \u2014 the same order plus "
            "every sign-off. <b>Internal. Do not forward.</b>",
            "Short or over deliveries are reported here afterwards, so you can "
            "chase the supplier.",
        ],
        "cmds": [("/mypos", "POs you raised"), ("/chatid", "this group's ID")],
    },
    "cash": {
        "title": "Cash Advance",
        "role": "Information only \u2014 POs Finance routed to cash advance.",
        "steps": ["Prices are shown here so the advance can be prepared."],
        "cmds": [("/chatid", "this group's ID")],
    },
}


# What the "/" menu offers. Telegram shows nothing unless setMyCommands is
# called, and scopes let each group get its own short list instead of one
# combined menu where most entries do not apply.
PRIVATE_COMMANDS = [
    ("new", "Start a purchase order"),
    ("mypos", "Your POs and their status"),
    ("myid", "Show your Telegram user ID"),
]
# Commands offered in every group, on top of that group's own.
COMMON_GROUP_COMMANDS = [
    ("mypos", "Your POs and their status"),
    ("setup", "Re-pin this group's instructions"),
    ("chatid", "Show this chat's ID"),
]
GROUP_COMMANDS = {
    "stock": [("receive", "Record a delivery: /receive <PO number>")],
    "fin": [("pending", "POs awaiting approval, and for how long"),
            ("outstanding", "Monthly review: cancel undelivered lines")],
}


async def publish_commands(app):
    """Called once at startup. A group whose chat ID is not configured is
    skipped -- setMyCommands on an unknown chat just errors."""
    bot = app.bot
    try:
        await bot.set_my_commands(
            [BotCommand(c, d) for c, d in PRIVATE_COMMANDS],
            scope=BotCommandScopeAllPrivateChats())
    except Exception as e:
        log.warning("could not set private commands: %s", e)

    for key, chat_id in Config.CHAT_IDS.items():
        if not chat_id:
            continue
        cmds = GROUP_COMMANDS.get(key, []) + COMMON_GROUP_COMMANDS
        try:
            await bot.set_my_commands(
                [BotCommand(c, d) for c, d in cmds],
                scope=BotCommandScopeChat(chat_id=chat_id))
        except Exception as e:
            log.warning("could not set commands for %s (%s): %s",
                        key, chat_id, e)


def _card(key):
    c = GROUP_CARDS[key]
    out = [f"<b>{c['title']}</b>", c["role"], ""]
    for i, step in enumerate(c["steps"], 1):
        out.append(f"{i}. {step}")
    out.append("")
    out.append("<b>Commands</b>")
    for cmd, what in c["cmds"]:
        out.append(f"  <code>{cmd}</code> \u2014 {what}")
    return "\n".join(out)


async def cmd_setup(update, context):
    """Post this group's card and pin it, clearing anything pinned before."""
    chat_id = update.effective_chat.id
    key = next((k for k, v in Config.CHAT_IDS.items() if v == chat_id), None)
    if key is None:
        await update.message.reply_text(
            "This chat is not configured as a PO group yet. Send /chatid and "
            "put the ID in the matching environment variable first.")
        return

    try:
        await context.bot.unpin_all_chat_messages(chat_id=chat_id)
    except Exception as e:
        log.warning("unpin failed in %s: %s", chat_id, e)

    cmds = GROUP_COMMANDS.get(key, []) + COMMON_GROUP_COMMANDS
    try:
        await context.bot.set_my_commands(
            [BotCommand(c, d) for c, d in cmds],
            scope=BotCommandScopeChat(chat_id=chat_id))
    except Exception as e:
        log.warning("could not refresh commands in %s: %s", chat_id, e)

    msg = await context.bot.send_message(
        chat_id=chat_id, text=_card(key), parse_mode=ParseMode.HTML,
        disable_web_page_preview=True)
    try:
        await context.bot.pin_chat_message(
            chat_id=chat_id, message_id=msg.message_id,
            disable_notification=True)
    except Exception as e:
        await update.message.reply_text(
            "Posted, but I could not pin it \u2014 make me an admin with "
            "'Pin messages' permission, then run /setup again.")
        log.warning("pin failed in %s: %s", chat_id, e)


def _age_days(created):
    for fmt in ("%d-%b-%Y %H:%M", "%d-%b-%Y"):
        try:
            return (local_now() - datetime.strptime(str(created).strip(),
                                                    fmt)).days
        except ValueError:
            continue
    return None


async def cmd_pending(update, context):
    """The FM chases approvals but had no way to see what is outstanding."""
    chat_id = update.effective_chat.id
    if chat_id != Config.CHAT_IDS.get("fin"):
        return
    pos = await asyncio.to_thread(sheets.active_pos)
    waiting = [p for p in pos
               if str(p.get("stage", "")).strip() in flow.APPROVAL_STAGES]
    if not waiting:
        await update.message.reply_text("Nothing is waiting for approval.")
        return

    by_stage = {}
    for p in waiting:
        by_stage.setdefault(str(p["stage"]).strip(), []).append(p)

    out = [f"<b>{len(waiting)} PO(s) awaiting approval</b>"]
    for stage in flow.APPROVAL_STAGES:
        group = by_stage.get(stage)
        if not group:
            continue
        out.append(f"\n<b>{flow.STAGE_LABEL[stage]}</b> \u2014 {len(group)}")
        for p in sorted(group, key=lambda x: str(x.get("created_at", ""))):
            age = _age_days(p.get("created_at"))
            bits = [f"  #{p['po_no']} \u00b7 {p.get('supplier', '')}"]
            if flow.is_urgent(p):
                bits.append("\u26a1")
            if age is not None:
                bits.append(f"\u00b7 {age}d")
            out.append(" ".join(bits))
    oldest = max((_age_days(p.get("created_at")) or 0) for p in waiting)
    if oldest >= 7:
        out.append(f"\n\u26a0\ufe0f Oldest has been waiting {oldest} days.")
    await update.message.reply_text("\n".join(out), parse_mode=ParseMode.HTML)


def register(app):
    app.add_handler(CommandHandler("setup", cmd_setup))
    app.add_handler(CommandHandler("pending", cmd_pending))
