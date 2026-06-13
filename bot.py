"""CML Purchase Order approval bot (Telegram + Google Sheets)."""
import asyncio
import difflib
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
from sheets import sheets, norm_supplier
from pdf import generate_po_pdf, ensure_fonts
import flow

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
    context.user_data["draft"] = {"items": [], "supplier": None, "urgent": None,
                                  "reason": None, "category": None, "editing_po": None}
    context.user_data["state"] = None
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("\U0001f9ea Laboratory consumption", callback_data="cat:lab")],
        [InlineKeyboardButton("\U0001f4e6 Other", callback_data="cat:other")],
    ])
    await update.message.reply_text("New PO \u2014 choose the type:", reply_markup=kb)


async def _cb_category(q, context, cat):
    if cat not in flow.CATEGORY_LABEL:
        return
    draft = _draft(context)
    draft["category"] = cat
    context.user_data["state"] = "item"
    label = flow.CATEGORY_LABEL[cat]
    if cat == "other":
        await q.message.reply_text(
            f"*{label}* PO. Send the item name \u2014 I'll suggest from your Other list, "
            f"or you can add a new one.", parse_mode=ParseMode.MARKDOWN)
    else:
        await q.message.reply_text(
            f"*{label}* PO. Send the *item name* (or part of it) to search the master list.",
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
    elif state == "other_price":
        await _handle_other_price(update, context)
    elif state == "other_reprice":
        await _handle_other_reprice(update, context)
    elif state == "other_supplier":
        await _handle_other_supplier(update, context)
    elif state == "variant_reason":
        await _handle_variant_reason(update, context)
    elif state == "reason":
        await _handle_po_reason(update, context)
    elif state == "editqty":
        await _handle_edit_qty(update, context)
    else:
        await update.message.reply_text("Send /new to start a PO, or /mypos to see yours.")


async def _handle_item_name(update, context):
    draft = _draft(context)
    name = update.message.text.strip()
    if draft.get("category") == "other":
        await _handle_other_item_name(update, context, name)
        return
    matches = await asyncio.to_thread(sheets.find_items, name)
    if matches:
        await _route_lab_item(update.message, context, matches)
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


async def _route_lab_item(target, context, matches):
    """One master row -> quantity. Several rows (same item, different supplier/pack) ->
    auto-pick the locked supplier's variant, or let the requester choose (prices hidden)."""
    draft = _draft(context)
    item_name = matches[0]["item"]
    if draft["supplier"]:
        same = [m for m in matches if m["supplier"] == draft["supplier"]]
        if not same:
            sups = ", ".join(dict.fromkeys(m["supplier"] for m in matches))
            await target.reply_text(
                f"\u26a0\ufe0f This PO is for *{draft['supplier']}*, but {item_name} is available "
                f"from: *{sups}*. One supplier per PO \u2014 send a different item, or submit this "
                f"PO and start a new one.", parse_mode=ParseMode.MARKDOWN)
            return
        matches = same
    if len(matches) == 1:
        await _ask_quantity(target, context, matches[0])
        return
    context.user_data["variants"] = matches
    context.user_data["state"] = "item"  # typing another name simply searches again
    rows = []
    for i, m in enumerate(matches):
        label = m["supplier"]
        if m.get("pack"):
            label += f" \u00b7 {m['pack']}"
        label += f" \u00b7 {flow.money(m.get('unit_price', 0))}"
        rows.append([InlineKeyboardButton(label[:60], callback_data=f"vr:{i}")])
    await target.reply_text(
        f"\u201c{item_name}\u201d is available from more than one supplier \u2014 "
        f"compare and pick one:",
        reply_markup=InlineKeyboardMarkup(rows))


async def _cb_variant(q, context, idx):
    variants = context.user_data.get("variants") or []
    if idx < 0 or idx >= len(variants):
        await q.message.reply_text("That option expired. Send the item name again.")
        return
    item = dict(variants[idx])
    draft = _draft(context)
    if draft["supplier"] and item["supplier"] != draft["supplier"]:
        await q.message.reply_text(
            f"\u26a0\ufe0f One supplier per PO ({draft['supplier']}). {item['item']} is from {item['supplier']}.")
        return
    # Compare across SUPPLIERS (cheapest variant per supplier), not across pack sizes:
    # picking a bigger pack from the same supplier needs no justification, but picking
    # a supplier whose best price is above another supplier's does.
    by_sup = {}
    for v in variants:
        p = float(v.get("unit_price", 0))
        s = v["supplier"]
        by_sup[s] = min(by_sup.get(s, p), p)
    cheapest_sup = min(by_sup, key=by_sup.get)
    if by_sup[item["supplier"]] - by_sup[cheapest_sup] > 0.005:
        context.user_data["pending_variant"] = item
        context.user_data["variants"] = None
        context.user_data["state"] = "variant_reason"
        await q.message.reply_text(
            f"\U0001f4b2 {cheapest_sup} offers {item['item']} from {flow.money(by_sup[cheapest_sup])}; "
            f"you picked {item['supplier']} at {flow.money(item['unit_price'])}.\n\n"
            f"Briefly, why this supplier? (e.g. out of stock, quality, delivery time) \u2014 "
            f"your note will be shown to the approvers.")
        return
    context.user_data.pop("variants", None)
    await _ask_quantity(q.message, context, item)


async def _handle_variant_reason(update, context):
    item = context.user_data.get("pending_variant")
    if not item:
        context.user_data["state"] = "item"
        await update.message.reply_text("Send the item name.")
        return
    reason = update.message.text.strip()
    if not reason:
        await update.message.reply_text("Please type a short reason for choosing this supplier.")
        return
    item["variant_reason"] = reason
    context.user_data.pop("pending_variant", None)
    await _ask_quantity(update.message, context, item)


async def _handle_other_item_name(update, context, name):
    draft = _draft(context)
    item = await asyncio.to_thread(sheets.find_other_item, name)
    if item:
        if draft["supplier"] and item["supplier"] != draft["supplier"]:
            await update.message.reply_text(
                f"\u26a0\ufe0f This PO is for *{draft['supplier']}*, but {item['item']} is from "
                f"*{item['supplier']}*. One supplier per PO.", parse_mode=ParseMode.MARKDOWN)
            return
        await _offer_reuse_price(update.message, context, item)
        return
    context.user_data["other_typed"] = name
    suggestions = await asyncio.to_thread(sheets.suggest_other_items, name)
    rows = []
    if suggestions:
        context.user_data["suggestions"] = suggestions
        rows = [[InlineKeyboardButton(s["item"], callback_data=f"pk:{i}")] for i, s in enumerate(suggestions)]
    rows.append([InlineKeyboardButton(f"\u2795 Add \u201c{name[:28]}\u201d as new", callback_data="onew")])
    rows.append([InlineKeyboardButton("\u2716\ufe0f Cancel", callback_data="cancel")])
    msg = (f"'{name}' isn't in the Other list yet. Pick a match or add it:"
           if suggestions else f"'{name}' isn't in the Other list yet. Add it as a new item?")
    await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(rows))


