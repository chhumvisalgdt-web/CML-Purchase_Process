"""CML Purchase Order approval bot (Telegram + Google Sheets)."""
import asyncio
import html
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import Conflict, NetworkError, TimedOut
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, filters,
)

from config import Config
from sheets import sheets
from pdf import generate_po_pdf, ensure_fonts
import flow
import upload_handlers

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("fontTools").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("po_bot")

WELCOME = (
    "\U0001f44b CML Purchase Order bot.\n\n"
    "\u2022 /new \u2014 create a purchase order\n"
    "\u2022 /mypos \u2014 your POs and their status\n"
    "\u2022 /chatid \u2014 show this chat's ID (run it inside a group to get the group ID)\n"
    "\u2022 /myid \u2014 show your Telegram user ID"
)


_po_lock = asyncio.Lock()


def now_str():
    return datetime.now(ZoneInfo(Config.TIMEZONE)).strftime("%d-%b-%Y %H:%M")


def fullname(user):
    name = user.full_name if user else ""
    if user and user.username:
        name = f"{name} (@{user.username})".strip()
    return name.strip()


def _draft(context):
    return context.user_data.setdefault(
        "draft", {"items": [], "supplier": None, "urgent": None, "reason": None, "editing_po": None}
    )


def _html_caption(text):
    """Captions cap at ~1024 chars; if over, strip tags and send as plain text."""
    if len(text) <= 1024:
        return text, ParseMode.HTML
    plain = re.sub(r"<[^>]+>", "", text)
    return plain[:1024], None


# ===================== talking to a stage group =====================
async def _send_pdf(context, chat_id, pdf, filename, caption, parse, reply_markup=None):
    """Send a PDF with generous timeouts, retrying transient network/timeout errors.

    Uploading a document over a slow link can exceed the default timeout; without
    a retry the PO would advance but its card would never reach the group.
    """
    data = pdf.getvalue() if hasattr(pdf, "getvalue") else pdf
    last = None
    for attempt in range(4):
        try:
            return await context.bot.send_document(
                chat_id=chat_id, document=data, filename=filename,
                caption=caption, parse_mode=parse, reply_markup=reply_markup,
                read_timeout=60, write_timeout=60, connect_timeout=20, pool_timeout=30)
        except (TimedOut, NetworkError) as e:
            last = e
            log.warning("send_document to %s failed (attempt %d/4): %s", chat_id, attempt + 1, e)
            await asyncio.sleep(2 * (attempt + 1))
    raise last


async def post_stage(context, po_no):
    po = await asyncio.to_thread(sheets.get_po, po_no)
    if not po:
        return
    stage = po["stage"]
    items = await asyncio.to_thread(sheets.get_line_items, po_no)
    chat_id = Config.CHAT_IDS.get(stage)
    if not chat_id:
        log.error("No chat id configured for stage %s (PO %s)", stage, po_no)
        try:
            await context.bot.send_message(
                chat_id=int(po["requester_id"]),
                text=f"\u26a0\ufe0f PO #{po_no} could not be routed: the {flow.STAGE_LABEL[stage]} "
                     f"group is not configured. Please tell the admin.")
        except Exception:
            pass
        return
    text = flow.po_summary(po, items, header=f"\u27a1\ufe0f Awaiting: {flow.STAGE_LABEL[stage]}",
                           show_prices=(stage != flow.STAGE_STOCK))
    caption, parse = _html_caption(text)
    kb = flow.action_keyboard(stage, po_no)
    pdf = await asyncio.to_thread(generate_po_pdf, po, items,
                                  show_prices=(stage != flow.STAGE_STOCK))
    await _send_pdf(context, chat_id, pdf, f"PO_{po_no}_{stage}.pdf", caption, parse, kb)


