"""Telegram wiring for the PO upload path.

Registered by bot.py via register(app, hooks). The two hooks let this module
hand a finished draft back to the existing conversation without importing
bot.py (which imports this module) -- no circular import, and the approval
state machine stays untouched.

    hooks["ask_reason"](update_or_query, context)  -> existing reason prompt
    hooks["resubmit"](query, context)              -> existing _cb_resubmit
"""
import asyncio
import html
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import CallbackQueryHandler, MessageHandler, filters

import upload_excel as xl
from sheets import sheets
from upload_validate import (CAT_OTHER, CAT_REAGENT, CATEGORY_LABEL, MAX_LINES,
                             build_index, validate)

log = logging.getLogger("po_bot.upload")

from config import Config

MAX_UPLOAD_BYTES = Config.MAX_UPLOAD_BYTES
SUPPLIER_PAGE = 8
_HOOKS = {}


def entry_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("\U0001f4c4 Upload a filled template",
                              callback_data="up:ask")],
        [InlineKeyboardButton("\u2b07\ufe0f Blank template \u00b7 Reagent",
                              callback_data=f"up:tpl:{CAT_REAGENT}")],
        [InlineKeyboardButton("\u2b07\ufe0f Blank template \u00b7 Other",
                              callback_data=f"up:tpl:{CAT_OTHER}")],
    ])


async def master_index():
    """MasterIndex over both master tabs. sheets.get_master() /
    get_other_master() cache for 60s underneath, so this is cheap to call."""
    reagent = await asyncio.to_thread(sheets.get_master)
    other = await asyncio.to_thread(sheets.get_other_master)
    return build_index(reagent, other)


async def _index(context=None):
    return await master_index()


async def cb_entry(q, context, data):
    parts = data.split(":")
    action = parts[1]

    if action == "ask":
        context.user_data["state"] = None
        await q.message.reply_text(
            "Send me the filled template as a file. You can forward an older "
            "one too \u2014 I will check it either way.")
        return

    if action == "tpl":
        category = parts[2]
        index = await _index(context)
        suppliers = index.suppliers(category)
        if not suppliers:
            await q.message.reply_text(
                f"There are no items on the {CATEGORY_LABEL[category]} list yet.")
            return
        context.user_data["up_cat"] = category
        context.user_data["up_sup"] = suppliers[:SUPPLIER_PAGE * 4]
        page = 0
        await q.message.reply_text(
            "Which supplier?", reply_markup=_supplier_keyboard(
                context.user_data["up_sup"], page))
        return

    if action == "sup":
        await _send_template(q, context, int(parts[2]))
        return

    if action == "page":
        await q.message.edit_reply_markup(
            reply_markup=_supplier_keyboard(context.user_data.get("up_sup", []),
                                            int(parts[2])))
        return

    if action == "go":
        await _confirm(q, context)
        return

    if action == "rep":
        await _send_report(q, context)
        return


