"""CML Purchase Order approval bot (Telegram + Google Sheets)."""
import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, filters,
)

from config import Config
from sheets import sheets
from pdf import generate_po_pdf
import flow

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("po_bot")

WELCOME = (
    "\U0001f44b CML Purchase Order bot.\n\n"
    "\u2022 /new \u2014 create a purchase order\n"
    "\u2022 /mypos \u2014 your POs and their status\n"
    "\u2022 /chatid \u2014 show this chat's ID (run it inside a group to get the group ID)\n"
    "\u2022 /myid \u2014 show your Telegram user ID"
)


def now_str():
    return datetime.now(ZoneInfo(Config.TIMEZONE)).strftime("%Y-%m-%d %H:%M")


def fullname(user):
    name = user.full_name if user else ""
    if user and user.username:
        name = f"{name} (@{user.username})".strip()
    return name.strip()


def _draft(context):
    return context.user_data.setdefault(
        "draft", {"items": [], "supplier": None, "urgent": None, "editing_po": None}
    )


# ===================== talking to a stage group =====================
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
    text = flow.po_summary(po, items, header=f"\u27a1\ufe0f Awaiting: {flow.STAGE_LABEL[stage]}")
    kb = flow.action_keyboard(stage, po_no)
    if stage == flow.STAGE_BOOK:
        pdf = await asyncio.to_thread(generate_po_pdf, po, items)
        await context.bot.send_document(chat_id=chat_id, document=pdf,
                                         filename=f"PO_{po_no}.pdf", caption=text, reply_markup=kb)
    else:
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)


async def notify_fyi(context, po_no, stage):
    chat_id = Config.CHAT_IDS.get(stage)
    if not chat_id:
        return
    po = await asyncio.to_thread(sheets.get_po, po_no)
    items = await asyncio.to_thread(sheets.get_line_items, po_no)
    text = flow.po_summary(po, items, header="\U0001f514 FYI \u2014 urgent PO approved (no action needed)")
    await context.bot.send_message(chat_id=chat_id, text=text)


async def finalize(context, po_no):
    await asyncio.to_thread(sheets.update_po, po_no, stage=flow.STAGE_APPROVED,
                            status="approved", updated_at=now_str())
    po = await asyncio.to_thread(sheets.get_po, po_no)
    if flow.is_urgent(po):
        await notify_fyi(context, po_no, flow.STAGE_GM)
        await notify_fyi(context, po_no, flow.STAGE_BOARD)
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
    context.user_data["draft"] = {"items": [], "supplier": None, "urgent": None, "editing_po": None}
    context.user_data["state"] = "item"
    await update.message.reply_text("New PO. Send the *item name* (or part of it).",
                                    parse_mode=ParseMode.MARKDOWN)


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
    rows.append([InlineKeyboardButton("\u2795 Add item", callback_data="addmore")])
    rows.append([InlineKeyboardButton(
        "\U0001f501 Resubmit PO" if editing else "\u2705 Submit PO",
        callback_data="resub" if editing else "submit")])
    rows.append([InlineKeyboardButton("\u2716\ufe0f Cancel", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)


async def _show_items(target, context, prefix=""):
    draft = _draft(context)
    text = (prefix + "\n\n" if prefix else "") + _items_overview(draft)
    await target.reply_text(text, reply_markup=_items_keyboard(draft))


async def _ask_quantity(target, context, item):
    context.user_data["pending_item"] = item
    context.user_data["state"] = "qty"
    lines = [f"Item: {item['item']}"]
    if item.get("material_code"):
        lines.append(f"Code: {item['material_code']}")
    lines.append(f"Supplier: {item['supplier']}")
    if item.get("supplier_reagent"):
        lines.append(f"Supplier reagent: {item['supplier_reagent']}")
    if item.get("pack"):
        lines.append(f"Pack: {item['pack']}")
    lines.append("\nHow many? Send a number.")
    await target.reply_text("\n".join(lines))


async def on_text(update, context):
    if update.effective_chat.type != "private":
        await _maybe_capture_reason(update, context)
        return
    state = context.user_data.get("state")
    if state == "item":
        await _handle_item_name(update, context)
    elif state == "qty":
        await _handle_quantity(update, context)
    elif state == "editqty":
        await _handle_edit_qty(update, context)
    else:
        await update.message.reply_text("Send /new to start a PO, or /mypos to see yours.")


async def _handle_item_name(update, context):
    draft = _draft(context)
    name = update.message.text.strip()
    item = await asyncio.to_thread(sheets.find_item, name)
    if item:
        if draft["supplier"] and item["supplier"] != draft["supplier"]:
            await update.message.reply_text(
                f"\u26a0\ufe0f This PO is for *{draft['supplier']}*, but {item['item']} is from "
                f"*{item['supplier']}*. One supplier per PO \u2014 send a different item.",
                parse_mode=ParseMode.MARKDOWN)
            return
        await _ask_quantity(update.message, context, item)
        return
    suggestions = await asyncio.to_thread(sheets.suggest_items, name)
    if suggestions:
        context.user_data["suggestions"] = suggestions
        rows = [[InlineKeyboardButton(s["item"], callback_data=f"pk:{i}")] for i, s in enumerate(suggestions)]
        rows.append([InlineKeyboardButton("\u2716\ufe0f Cancel", callback_data="cancel")])
        await update.message.reply_text(f"'{name}' isn't in the master list. Did you mean:",
                                        reply_markup=InlineKeyboardMarkup(rows))
    else:
        await update.message.reply_text(f"No match for '{name}' in the master list. Try another name.")


async def _handle_quantity(update, context):
    item = context.user_data.get("pending_item")
    if not item:
        context.user_data["state"] = "item"
        await update.message.reply_text("Send the item name.")
        return
    try:
        qty = int(update.message.text.strip())
        if qty <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Please send a whole number greater than 0.")
        return
    draft = _draft(context)
    if draft["supplier"] is None:
        draft["supplier"] = item["supplier"]
    draft["items"].append({
        "item": item["item"], "qty": qty, "unit_price": item["unit_price"],
        "line_total": round(item["unit_price"] * qty, 2),
        "material_code": item.get("material_code", ""),
        "supplier_reagent": item.get("supplier_reagent", ""),
        "pack": item.get("pack", ""),
    })
    context.user_data["state"] = None
    context.user_data.pop("pending_item", None)
    await _show_items(update.message, context, prefix="\u2705 Added.")


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
    await q.answer()
    if data.startswith("pk:"):
        await _cb_pick(q, context, int(data.split(":")[1]))
    elif data == "addmore":
        context.user_data["state"] = "item"
        await q.message.reply_text("Send the next item name.")
    elif data == "submit":
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


async def _cb_pick(q, context, idx):
    suggestions = context.user_data.get("suggestions") or []
    if idx < 0 or idx >= len(suggestions):
        await q.message.reply_text("That option expired. Send the item name again.")
        return
    item = suggestions[idx]
    draft = _draft(context)
    if draft["supplier"] and item["supplier"] != draft["supplier"]:
        await q.message.reply_text(
            f"\u26a0\ufe0f One supplier per PO ({draft['supplier']}). {item['item']} is from {item['supplier']}.")
        return
    await _ask_quantity(q.message, context, item)


async def _cb_submit(q, context):
    if not _draft(context)["items"]:
        await q.message.reply_text("Add at least one item first.")
        return
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("\u26a1 Urgent", callback_data="u:1"),
        InlineKeyboardButton("Not urgent", callback_data="u:0"),
    ]])
    await q.message.reply_text("Is this PO urgent?", reply_markup=kb)