async def notify_group(context, po_no, chat_key, header, show_prices=True):
    """Post a PO card (no buttons) to a group for information only."""
    chat_id = Config.CHAT_IDS.get(chat_key)
    if not chat_id:
        return
    po = await asyncio.to_thread(sheets.get_po, po_no)
    items = await asyncio.to_thread(sheets.get_line_items, po_no)
    text = flow.po_summary(po, items, header=header, show_prices=show_prices)
    caption, parse = _html_caption(text)
    pdf = await asyncio.to_thread(generate_po_pdf, po, items, show_prices=show_prices)
    await _send_pdf(context, chat_id, pdf, f"PO_{po_no}_{chat_key}.pdf", caption, parse)


async def finalize(context, po_no):
    await asyncio.to_thread(sheets.update_po, po_no, stage=flow.STAGE_APPROVED,
                            status="approved", updated_at=now_str())
    po = await asyncio.to_thread(sheets.get_po, po_no)
    if flow.is_urgent(po):
        fyi = "\U0001f514 FYI \u2014 urgent PO approved (no action needed)"
        await notify_group(context, po_no, flow.STAGE_GM, fyi)
        await notify_group(context, po_no, flow.STAGE_BOARD, fyi)
    # Approved POs group is a broad audience — keep it price-free.
    await notify_group(context, po_no, "approved", "\u2705 Approved (for your records)",
                       show_prices=False)
    if str(po.get("payment_type", "")).strip() == flow.PAYMENT_LABEL["ca"]:
        await notify_group(context, po_no, "cash", "\U0001f4b5 Cash Advance \u2014 approved")
    try:
        await context.bot.send_message(chat_id=int(po["requester_id"]),
                                        text=f"\U0001f389 PO #{po_no} is fully approved.")
    except Exception as e:
        log.warning("Could not DM requester on approval: %s", e)


# ===================== commands =====================
async def cmd_start(update, context):
    context.user_data.clear()
    await update.message.reply_text(WELCOME)


async def cmd_chatid(update, context):
    c = update.effective_chat
    await update.message.reply_text(f"Chat ID: {c.id}\nType: {c.type}")


async def cmd_myid(update, context):
    u = update.effective_user
    await update.message.reply_text(f"Your user ID: {u.id}\nName: {fullname(u)}")


async def cmd_new(update, context):
    if update.effective_chat.type != "private":
        await update.message.reply_text("Please create POs in a private chat with me \u2014 send /new in DM.")
        return
    context.user_data.clear()
    await update.message.reply_text(
        "New PO \u2014 send me the items as a filled template.",
        reply_markup=upload_handlers.entry_keyboard())


async def cmd_mypos(update, context):
    uid = update.effective_user.id
    pos = await asyncio.to_thread(sheets.get_pos_by_requester, uid)
    if not pos:
        await update.message.reply_text("You have no POs yet. Send /new to create one.")
        return
    lines = []
    for p in pos[-15:]:
        stg = flow.STAGE_LABEL.get(p.get("stage"), p.get("stage"))
        lines.append(f"PO #{p['po_no']} \u2014 {stg} ({p.get('status', '')})")
    await update.message.reply_text("\n".join(lines))


async def cmd_mastercheck(update, context):
    """List codes duplicated across the two master tabs. Such a code cannot
    identify one item, so it is unorderable until the sheet is corrected."""
    index = await upload_handlers.master_index()
    if not index.dup_codes:
        await update.message.reply_text(
            f"No duplicate codes. {len(index.by_code)} item(s) orderable.")
        return
    lines = [f"{len(index.dup_codes)} duplicated code(s) \u2014 these cannot be "
             f"ordered until the master list is fixed:", ""]
    lines += [f"  {c}" for c in sorted(index.dup_codes)[:40]]
    if len(index.dup_codes) > 40:
        lines.append(f"  \u2026 and {len(index.dup_codes) - 40} more")
    await update.message.reply_text("\n".join(lines))