def _supplier_keyboard(suppliers, page):
    start = page * SUPPLIER_PAGE
    chunk = suppliers[start:start + SUPPLIER_PAGE]
    rows = [[InlineKeyboardButton(s[:40], callback_data=f"up:sup:{start + i}")]
            for i, s in enumerate(chunk)]
    nav = []
    if start:
        nav.append(InlineKeyboardButton("\u2039 Back",
                                        callback_data=f"up:page:{page - 1}"))
    if start + SUPPLIER_PAGE < len(suppliers):
        nav.append(InlineKeyboardButton("More \u203a",
                                        callback_data=f"up:page:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("\u2716\ufe0f Cancel", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)


async def _send_template(q, context, idx):
    category = context.user_data.get("up_cat")
    suppliers = context.user_data.get("up_sup") or []
    if not category or idx < 0 or idx >= len(suppliers):
        await q.message.reply_text("That option expired. Send /new to start again.")
        return
    supplier = suppliers[idx]
    index = await _index(context)
    try:
        built = await asyncio.to_thread(xl.build_template, index, category, supplier)
    except ValueError:
        await q.message.reply_text(
            f"There are no orderable items for {supplier} on the "
            f"{CATEGORY_LABEL[category]} list.")
        return

    caption = (f"{CATEGORY_LABEL[category]} template for {html.escape(supplier)} "
               f"\u2014 {built['n_items']} items.\n"
               f"Fill in the shaded cells only: code, quantity, note. "
               f"Maximum {MAX_LINES} lines.")
    if built.get("n_no_price"):
        caption += (f"\n\n{built['n_no_price']} item(s) are left out because they "
                    f"have no price in the master list.")
    if built["n_excluded"]:
        caption += (f"\n\n{built['n_excluded']} item(s) are not available right "
                    f"now: their codes appear more than once in the master list. "
                    f"Tell me if you need one of them.")
    await context.bot.send_document(
        chat_id=q.message.chat_id, document=built["bytes"],
        filename=built["filename"], caption=caption,
        read_timeout=60, write_timeout=60, connect_timeout=20)
    for k in ("up_cat", "up_sup"):
        context.user_data.pop(k, None)


async def on_document(update, context):
    doc = update.message.document
    if not doc.file_name.lower().endswith((".xlsx", ".xlsm")):
        await update.message.reply_text(
            "Please send the Excel template (.xlsx). Send /new if you need a "
            "blank one.")
        return
    if doc.file_size and doc.file_size > MAX_UPLOAD_BYTES:
        await update.message.reply_text(
            "That file is too large. The template should be well under 2 MB.")
        return

    note = await update.message.reply_text("Checking the file\u2026")
    tg_file = await context.bot.get_file(doc.file_id)
    data = bytes(await tg_file.download_as_bytearray())

    try:
        rows, meta = await asyncio.to_thread(xl.read_request, data)
    except xl.ReadError as e:
        await note.edit_text(str(e))
        return

    meta["file_id"] = doc.file_id
    meta["file_name"] = doc.file_name
    upload_id = await asyncio.to_thread(
        sheets.log_upload_start, update.effective_user, meta,
        context.user_data.get("upload_supersedes"))

    index = await _index(context)
    result = validate(rows, index,
                      template_supplier=meta.get("template_supplier") or None,
                      template_category=meta.get("template_category"))

    await asyncio.to_thread(sheets.log_upload_rows, upload_id, result.report)
    await asyncio.to_thread(
        sheets.log_upload_result, upload_id,
        "blocked" if result.blocked else "checked", result.summary)

    context.user_data["upload"] = {
        "id": upload_id, "meta": meta, "result": result,
        "editing_po": context.user_data.get("upload_editing_po"),
    }
    await note.delete()

    if result.blocked:
        await _post_blocked(update.message, context, result, meta)
    else:
        await _post_ready(update.message, context, result, meta)


def _plural(n, word):
    return f"{n} {word}" + ("" if n == 1 else "s")


async def _post_blocked(target, context, result, meta):
    s = result.summary
    lines = [f"\u26a0\ufe0f Upload checked \u2014 "
             f"{_plural(s['rows_blocked'], 'row')} need fixing", ""]
    if s["category_label"]:
        lines.append(f"{s['category_label']} \u00b7 {html.escape(s['supplier'])}")
    lines.append(f"Read: {s['rows_populated']} of {MAX_LINES} lines")
    lines.append("")
    lines.append(f"{s['rows_ok']} ready")
    lines.append(f"{s['rows_blocked']} blocked")
    for row in result.report:
        if row["status"] == "ok":
            continue
        lines.append(f"  row {row['row_no']} \u2014 {html.escape(row['message'])}")
    if meta.get("populated_below_range"):
        lines.append("")
        lines.append(f"\u26a0\ufe0f {meta['populated_below_range']} row(s) below "
                     f"the marked line were ignored.")
    lines.append("")
    lines.append("Nothing has been submitted. Fix the rows and send the file again.")

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("\u2b07\ufe0f Download report", callback_data="up:rep"),
        InlineKeyboardButton("\u2716\ufe0f Cancel", callback_data="cancel"),
    ]])
    await target.reply_text("\n".join(lines)[:4000], reply_markup=kb)


