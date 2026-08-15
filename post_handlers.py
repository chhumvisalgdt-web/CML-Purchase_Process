"""Telegram wiring for the post-approval stages and the two Excel side-files
that feed the approval stages.

Five entry points, all reusing the same parse -> preview -> confirm shape as
the PO upload path:

  /receive <po>   goods receipt          (stock controller group)
  sc:ask          stock count            (stock controller group, stage 1)
  bk:price        price confirmation     (bookkeeping group, stage 2)
  a:ordering:*    order placement        (ordering group)
  /outstanding    monthly cancellation   (finance group)

The document handler is shared: a spreadsheet arriving in any of these groups
is routed by what the pending request was, so the receiver never has to press
a button before sending a file.
"""
import asyncio
import hashlib
import logging
from datetime import date, datetime
from io import BytesIO

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler, filters

import flow
import receipt_excel as rx
import receipt_validate as rv
import side_excel as sx
from clock import now_str, today
from config import Config
from sheets import sheets

log = logging.getLogger("po_bot.post")

MAX_BYTES = Config.MAX_UPLOAD_BYTES
XL = ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      "application/vnd.ms-excel")


def fullname(u):
    n = " ".join(filter(None, [u.first_name, u.last_name])).strip()
    return f"{n} (@{u.username})" if u.username else n


def _is(chat_id, key):
    want = Config.CHAT_IDS.get(key)
    return want is not None and chat_id == want


def _wrong_file(meta, kind, po_no=None):
    """Every generated side-file carries the PO it was made for in its hidden
    _meta tab. Check it before writing anything.

    Only ONE request is pending per group at a time, and the stock group runs
    both stock counts and receipts, so a file that comes back late -- after
    someone has asked for a different PO -- would otherwise be applied to
    whatever PO happened to be pending, matched by material code. On the price
    file that silently rewrites approved unit prices on the wrong order.
    """
    got_kind = str(meta.get("kind", "")).strip()
    if got_kind and got_kind != kind:
        return (f"That looks like the {got_kind} file, not the {kind} one. "
                f"Send the file I asked for, or start again.")
    got_po = str(meta.get("po_no", "")).strip()
    if po_no is not None and got_po and got_po != str(po_no):
        return (f"This file was generated for PO #{got_po}, but the request "
                f"waiting here is for PO #{po_no}. Nothing has been recorded "
                f"— ask for a fresh file for the PO you mean.")
    return ""


async def _download(doc, context):
    if doc.file_size and doc.file_size > MAX_BYTES:
        return None, f"File is too large ({doc.file_size // 1024} KB)."
    if doc.mime_type and doc.mime_type not in XL and not doc.file_name.endswith(
            (".xlsx", ".xlsm")):
        return None, "Send the Excel file, not a PDF or a photo."
    f = await context.bot.get_file(doc.file_id)
    buf = BytesIO()
    await f.download_to_memory(buf)
    return buf.getvalue(), ""


# ===================== goods receipt =====================

async def cmd_receive(update, context):
    """A receipt always belongs to a PO. Goods arriving against no PO are a
    process exception handled outside the bot -- recording them here would
    legitimise ordering without approval."""
    if not _is(update.effective_chat.id, "stock"):
        return
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Send /receive followed by the PO number, e.g. /receive 173.")
        return
    po_no = str(args[0]).strip().lstrip("#")
    po = await asyncio.to_thread(sheets.get_po, po_no)
    if not po:
        await update.message.reply_text(f"PO #{po_no} not found.")
        return
    stage = str(po.get("stage", "")).strip()
    if stage != flow.STAGE_RECEIVING:
        await update.message.reply_text(
            f"PO #{po_no} is at '{flow.STAGE_LABEL.get(stage, stage)}'. "
            f"It can only be received once it is fully approved.")
        return

    lines = await asyncio.to_thread(sheets.get_line_items, po_no)
    receipts = await asyncio.to_thread(sheets.get_receipts, po_no)
    outstanding = rv.outstanding_from(lines, receipts)
    if not outstanding:
        await update.message.reply_text(
            f"PO #{po_no} has nothing outstanding \u2014 every line is fully "
            f"received.")
        return

    built = await asyncio.to_thread(rx.build_receipt_file, po_no, outstanding)
    context.chat_data["pending"] = {"kind": "receipt", "po_no": po_no}
    await update.message.reply_document(
        document=BytesIO(built["bytes"]), filename=built["filename"],
        caption=(f"PO #{po_no} \u00b7 {po.get('supplier', '')}\n"
                 f"{built['lines']} line(s) still outstanding.\n\n"
                 "Enter what you counted, not what the invoice says. "
                 "Invoice no. and date are required. Send the file back when "
                 "you are done."))