# ===================== creation / edit (DM) =====================
def _items_overview(draft):
    if not draft["items"]:
        return "No items yet."
    lines = []
    for i, it in enumerate(draft["items"], 1):
        s = f"{i}. {it['item']} \u00d7{it['qty']}"
        if it.get("pack"):
            s += f"  ({it['pack']})"
        lines.append(s)
    lines.append(f"{len(draft['items'])} item(s).")
    return "\n".join(lines)


def _items_keyboard(draft):
    editing = bool(draft.get("editing_po"))
    rows = []
    if editing:
        for i, it in enumerate(draft["items"]):
            rows.append([InlineKeyboardButton(f"\u270f\ufe0f {it['item']} \u00d7{it['qty']}", callback_data=f"ei:{i}")])
    rows.append([InlineKeyboardButton(
        "\U0001f4c4 Upload a replacement file", callback_data="up:ask")])
    rows.append([InlineKeyboardButton(
        "\U0001f501 Resubmit PO" if editing else "\u2705 Submit PO",
        callback_data="resub" if editing else "submit")])
    rows.append([InlineKeyboardButton("\u2716\ufe0f Cancel", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)


async def _show_items(target, context, prefix=""):
    draft = _draft(context)
    text = (prefix + "\n\n" if prefix else "") + _items_overview(draft)
    await target.reply_text(text, reply_markup=_items_keyboard(draft))


async def on_text(update, context):
    if update.effective_chat.type != "private":
        await _maybe_capture_reason(update, context)
        return
    state = context.user_data.get("state")
    if state == "supplier_why":
        await upload_handlers.handle_supplier_why(update, context)
    elif state == "reason":
        await _handle_po_reason(update, context)
    elif state == "editqty":
        await _handle_edit_qty(update, context)
    else:
        await update.message.reply_text(
            "Send /new to start a PO, or /mypos to see yours.")


async def _handle_po_reason(update, context):
    reason = update.message.text.strip()
    if not reason:
        await update.message.reply_text("Please type a short reason / purpose for this PO.")
        return
    _draft(context)["reason"] = reason
    context.user_data["state"] = None
    await _ask_urgent(update.message)


async def _handle_edit_qty(update, context):
    draft = _draft(context)
    idx = context.user_data.get("edit_idx")
    if idx is None or idx >= len(draft["items"]):
        context.user_data["state"] = None
        await _show_items(update.message, context)
        return
    try:
        qty = int(update.message.text.strip())
        if qty < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Send a whole number (0 to remove).")
        return
    if qty == 0:
        draft["items"].pop(idx)
    else:
        it = draft["items"][idx]
        it["qty"] = qty
        it["line_total"] = round(it["unit_price"] * qty, 2)
    context.user_data["state"] = None
    context.user_data.pop("edit_idx", None)
    await _show_items(update.message, context, prefix="Updated.")


# ===================== callbacks =====================
async def on_callback(update, context):
    q = update.callback_query
    data = q.data or ""
    # _cb_action answers the query itself (with show_alert for "not authorized"
    # / "already processed"). Telegram allows ONE answer per query, so answering
    # here first made those alerts raise BadRequest -- which aborted the handler
    # before it could strip the stale buttons.
    if not data.startswith("a:"):
        await q.answer()
    if data == "submit":
        await _cb_submit(q, context)
    elif data.startswith("u:"):
        await _cb_urgent(q, context, data.split(":")[1] == "1")
    elif data == "go":
        await _cb_confirm(q, context)
    elif data == "cancel":
        context.user_data.clear()
        await q.message.reply_text("Cancelled.")
    elif data.startswith("ed:"):
        await _cb_edit_open(q, context, data.split(":")[1])
    elif data.startswith("ei:"):
        await _cb_edit_item(q, context, int(data.split(":")[1]))
    elif data == "resub":
        await _cb_resubmit(q, context)
    elif data.startswith("a:"):
        await _cb_action(q, context, data)


async def _ask_reason(target, context):
    """Shared by the submit button and the upload path."""
    msg = getattr(target, "message", None) or target
    context.user_data["state"] = "reason"
    await msg.reply_text(
        "What is the reason / purpose for this PO? Reply with a short note.")


async def _cb_submit(q, context):
    if not _draft(context)["items"]:
        await q.message.reply_text("Add at least one item first.")
        return
    await _ask_reason(q, context)


async def _ask_urgent(target):
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("\u26a1 Urgent", callback_data="u:1"),
        InlineKeyboardButton("Not urgent", callback_data="u:0"),
    ]])
    await target.reply_text("Is this PO urgent?", reply_markup=kb)