async def _cb_urgent(q, context, urgent):
    draft = _draft(context)
    draft["urgent"] = urgent
    lines = ["Please confirm:", "",
             f"Supplier: {draft['supplier']}",
             f"Urgent: {'Yes' if urgent else 'No'}", ""]
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
    po_no = await asyncio.to_thread(sheets.next_po_no)
    po = {
        "po_no": po_no, "created_at": now_str(),
        "requester_id": user.id, "requester_name": fullname(user),
        "supplier": draft["supplier"], "total": total,
        "urgent": "yes" if draft["urgent"] else "no",
        "stage": flow.STAGE_STOCK, "status": "active", "updated_at": now_str(),
    }
    await asyncio.to_thread(sheets.create_po, po)
    await asyncio.to_thread(sheets.add_line_items, po_no, draft["items"])
    context.user_data.clear()
    await q.message.reply_text(f"\u2705 PO #{po_no} submitted. It's now with the Stock controller.")
    await post_stage(context, po_no)


async def _cb_edit_open(q, context, po_no):
    po = await asyncio.to_thread(sheets.get_po, po_no)
    if not po:
        await q.message.reply_text("PO not found.")
        return
    items = await asyncio.to_thread(sheets.get_line_items, po_no)
    context.user_data["draft"] = {
        "items": [dict(it) for it in items],
        "supplier": po["supplier"], "urgent": flow.is_urgent(po), "editing_po": po_no,
    }
    context.user_data["state"] = None
    await _show_items(q.message, context,
                      prefix=f"Editing PO #{po_no}. Tap an item to change its quantity, add items, then resubmit.")


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
    await asyncio.to_thread(
        sheets.update_po, po_no, supplier=draft["supplier"], total=total,
        urgent="yes" if draft["urgent"] else "no", stage=flow.STAGE_STOCK, status="active",
        reject_stage="", reject_reason="", updated_at=now_str())
    context.user_data.clear()
    await q.message.reply_text(f"\U0001f501 PO #{po_no} resubmitted. Back to the Stock controller.")
    await post_stage(context, po_no)


async def _ack_edit(q, suffix):
    base = q.message.caption if q.message.caption is not None else (q.message.text or "")
    try:
        if q.message.caption is not None:
            await q.edit_message_caption(caption=base + suffix)
        else:
            await q.edit_message_text(text=base + suffix)
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

    if act in ("ok", "booked"):
        by_col, at_col = flow.STAGE_AUDIT[stage]
        await asyncio.to_thread(sheets.update_po, po_no,
                                **{by_col: fullname(user), at_col: now_str(), "updated_at": now_str()})
        verb = "Booked" if act == "booked" else "Approved"
        await _ack_edit(q, f"\n\n\u2705 {verb} by {fullname(user)} \u00b7 {now_str()}")
        nxt = flow.next_stage(stage, flow.is_urgent(po))
        if nxt == flow.STAGE_APPROVED:
            await finalize(context, po_no)
        else:
            await asyncio.to_thread(sheets.update_po, po_no, stage=nxt, updated_at=now_str())
            await post_stage(context, po_no)
    elif act == "no":
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
def build_app():
    app = Application.builder().token(Config.BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("mypos", cmd_mypos))
    app.add_handler(CommandHandler("chatid", cmd_chatid))
    app.add_handler(CommandHandler("myid", cmd_myid))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app


def main():
    missing = Config.missing_core()
    if missing:
        raise SystemExit("Missing required env vars: " + ", ".join(missing))
    email = sheets.ensure_tabs()
    log.info("Sheets ready. Make sure the spreadsheet is shared (Editor) with: %s", email)
    app = build_app()
    log.info("Bot starting (polling) in timezone %s", Config.TIMEZONE)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