async def _handle_receipt(update, context, data, po_no):
    rows, meta = await asyncio.to_thread(rx.read_receipt, data)
    if meta.get("error"):
        await update.message.reply_text(meta["error"])
        return
    if meta.get("po_no") and str(meta["po_no"]) != str(po_no):
        await update.message.reply_text(
            f"This file was generated for PO #{meta['po_no']}, but the last "
            f"/receive was for #{po_no}. Send /receive {meta['po_no']} first.")
        return

    lines = await asyncio.to_thread(sheets.get_line_items, po_no)
    receipts = await asyncio.to_thread(sheets.get_receipts, po_no)
    outstanding = rv.outstanding_from(lines, receipts)
    # Expiry and short-dating are judged against the local calendar date, not
    # the server's -- Phnom Penh is 7 hours ahead of a UTC container.
    res = rv.validate(rows, outstanding, invoice_no=meta["invoice_no"],
                      invoice_date=meta["invoice_date"], today=today())

    if res.blocked:
        rep = await asyncio.to_thread(rx.write_report, res, meta)
        await update.message.reply_document(
            document=BytesIO(rep["bytes"]), filename=rep["filename"],
            caption=_receipt_card(po_no, res, blocked=True))
        return

    context.chat_data["pending"] = {
        "kind": "receipt_confirm", "po_no": po_no,
        "receipts": res.receipts, "summary": res.summary,
        "invoice_no": meta["invoice_no"],
        "invoice_date": _fmt_date(meta["invoice_date"]),
    }
    kb = [[InlineKeyboardButton("\u2705 Record receipt",
                                callback_data=f"rc:save:{po_no}"),
           InlineKeyboardButton("\u2716\ufe0f Cancel", callback_data="rc:cancel")]]
    await update.message.reply_text(
        _receipt_card(po_no, res), reply_markup=InlineKeyboardMarkup(kb))


def _fmt_date(v):
    if isinstance(v, (datetime, date)):
        return v.strftime("%d-%b-%Y")
    return str(v or "").strip()


def _receipt_card(po_no, res, blocked=False):
    s = res.summary
    out = [f"PO #{po_no} \u00b7 invoice {s.get('invoice_no') or '(missing)'}"]
    if blocked:
        out.append(f"\n\u26a0\ufe0f {s['rows_blocked']} row(s) need fixing. "
                   f"Nothing has been recorded.")
        for r in res.report:
            if r["status"] in rv.BLOCKING:
                where = f"row {r['row_no']}" if r["row_no"] else "header"
                out.append(f"  {where} \u2014 {r['status'].replace('_', ' ')}")
        return "\n".join(out)

    out.append(f"{s['rows_ok']} entry(s) \u00b7 {s['total_units']} unit(s)")
    out.append("")
    for l in s["lines_after"]:
        if not l["received_now"]:
            continue
        mark = {"complete": "\u2705", "over": "\u26a0\ufe0f",
                "partial": "\u23f3"}.get(l["state"], "")
        out.append(f"{mark} {l['item']}: +{l['received_now']} "
                   f"({l['total']}/{l['ordered']})")
    for r in res.report:
        if r["status"] in rv.CONFIRMABLE:
            out.append(f"\u26a0\ufe0f {r['matched_item']}: {r['message']}")
    still = [l for l in s["lines_after"] if l["state"] in ("open", "partial")]
    if still:
        out.append(f"\n{len(still)} line(s) still outstanding after this.")
    else:
        out.append("\nThis completes the PO.")
    return "\n".join(out)