async def _cb_urgent(q, context, urgent):
    draft = _draft(context)
    draft["urgent"] = urgent
    lines = ["Please confirm:", "",
             f"Category: {flow.CATEGORY_LABEL.get(draft.get('category'), '')}",
             f"Supplier: {draft['supplier']}",
             f"Urgent: {'Yes' if urgent else 'No'}"]
    if draft.get("reason"):
        lines.append(f"Reason: {draft['reason']}")
    lines.append("")
    for i, it in enumerate(draft["items"], 1):
        s = f"{i}. {it['item']} \u00d7{it['qty']}"
        if it.get("pack"):
            s += f"  ({it['pack']})"
        lines.append(s)
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("\u2705 Confirm & send", callback_data="go"),
        InlineKeyboardButton("\u2716\ufe0f Cancel", callback_data="cancel"),
    ]])
    await q.message.reply_text("\n".join(lines), reply_markup=kb)


async def _cb_confirm(q, context):
    draft = _draft(context)
    if draft.get("editing_po"):
        await _cb_resubmit(q, context)
        return
    if not draft["items"]:
        await q.message.reply_text("Nothing to send.")
        return
    user = q.from_user
    total = round(sum(it["line_total"] for it in draft["items"]), 2)
    async with _po_lock:
        po_no = await asyncio.to_thread(sheets.next_po_no)
    po = {
        "po_no": po_no, "created_at": now_str(),
        "requester_id": user.id, "requester_name": fullname(user),
        "supplier": draft["supplier"], "total": total,
        "urgent": "yes" if draft["urgent"] else "no",
        "reason": draft.get("reason", ""),
        "category": flow.CATEGORY_LABEL.get(draft.get("category"), ""),
        "stage": flow.STAGE_STOCK, "status": "active", "updated_at": now_str(),
        "upload_id": draft.get("upload_id", ""),
        "supplier_reason": draft.get("supplier_reason", ""),
    }
    await asyncio.to_thread(sheets.create_po, po)
    await asyncio.to_thread(sheets.add_line_items, po_no, draft["items"])
    if draft.get("upload_id"):
        await asyncio.to_thread(sheets.log_upload_result,
                                draft["upload_id"], "submitted", None, po_no)
    context.user_data.clear()
    await q.message.reply_text(f"\u2705 PO #{po_no} submitted. It's now with the Stock controller.")
    await post_stage(context, po_no)


async def _cb_edit_open(q, context, po_no):
    po = await asyncio.to_thread(sheets.get_po, po_no)
    if not po:
        await q.message.reply_text("PO not found.")
        return
    # Without these two guards an old button could reset a PO that is already
    # mid-approval -- or already approved -- back to the Stock controller.
    if str(po.get("requester_id")).strip() != str(q.from_user.id):
        await q.message.reply_text("Only the requester can edit this PO.")
        return
    if str(po.get("status")).strip() != "returned":
        await q.message.reply_text(f"PO #{po_no} is no longer waiting for changes.")
        return
    items = await asyncio.to_thread(sheets.get_line_items, po_no)
    context.user_data["draft"] = {
        "items": [dict(it) for it in items],
        "supplier": po["supplier"], "urgent": flow.is_urgent(po),
        "reason": po.get("reason", ""),
        "category": flow.CATEGORY_CODE.get(str(po.get("category", "")).strip(), "lab"),
        "editing_po": po_no,
    }
    context.user_data["state"] = None
    context.user_data["upload_editing_po"] = po_no
    await _show_items(
        q.message, context,
        prefix=f"Editing PO #{po_no}. Tap an item to change its quantity, or "
               f"upload a replacement file, then resubmit.")