async def _cb_other_new(q, context):
    name = context.user_data.get("other_typed")
    if not name:
        context.user_data["state"] = "item"
        await q.message.reply_text("Send the item name.")
        return
    context.user_data["state"] = "other_price"
    await q.message.reply_text(f"Adding \u201c{name}\u201d. Send its *unit price* (number).",
                               parse_mode=ParseMode.MARKDOWN)


# ---- reusing an existing "Other" item: confirm or update its price ----
async def _offer_reuse_price(target, context, item):
    context.user_data["pending_other"] = dict(item)
    context.user_data["state"] = "item"  # typing another name simply searches again
    last = flow.money(item.get("unit_price", 0))
    lines = [f"\U0001f501 {item['item']}", f"Supplier: {item['supplier']}", f"Last price: {last}"]
    upd_at, upd_by = item.get("updated_at", ""), item.get("updated_by", "")
    if upd_at or upd_by:
        lines.append("(updated " + " by ".join(x for x in (upd_at, upd_by) if x) + ")")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"\u2705 Use {last}", callback_data="op:keep")],
        [InlineKeyboardButton("\u270f\ufe0f Enter new price", callback_data="op:new")],
    ])
    await target.reply_text("\n".join(lines), reply_markup=kb)


async def _cb_other_reuse(q, context, act):
    item = context.user_data.get("pending_other")
    if not item:
        context.user_data["state"] = "item"
        await q.message.reply_text("That item expired. Send the item name again.")
        return
    if act == "keep":
        it = dict(item)
        it["ref_price"] = it["unit_price"]
        context.user_data.pop("pending_other", None)
        await _ask_quantity(q.message, context, it)
    elif act in ("new", "re"):
        context.user_data["state"] = "other_reprice"
        await q.message.reply_text(
            f"Send the new unit price for \u201c{item['item']}\u201d "
            f"(last: {flow.money(item['unit_price'])}).")
    elif act == "cfm":
        new_price = context.user_data.pop("reprice_val", None)
        if new_price is None:
            context.user_data["state"] = "other_reprice"
            await q.message.reply_text("Send the new unit price.")
            return
        it = dict(item)
        it["ref_price"] = it["unit_price"]
        it["unit_price"] = new_price
        context.user_data.pop("pending_other", None)
        await _ask_quantity(q.message, context, it)