async def _cb_receipt_save(q, context, po_no):
    p = context.chat_data.get("pending") or {}
    if p.get("kind") != "receipt_confirm" or str(p.get("po_no")) != str(po_no):
        await q.message.reply_text("That receipt is no longer pending. "
                                   "Send /receive again.")
        return
    by, at = fullname(q.from_user), now_str()
    n = await asyncio.to_thread(
        sheets.add_receipts, po_no, p["receipts"], p["invoice_no"],
        p["invoice_date"], by, at)
    context.chat_data.pop("pending", None)

    receipts = await asyncio.to_thread(sheets.get_receipts, po_no)
    lines = await asyncio.to_thread(sheets.get_line_items, po_no)
    outstanding = rv.outstanding_from(lines, receipts)
    po = await asyncio.to_thread(sheets.get_po, po_no)

    if outstanding:
        await asyncio.to_thread(sheets.update_po, po_no,
                                stage=flow.STAGE_RECEIVING,
                                received_status="partial", updated_at=at)
        await q.message.reply_text(
            f"Recorded {n} entry(s) on PO #{po_no}. "
            f"{len(outstanding)} line(s) still outstanding \u2014 send "
            f"/receive {po_no} again when the rest arrives.")
    else:
        await asyncio.to_thread(sheets.update_po, po_no, stage=flow.STAGE_CLOSED,
                                status="closed", received_status="complete",
                                closed_at=at, updated_at=at)
        await q.message.reply_text(
            f"Recorded {n} entry(s). PO #{po_no} is fully received and closed.")

    flags = [r for r in (p.get("summary") or {}).get("confirm_by_status", {})]
    if flags:
        await _tell_ordering(
            context,
            f"PO #{po_no} \u00b7 {po.get('supplier', '')}\n"
            f"Receipt recorded with: {', '.join(f.replace('_', ' ') for f in flags)}.\n"
            f"Invoice {p['invoice_no']}. Worth raising with the supplier.")


async def _tell_ordering(context, text):
    """Discrepancies go to the group that sent the order to the supplier --
    they are the ones who chase it. Nothing is unwound: a short delivery is a
    fact about what arrived, not a rejection."""
    cid = Config.CHAT_IDS.get("approved")
    if cid:
        try:
            await context.bot.send_message(chat_id=cid, text=text)
        except Exception as e:
            log.warning("ordering notify failed: %s", e)


# ===================== stock count (stage 1) =====================

async def cb_stock_ask(q, context, po_no):
    lines = await asyncio.to_thread(sheets.get_line_items, po_no)
    if not lines:
        await q.message.reply_text("No line items on that PO.")
        return
    built = await asyncio.to_thread(sx.build_stock_file, po_no, lines)
    context.chat_data["pending"] = {"kind": "stock", "po_no": po_no}
    await q.message.reply_document(
        document=BytesIO(built["bytes"]), filename=built["filename"],
        caption=(f"PO #{po_no} \u2014 enter units on hand for each line.\n"
                 "Enter #N/A if a count is not possible. Send the file back, "
                 "then tap Checked."))


async def _handle_stock(update, context, data, po_no):
    rows, meta = await asyncio.to_thread(sx.read_stock, data)
    if meta.get("error"):
        await update.message.reply_text(meta["error"])
        return
    wrong = _wrong_file(meta, "stock", po_no)
    if wrong:
        await update.message.reply_text(wrong)
        return
    lines = await asyncio.to_thread(sheets.get_line_items, po_no)
    counts, errs = sx.validate_stock(rows, lines)
    if errs:
        await update.message.reply_text(
            "\u26a0\ufe0f Stock count not recorded:\n" +
            "\n".join(f"  row {e['row_no']} \u2014 {e['message']}" for e in errs[:10]))
        return

    by, at = fullname(update.effective_user), now_str()
    await asyncio.to_thread(sheets.add_stock_counts, po_no, counts, by, at)
    await asyncio.to_thread(
        sheets.update_lines, po_no,
        {c["line_id"]: {"on_hand": c["on_hand"]} for c in counts})
    context.chat_data.pop("pending", None)
    na = sum(1 for c in counts if str(c["on_hand"]).upper() in ("#N/A", "N/A"))
    await update.message.reply_text(
        f"Stock count recorded for PO #{po_no} ({len(counts)} line(s)"
        + (f", {na} without a count" if na else "") +
        "). Approvers will see it on the card and the PDF. You can tap Checked now.")


# ===================== price confirmation (stage 2) =====================