async def _cb_edit_item(q, context, idx):
    draft = _draft(context)
    if idx < 0 or idx >= len(draft["items"]):
        await q.message.reply_text("That item expired.")
        return
    context.user_data["edit_idx"] = idx
    context.user_data["state"] = "editqty"
    it = draft["items"][idx]
    await q.message.reply_text(f"New quantity for {it['item']} (send 0 to remove).")


async def _cb_resubmit(q, context):
    draft = _draft(context)
    po_no = draft.get("editing_po")
    if not po_no:
        await q.message.reply_text("Nothing to resubmit.")
        return
    if not draft["items"]:
        await q.message.reply_text("Add at least one item before resubmitting.")
        return
    total = round(sum(it["line_total"] for it in draft["items"]), 2)
    await asyncio.to_thread(sheets.replace_line_items, po_no, draft["items"])
    # Clear every earlier sign-off. Without this the revised PO arrives at the
    # Stock controller with "Checked by" / "Booked by" already filled in from
    # the previous pass -- approvals that were never given on this document.
    await asyncio.to_thread(
        sheets.update_po, po_no, supplier=draft["supplier"], total=total,
        urgent="yes" if draft["urgent"] else "no", reason=draft.get("reason", ""),
        stage=flow.STAGE_STOCK, status="active",
        reject_stage="", reject_reason="", updated_at=now_str(),
        stock_by="", stock_at="", book_by="", book_at="",
        fin_by="", fin_at="", gm_by="", gm_at="", board_by="", board_at="",
        payment_type="", supplier_reason=draft.get("supplier_reason", ""),
        upload_id=draft.get("upload_id", ""))
    context.user_data.clear()
    await q.message.reply_text(f"\U0001f501 PO #{po_no} resubmitted. Back to the Stock controller.")
    await post_stage(context, po_no)


async def _ack_edit(q, suffix):
    msg = q.message
    try:
        if msg.caption is not None:
            base = msg.caption_html if msg.caption_html is not None else (msg.caption or "")
            await q.edit_message_caption(caption=base + suffix, parse_mode=ParseMode.HTML)
        else:
            base = msg.text_html if msg.text_html is not None else (msg.text or "")
            await q.edit_message_text(text=base + suffix, parse_mode=ParseMode.HTML)
    except Exception:
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass


async def _cb_action(q, context, data):
    parts = data.split(":")
    if len(parts) != 4:
        return
    _, stage, act, po_no = parts
    user = q.from_user
    allowed = Config.APPROVERS.get(stage) or []
    if allowed and user.id not in allowed:
        await q.answer("You're not authorized to act on this stage.", show_alert=True)
        return
    po = await asyncio.to_thread(sheets.get_po, po_no)
    if not po:
        await q.answer("PO not found.", show_alert=True)
        return
    if po.get("stage") != stage or str(po.get("status")) != "active":
        await q.answer("Already processed.", show_alert=True)
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    if act in ("ok", "booked", "ca", "ap"):
        await q.answer()
        by_col, at_col = flow.STAGE_AUDIT[stage]
        fields = {by_col: fullname(user), at_col: now_str(), "updated_at": now_str()}
        pay = flow.PAYMENT_LABEL.get(act) if stage == flow.STAGE_FIN else None
        if pay:
            fields["payment_type"] = pay
        await asyncio.to_thread(sheets.update_po, po_no, **fields)
        bits = [f"\u2705 {flow.action_verb(stage)} by {html.escape(fullname(user))}"]
        pos = flow.position(stage)
        if pos:
            bits.append(pos)
        if pay:
            bits.append(pay)
        bits.append(now_str())
        await _ack_edit(q, "\n\n" + "  |  ".join(bits))
        nxt = flow.next_stage(stage, flow.is_urgent(po))
        if nxt == flow.STAGE_APPROVED:
            await finalize(context, po_no)
        else:
            await asyncio.to_thread(sheets.update_po, po_no, stage=nxt, updated_at=now_str())
            await post_stage(context, po_no)
    elif act == "no":
        await q.answer()
        context.chat_data["await_reason"] = {
            "po_no": po_no, "user_id": user.id, "stage": stage, "msg_id": q.message.message_id,
        }
        await q.message.reply_text(
            f"{fullname(user)}, *reply to this message* with the reason for rejecting PO #{po_no}.",
            parse_mode=ParseMode.MARKDOWN)