async def _handle_other_reprice(update, context):
    item = context.user_data.get("pending_other")
    if not item:
        context.user_data["state"] = "item"
        await update.message.reply_text("Send the item name.")
        return
    try:
        price = float(update.message.text.strip().replace("$", "").replace(",", ""))
        if price < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Send a valid price, e.g. 18.50")
        return
    price = round(price, 2)
    ref = float(item.get("unit_price", 0))
    if abs(price - ref) <= 0.005:  # same price — nothing changed
        it = dict(item)
        it["ref_price"] = ref
        context.user_data.pop("pending_other", None)
        context.user_data["state"] = None
        await _ask_quantity(update.message, context, it)
        return
    tol = Config.OTHER_PRICE_TOLERANCE_PCT
    pct = ((price - ref) / ref * 100.0) if ref > 0 else None
    if pct is None or abs(pct) >= tol:
        context.user_data["reprice_val"] = price
        pct_txt = f" ({pct:+.0f}%)" if pct is not None else ""
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"\u2705 Confirm {flow.money(price)}", callback_data="op:cfm")],
            [InlineKeyboardButton("\u21a9\ufe0f Re-enter price", callback_data="op:re")],
        ])
        await update.message.reply_text(
            f"\u26a0\ufe0f {flow.money(ref)} \u2192 {flow.money(price)}{pct_txt} \u2014 outside the "
            f"\u00b1{tol:.0f}% tolerance.\nApprovers will see this change flagged. Confirm?",
            reply_markup=kb)
        return
    it = dict(item)
    it["ref_price"] = ref
    it["unit_price"] = price
    context.user_data.pop("pending_other", None)
    context.user_data["state"] = None
    await _ask_quantity(update.message, context, it)


async def _handle_other_price(update, context):
    try:
        price = float(update.message.text.strip().replace("$", "").replace(",", ""))
        if price < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Send a valid price, e.g. 18.50")
        return
    context.user_data["other_price_val"] = round(price, 2)
    await _ask_other_supplier(update.message, context, user=update.effective_user)


