"""Telegram wiring for PO supporting documents (DM only).

The step sits between the reason and the urgent flag, and only for the
categories in `ATTACH_CATEGORIES` -- "Other" by default. Reagent orders are
placed against a priced master list the approvers already trust; a purchase of
something nobody has bought before is the one that needs a quotation behind it.

It is **optional by design**. Nothing is blocked by an empty attachment list.
What the design refuses to do is stay quiet about it: a PO in an
attachment-eligible category that arrives with no document prints *"No
supporting document attached"* on the approvers' copy, so the absence is a
fact on the record rather than a silence. Blocking would have pushed people
to attach anything at all to get past the gate, which produces files nobody
reads and a control that measures compliance instead of evidence.

Files are collected but not yet stored: the PO has no number until the
requester confirms, and an attachment with no PO to belong to is litter. They
are downloaded, fingerprinted, archived and logged in `persist()`, once the PO
row exists.
"""
import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler

import attachments as att
import drive
from clock import local_now
from config import Config
from sheets import sheets

log = logging.getLogger("po_bot.attach")

_HOOKS = {}


def wanted(draft):
    """Is this a PO we ask for supporting documents on?"""
    if draft.get("editing_po"):
        # A resubmission keeps the documents already on the PO. Asking again
        # would invite a second copy of the same quotation and leave two rows
        # claiming to be the evidence.
        return False
    return str(draft.get("category", "")).lower() in Config.ATTACH_CATEGORIES


def _keyboard(count):
    label = "✓ Done" if count else "Skip — no documents"
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data="at:done")]])


async def ask(target, context):
    msg = getattr(target, "message", None) or target
    context.user_data["state"] = "attach"
    context.user_data.setdefault("attachments", [])
    await msg.reply_text(
        "Attach any supporting documents for this purchase — a quotation, "
        "a specification, a photo. Send them one at a time.\n\n"
        f"Optional. PDF, photo, Excel or Word; up to {Config.ATTACH_MAX_COUNT} "
        f"files, {Config.ATTACH_MAX_BYTES // 1048576} MB each.\n\n"
        "They go to Bookkeeping, the Finance manager, the GM and the Board. "
        "They are not sent to the stock controller and never to the supplier.",
        reply_markup=_keyboard(0))


async def on_document(update, context):
    """A document arriving while the attach step is open."""
    doc = update.message.document or update.message.photo
    files = context.user_data.setdefault("attachments", [])

    if update.message.document is None:
        # Photos sent as photos are compressed by Telegram and arrive without
        # a filename. Refusing is kinder than silently archiving a 40 kB
        # smear of a quotation that nobody can read the figures on.
        await update.message.reply_text(
            "Please send the picture as a *file* rather than a photo, so it "
            "keeps its full quality — in Telegram, attach it and turn off "
            "“compress”.", reply_markup=_keyboard(len(files)))
        return

    ok, why = att.check(doc.file_name, doc.file_size, len(files),
                        Config.ATTACH_MAX_COUNT, Config.ATTACH_MAX_BYTES)
    if not ok:
        await update.message.reply_text(why, reply_markup=_keyboard(len(files)))
        return

    files.append({"file_id": doc.file_id, "file_name": doc.file_name,
                  "size": doc.file_size or 0,
                  "mime": doc.mime_type or att.mime_for(doc.file_name)})
    n = len(files)
    await update.message.reply_text(
        f"Attached ({n}/{Config.ATTACH_MAX_COUNT}): {doc.file_name}\n"
        "Send another, or tap Done.", reply_markup=_keyboard(n))


async def _done(q, context):
    context.user_data["state"] = None
    files = context.user_data.get("attachments") or []
    if files:
        await q.message.reply_text(
            f"{len(files)} document(s) will go with this PO.")
    await _HOOKS["ask_urgent"](q.message)


async def persist(context, po_no, files, user):
    """Download, fingerprint, archive and log. Called once the PO row exists.

    Runs after the PO is created and never blocks it: an attachment that
    cannot be downloaded is logged with an empty sha256 and drive_url rather
    than aborting a purchase order that is otherwise complete. The gap is
    visible on the row, which is the honest outcome.
    """
    if not files:
        return []
    year = local_now().year
    rows = []
    for seq, f in enumerate(files, 1):
        sha, url, size = "", "", f.get("size", 0)
        name = att.archive_name(po_no, seq, f["file_name"])
        try:
            tg = await context.bot.get_file(f["file_id"])
            data = bytes(await tg.download_as_bytearray())
            sha = att.digest(data)
            size = len(data)
            url = await asyncio.to_thread(
                drive.upload, po_no, name, data, f["mime"], year)
        except Exception as e:
            log.warning("Attachment %s on PO %s could not be archived: %s",
                        f.get("file_name"), po_no, e)
        rows.append({
            "seq": seq, "uploaded_by": getattr(user, "full_name", "") or "",
            "uploaded_by_id": getattr(user, "id", ""), "role": "requester",
            "file_name": f["file_name"], "archive_name": name,
            "mime": f["mime"], "size": size, "sha256": sha,
            "file_id": f["file_id"], "drive_url": url, "delivered_to": "",
        })
    try:
        return await asyncio.to_thread(sheets.log_attachments, po_no, rows)
    except Exception as e:
        log.warning("Could not log attachments for PO %s: %s", po_no, e)
        return []


async def deliver(context, po_no, stage, po, requester=""):
    """Send this PO's attachments to one stage group.

    The stage has already been checked against `att.delivery_stages`; this
    function trusts that and does the sending. A failure is logged and the PO
    carries on -- the approver has the PO card and can ask for the file.
    """
    try:
        rows = await asyncio.to_thread(sheets.get_attachments, po_no)
    except Exception as e:
        log.warning("Could not read attachments for PO %s: %s", po_no, e)
        return 0
    if not rows:
        return 0
    chat_id = Config.CHAT_IDS.get(stage)
    if not chat_id:
        return 0
    sent = 0
    total = len(rows)
    for i, r in enumerate(rows, 1):
        try:
            await context.bot.send_document(
                chat_id=chat_id, document=r["file_id"],
                filename=r.get("file_name") or None,
                caption=att.caption(po_no, i, total, r.get("file_name", ""),
                                    requester),
                read_timeout=60, write_timeout=60, connect_timeout=20)
            sent += 1
        except Exception as e:
            log.warning("Attachment %s of PO %s did not reach %s: %s",
                        r.get("file_name"), po_no, stage, e)
    if sent:
        try:
            await asyncio.to_thread(sheets.mark_delivered, po_no, stage)
        except Exception as e:
            log.warning("Could not record attachment delivery for PO %s: %s",
                        po_no, e)
    return sent


def register(app, hooks):
    _HOOKS.update(hooks)
    app.add_handler(CallbackQueryHandler(_route, pattern=r"^at:"))


async def _route(update, context):
    q = update.callback_query
    await q.answer()
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    if (q.data or "") == "at:done":
        await _done(q, context)