async def _maybe_capture_reason(update, context):
    pend = context.chat_data.get("await_reason")
    if not pend or update.effective_user.id != pend["user_id"]:
        return
    reason = update.message.text.strip()
    po_no, stage = pend["po_no"], pend["stage"]
    context.chat_data.pop("await_reason", None)
    po = await asyncio.to_thread(sheets.get_po, po_no)
    if not po or po.get("stage") != stage:
        await update.message.reply_text("This PO was already processed.")
        return
    await asyncio.to_thread(sheets.update_po, po_no, stage=flow.STAGE_RETURNED, status="returned",
                            reject_stage=stage, reject_reason=reason, updated_at=now_str())
    await update.message.reply_text(
        f"\u274c PO #{po_no} rejected at {flow.STAGE_LABEL[stage]}. The requester has been notified.")
    try:
        await context.bot.edit_message_reply_markup(
            chat_id=update.effective_chat.id, message_id=pend["msg_id"], reply_markup=None)
    except Exception:
        pass
    try:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("\u270f\ufe0f Edit & resubmit", callback_data=f"ed:{po_no}")]])
        await context.bot.send_message(
            chat_id=int(po["requester_id"]),
            text=(f"\u274c PO #{po_no} was rejected at *{flow.STAGE_LABEL[stage]}* by "
                  f"{fullname(update.effective_user)}.\n\nReason: {reason}\n\n"
                  f"Fix it and resubmit \u2014 it will re-run from the Stock controller."),
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    except Exception as e:
        log.warning("Could not DM requester on reject: %s", e)


# ===================== bootstrap =====================
async def _on_error(update, context):
    err = context.error
    if isinstance(err, Conflict):
        log.error("Conflict: another instance is polling this SAME bot token. Run only ONE "
                  "instance/replica, and don't share this token with your other bot.")
    elif isinstance(err, (TimedOut, NetworkError)):
        log.warning("Network issue talking to Telegram: %s", err)
    else:
        log.error("Unhandled error: %s", err, exc_info=err)


def build_app():
    request = HTTPXRequest(connection_pool_size=16, read_timeout=60,
                           write_timeout=60, connect_timeout=20, pool_timeout=30)
    app = Application.builder().token(Config.BOT_TOKEN).request(request).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("mypos", cmd_mypos))
    app.add_handler(CommandHandler("chatid", cmd_chatid))
    app.add_handler(CommandHandler("myid", cmd_myid))
    app.add_handler(CommandHandler("mastercheck", cmd_mastercheck))
    # Registration order matters: within a group the first handler added wins.
    # The upload router must come BEFORE the generic one, or every "up:" tap is
    # swallowed with no error and nothing in the logs.
    upload_handlers.register(app, {"ask_reason": _ask_reason})
    app.add_handler(CallbackQueryHandler(on_callback, pattern=r"^(?!up:)"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(_on_error)
    return app


def main():
    missing = Config.missing_core()
    if missing:
        raise SystemExit("Missing required env vars: " + ", ".join(missing))
    email = sheets.ensure_tabs()
    log.info("Sheets ready. Make sure the spreadsheet is shared (Editor) with: %s", email)
    ensure_fonts()
    app = build_app()
    log.info("Bot starting (polling) in timezone %s", Config.TIMEZONE)
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