# ---- supplier entry for a new "Other" item (pick-from-known first) ----
async def _ask_other_supplier(target, context, user=None):
    draft = _draft(context)
    name = context.user_data.get("other_typed", "this item")
    if draft["supplier"]:  # PO already locked to one supplier — don't ask, just use it
        await target.reply_text(f"Supplier: {draft['supplier']} (this PO's supplier).")
        await _finish_new_other(target, context, draft["supplier"], user=user)
        return
    opts = (await asyncio.to_thread(sheets.other_suppliers))[:8]
    context.user_data["supplier_opts"] = opts
    context.user_data["state"] = "other_supplier"
    if opts:
        rows = [[InlineKeyboardButton(s[:40], callback_data=f"sup:{i}")] for i, s in enumerate(opts)]
        rows.append([InlineKeyboardButton("\u2795 New supplier", callback_data="supnew")])
        await target.reply_text(f"Supplier for \u201c{name}\u201d? Pick one or type a name:",
                                reply_markup=InlineKeyboardMarkup(rows))
    else:
        await target.reply_text(f"Supplier for \u201c{name}\u201d? Type the name:")


async def _cb_supplier_pick(q, context, idx):
    opts = context.user_data.get("supplier_opts") or []
    if idx < 0 or idx >= len(opts):
        await q.message.reply_text("That option expired. Type the supplier name.")
        return
    await _finish_new_other(q.message, context, opts[idx], user=q.from_user)


async def _cb_supplier_fix(q, context, use_match):
    typed = context.user_data.pop("supplier_typed", None)
    match = context.user_data.pop("supplier_match", None)
    chosen = match if use_match else typed
    if not chosen:
        context.user_data["state"] = "other_supplier"
        await q.message.reply_text("Type the supplier name.")
        return
    await _finish_new_other(q.message, context, chosen, user=q.from_user)


async def _handle_other_supplier(update, context):
    draft = _draft(context)
    typed = update.message.text.strip()
    if not typed:
        await update.message.reply_text("Send the supplier name.")
        return
    if draft["supplier"] and typed != draft["supplier"]:
        await update.message.reply_text(
            f"\u26a0\ufe0f This PO is for *{draft['supplier']}*. Use the same supplier, "
            f"or start a new PO.", parse_mode=ParseMode.MARKDOWN)
        return
    known = await asyncio.to_thread(sheets.all_known_suppliers)
    nt = norm_supplier(typed)
    # Same name modulo case/punctuation ("abc co." vs "ABC Co") — merge silently.
    exact = next((s for s in known if norm_supplier(s) == nt), None)
    if exact:
        if exact != typed:
            await update.message.reply_text(f"Using existing supplier \u201c{exact}\u201d.")
        await _finish_new_other(update.message, context, exact, user=update.effective_user)
        return
    # Close-but-not-equal (likely typo) — ask before creating a near-duplicate.
    close = difflib.get_close_matches(nt, [norm_supplier(s) for s in known], n=1, cutoff=0.85)
    if close:
        match = next(s for s in known if norm_supplier(s) == close[0])
        context.user_data["supplier_typed"] = typed
        context.user_data["supplier_match"] = match
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"\u2705 Use \u201c{match[:35]}\u201d", callback_data="supfix:1")],
            [InlineKeyboardButton(f"\u2795 Keep \u201c{typed[:35]}\u201d as new", callback_data="supfix:0")],
        ])
        await update.message.reply_text(
            f"\u201c{typed}\u201d looks like existing supplier \u201c{match}\u201d. Same one?",
            reply_markup=kb)
        return
    await _finish_new_other(update.message, context, typed, user=update.effective_user)