async def _post_ready(target, context, result, meta):
    s = result.summary
    lines = ["\u2705 Upload checked \u2014 ready", "",
             f"{s['category_label']} \u00b7 {html.escape(s['supplier'])}",
             f"{_plural(s['rows_ok'], 'line')} \u00b7 {s['total_units']} units", ""]
    for i, it in enumerate(result.items, 1):
        pack = f" ({html.escape(str(it['pack']))})" if it["pack"] else ""
        lines.append(f"{i}. {html.escape(it['item'])} \u00d7{it['qty']}{pack}")
    if not s["template_matches"]:
        lines.append("")
        lines.append("\u26a0\ufe0f This file was generated for a different "
                     "supplier or list. The codes decide \u2014 shown above.")

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"\u2705 Continue ({_plural(s['rows_ok'], 'line')})",
                             callback_data="up:go"),
        InlineKeyboardButton("\u2716\ufe0f Cancel", callback_data="cancel"),
    ]])
    await target.reply_text("\n".join(lines)[:4000], reply_markup=kb)


async def _send_report(q, context):
    up = context.user_data.get("upload")
    if not up:
        await q.message.reply_text("That upload expired. Send the file again.")
        return
    built = await asyncio.to_thread(xl.write_report, up["result"], up["meta"])
    await context.bot.send_document(
        chat_id=q.message.chat_id, document=built["bytes"],
        filename=built["filename"],
        caption="Fix the highlighted rows in your own file, then send it to me "
                "again.",
        read_timeout=60, write_timeout=60, connect_timeout=20)


async def _confirm(q, context):
    up = context.user_data.get("upload")
    if not up:
        await q.message.reply_text("That upload expired. Send the file again.")
        return
    result = up["result"]
    if result.blocked or not result.items:
        await q.message.reply_text("That upload still has rows to fix.")
        return
    s = result.summary

    context.user_data["draft"] = {
        "items": [dict(it) for it in result.items],
        "supplier": s["supplier"],
        "urgent": None,
        "reason": None,
        "category": s["category"],
        "editing_po": up.get("editing_po"),
        "upload_id": up["id"],
    }
    context.user_data["upload_supersedes"] = up["id"]
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    if s["cheaper_elsewhere"]:
        context.user_data["state"] = "supplier_why"
        names = "\n".join(f"  \u2022 {html.escape(n)}"
                          for n in s["cheaper_elsewhere"][:6])
        await q.message.reply_text(
            f"{_plural(len(s['cheaper_elsewhere']), 'item')} on this order "
            f"can be bought cheaper from another supplier:\n{names}\n\n"
            f"Why {html.escape(s['supplier'])} for this order? Reply with a short "
            f"note \u2014 the approvers will see it.")
        return

    await _HOOKS["ask_reason"](q, context)


async def handle_supplier_why(update, context):
    """state == 'supplier_why'. Called from bot.on_text."""
    reason = update.message.text.strip()
    if not reason:
        await update.message.reply_text("Please type a short reason.")
        return
    draft = context.user_data.get("draft") or {}
    draft["supplier_reason"] = reason
    context.user_data["state"] = None
    await _HOOKS["ask_reason"](update, context)


def register(app, hooks):
    _HOOKS.update(hooks)
    app.add_handler(MessageHandler(
        filters.Document.ALL & filters.ChatType.PRIVATE, on_document))
    app.add_handler(CallbackQueryHandler(_route, pattern=r"^up:"))


async def _route(update, context):
    q = update.callback_query
    await q.answer()
    await cb_entry(q, context, q.data or "")