async def cb_price_ask(q, context, po_no):
    lines = await asyncio.to_thread(sheets.get_line_items, po_no)
    if not lines:
        await q.message.reply_text("No line items on that PO.")
        return
    built = await asyncio.to_thread(sx.build_price_file, po_no, lines)
    context.chat_data["pending"] = {"kind": "price", "po_no": po_no}
    await q.message.reply_document(
        document=BytesIO(built["bytes"]), filename=built["filename"],
        caption=(f"PO #{po_no} \u2014 confirmed prices from the supplier.\n"
                 "Leave a line blank to keep the master-list price. Anything "
                 "you change is flagged to Finance, GM and the Board."))


async def _handle_price(update, context, data, po_no):
    rows, meta = await asyncio.to_thread(sx.read_price, data)
    if meta.get("error"):
        await update.message.reply_text(meta["error"])
        return
    wrong = _wrong_file(meta, "price", po_no)
    if wrong:
        await update.message.reply_text(wrong)
        return
    lines = await asyncio.to_thread(sheets.get_line_items, po_no)
    changes, errs = sx.validate_price(rows, lines)
    if errs:
        await update.message.reply_text(
            "\u26a0\ufe0f Prices not updated:\n" +
            "\n".join(f"  row {e['row_no']} \u2014 {e['message']}" for e in errs[:10]))
        return
    if not changes:
        await update.message.reply_text(
            "No price changes in that file \u2014 nothing updated.")
        return

    # ref_price keeps the master figure, so the approvers' cards and PDF page 2
    # show approved-versus-confirmed for the life of the PO.
    by_line = {c["line_id"]: {"unit_price": c["new_price"],
                              "line_total": c["new_total"]} for c in changes}
    await asyncio.to_thread(sheets.update_lines, po_no, by_line)
    lines = await asyncio.to_thread(sheets.get_line_items, po_no)
    total = round(sum(l["line_total"] for l in lines), 2)
    await asyncio.to_thread(sheets.update_po, po_no, total=total,
                            price_confirmed_by=fullname(update.effective_user),
                            price_confirmed_at=now_str(), updated_at=now_str())
    context.chat_data.pop("pending", None)

    out = [f"PO #{po_no} \u2014 {len(changes)} price(s) confirmed:"]
    for c in changes:
        pct = ((c["new_price"] - c["old_price"]) / c["old_price"] * 100
               if c["old_price"] else None)
        out.append(f"  {c['item']}: {flow.money(c['old_price'])} \u2192 "
                   f"{flow.money(c['new_price'])}"
                   + (f" ({pct:+.0f}%)" if pct is not None else ""))
    out.append(f"\nNew total: {flow.money(total)}. Tap Booked to send it on.")
    await update.message.reply_text("\n".join(out))


# ===================== cancellation review =====================

async def cmd_outstanding(update, context):
    if not _is(update.effective_chat.id, "fin"):
        return
    rows = await asyncio.to_thread(sheets.open_lines)
    if not rows:
        await update.message.reply_text("Nothing outstanding \u2014 every PO is "
                                        "fully received or closed.")
        return
    built = await asyncio.to_thread(sx.build_outstanding_file, rows)
    context.chat_data["pending"] = {"kind": "cancel"}
    await update.message.reply_document(
        document=BytesIO(built["bytes"]), filename=built["filename"],
        caption=(f"{len(rows)} outstanding line(s) across "
                 f"{len({r['po_no'] for r in rows})} PO(s).\n"
                 "Put x in the Remove column and give a reason, then send the "
                 "file back. Only the un-received remainder is cancelled."))


async def _handle_cancel(update, context, data):
    rows, meta = await asyncio.to_thread(sx.read_outstanding, data)
    if meta.get("error"):
        await update.message.reply_text(meta["error"])
        return
    wrong = _wrong_file(meta, "cancel")
    if wrong:
        await update.message.reply_text(wrong)
        return
    open_now = await asyncio.to_thread(sheets.open_lines)
    removals, errs = sx.validate_cancel(rows, open_now)
    if errs:
        await update.message.reply_text(
            "\u26a0\ufe0f Nothing cancelled:\n" +
            "\n".join(f"  row {e['row_no']} \u2014 {e['message']}" for e in errs[:10]))
        return
    if not removals:
        await update.message.reply_text("No lines marked for removal.")
        return

    context.chat_data["pending"] = {"kind": "cancel_confirm", "removals": removals}
    out = [f"\u26a0\ufe0f {len(removals)} line(s) to cancel:"]
    for r in removals[:15]:
        out.append(f"  PO #{r['po_no']} {r['item']} \u2014 {r['qty']} unit(s): "
                   f"{r['reason']}")
    if len(removals) > 15:
        out.append(f"  \u2026 and {len(removals) - 15} more")
    out.append("\nReceived quantities are untouched. Lines are marked "
               "cancelled, never deleted.")
    kb = [[InlineKeyboardButton("\u2705 Cancel these lines",
                                callback_data="cx:save"),
           InlineKeyboardButton("\u2716\ufe0f Keep", callback_data="cx:cancel")]]
    await update.message.reply_text("\n".join(out),
                                    reply_markup=InlineKeyboardMarkup(kb))