async def _finish_new_other(target, context, supplier, user=None):
    name = context.user_data.get("other_typed", "")
    price = context.user_data.get("other_price_val", 0.0)
    item = {"item": name, "unit_price": price, "ref_price": price, "supplier": supplier,
            "material_code": "", "supplier_reagent": "", "pack": ""}
    by = fullname(user) if user else ""
    await asyncio.to_thread(sheets.add_other_item, name, price, supplier, by, now_str())
    for k in ("other_typed", "other_price_val", "supplier_opts"):
        context.user_data.pop(k, None)
    await _ask_quantity(target, context, item)


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
        "ref_price": item.get("ref_price", item["unit_price"]),
        "variant_reason": item.get("variant_reason", ""),
        "material_code": item.get("material_code", ""),
        "supplier_reagent": item.get("supplier_reagent", ""),
        "pack": item.get("pack", ""),
    })
    context.user_data["state"] = None
    context.user_data.pop("pending_item", None)
    await _show_items(update.message, context, prefix="\u2705 Added.")


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
    await q.answer()
    if data.startswith("cat:"):
        await _cb_category(q, context, data.split(":")[1])
    elif data == "onew":
        await _cb_other_new(q, context)
    elif data.startswith("op:"):
        await _cb_other_reuse(q, context, data.split(":")[1])
    elif data == "supnew":
        context.user_data["state"] = "other_supplier"
        await q.message.reply_text("Type the supplier name.")
    elif data.startswith("supfix:"):
        await _cb_supplier_fix(q, context, data.split(":")[1] == "1")
    elif data.startswith("sup:"):
        await _cb_supplier_pick(q, context, int(data.split(":")[1]))
    elif data.startswith("vr:"):
        await _cb_variant(q, context, int(data.split(":")[1]))
    elif data.startswith("pk:"):
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
    if draft.get("category") == "other":
        if draft["supplier"] and item["supplier"] != draft["supplier"]:
            await q.message.reply_text(
                f"\u26a0\ufe0f One supplier per PO ({draft['supplier']}). {item['item']} is from {item['supplier']}.")
            return
        await _offer_reuse_price(q.message, context, item)
        return
    # Lab: the suggestion carries one row, but the item may exist under several
    # suppliers/packs — re-resolve so all variants are considered.
    matches = await asyncio.to_thread(sheets.find_items, item["item"]) or [item]
    await _route_lab_item(q.message, context, matches)


async def _cb_submit(q, context):
    if not _draft(context)["items"]:
        await q.message.reply_text("Add at least one item first.")
        return
    context.user_data["state"] = "reason"
    await q.message.reply_text("What is the reason / purpose for this PO? Reply with a short note.")


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


async def _sync_other_prices(draft, by):
    """After submitting an Other-category PO, make changed prices the new reference
    in Other_Items. Per-PO history stays in Line_Items (ref_price vs unit_price)."""
    if draft.get("category") != "other":
        return
    for it in draft["items"]:
        ref = float(it.get("ref_price", it["unit_price"]))
        if abs(float(it["unit_price"]) - ref) > 0.005:
            try:
                await asyncio.to_thread(sheets.update_other_item_price,
                                        it["item"], it["unit_price"], by, now_str())
            except Exception as e:
                log.warning("Could not update reference price for %s: %s", it["item"], e)


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
        "reason": draft.get("reason", ""),
        "category": flow.CATEGORY_LABEL.get(draft.get("category"), ""),
        "stage": flow.STAGE_STOCK, "status": "active", "updated_at": now_str(),
    }
    await asyncio.to_thread(sheets.create_po, po)
    await asyncio.to_thread(sheets.add_line_items, po_no, draft["items"])
    await _sync_other_prices(draft, fullname(user))
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
        "supplier": po["supplier"], "urgent": flow.is_urgent(po),
        "reason": po.get("reason", ""),
        "category": flow.CATEGORY_CODE.get(str(po.get("category", "")).strip(), "lab"),
        "editing_po": po_no,
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
    await _sync_other_prices(draft, fullname(q.from_user))
    await asyncio.to_thread(
        sheets.update_po, po_no, supplier=draft["supplier"], total=total,
        urgent="yes" if draft["urgent"] else "no", reason=draft.get("reason", ""),
        stage=flow.STAGE_STOCK, status="active",
        reject_stage="", reject_reason="", updated_at=now_str())
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
    app.add_handler(CallbackQueryHandler(on_callback))
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