async def _cb_cancel_save(q, context):
    p = context.chat_data.get("pending") or {}
    if p.get("kind") != "cancel_confirm":
        await q.message.reply_text("That review is no longer pending. "
                                   "Send /outstanding again.")
        return
    by, at = fullname(q.from_user), now_str()
    by_po = {}
    for r in p["removals"]:
        by_po.setdefault(r["po_no"], {})[r["line_id"]] = {
            "cancelled_qty": r["qty"], "cancelled_by": by,
            "cancelled_at": at, "cancel_reason": r["reason"]}
    for po_no, fields in by_po.items():
        await asyncio.to_thread(sheets.update_lines, po_no, fields)
        lines = await asyncio.to_thread(sheets.get_line_items, po_no)
        receipts = await asyncio.to_thread(sheets.get_receipts, po_no)
        if not rv.outstanding_from(lines, receipts):
            await asyncio.to_thread(
                sheets.update_po, po_no, stage=flow.STAGE_CLOSED,
                status="closed", closed_at=at,
                closed_reason="cancelled at monthly review", updated_at=at)
    context.chat_data.pop("pending", None)
    await q.message.reply_text(
        f"Cancelled {len(p['removals'])} line(s) across {len(by_po)} PO(s).")


# ===================== shared document router =====================

async def on_document(update, context):
    doc = update.message.document
    if not doc:
        return
    pending = context.chat_data.get("pending") or {}
    kind = pending.get("kind")
    if kind not in ("receipt", "stock", "price", "cancel"):
        return
    data, err = await _download(doc, context)
    if err:
        await update.message.reply_text(err)
        return
    sha = hashlib.sha256(data).hexdigest()
    log.info("post-approval upload kind=%s sha=%s", kind, sha[:12])
    try:
        if kind == "receipt":
            await _handle_receipt(update, context, data, pending["po_no"])
        elif kind == "stock":
            await _handle_stock(update, context, data, pending["po_no"])
        elif kind == "price":
            await _handle_price(update, context, data, pending["po_no"])
        elif kind == "cancel":
            await _handle_cancel(update, context, data)
    except Exception:
        log.exception("post-approval upload failed")
        await update.message.reply_text(
            "Could not read that file. Ask for a fresh one and try again.")


async def on_callback(update, context):
    q = update.callback_query
    data = q.data or ""
    await q.answer()
    parts = data.split(":")
    try:
        if data.startswith("sc:ask:"):
            await cb_stock_ask(q, context, parts[2])
        elif data.startswith("bk:price:"):
            await cb_price_ask(q, context, parts[2])
        elif data.startswith("rc:save:"):
            await _cb_receipt_save(q, context, parts[2])
        elif data == "rc:cancel":
            context.chat_data.pop("pending", None)
            await q.message.reply_text("Receipt discarded. Nothing recorded.")
        elif data == "cx:save":
            await _cb_cancel_save(q, context)
        elif data == "cx:cancel":
            context.chat_data.pop("pending", None)
            await q.message.reply_text("No lines cancelled.")
    except Exception:
        log.exception("post-approval callback failed")
        await q.message.reply_text("Something went wrong. Try again.")


def register(app):
    """Registered BEFORE the generic callback handler: within a group the first
    handler added wins, so a broad handler registered first would swallow these
    with no error and nothing in the logs."""
    app.add_handler(CommandHandler("receive", cmd_receive))
    app.add_handler(CommandHandler("outstanding", cmd_outstanding))
    app.add_handler(CallbackQueryHandler(
        on_callback, pattern=r"^(sc:|bk:price:|rc:|cx:)"))
    app.add_handler(MessageHandler(
        filters.Document.ALL & ~filters.ChatType.PRIVATE, on_document))
