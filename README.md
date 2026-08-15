# CML Purchase Order Bot

A Telegram bot that runs the CML purchase-order approval flow and stores everything in Google Sheets. Requesters submit items by **uploading a filled Excel template** — there is no item-by-item typing.

## Flow

1. **Requester** (DM) — `/new`, then either upload a filled template or ask for a blank one (Reagent or Other). The bot validates the file, shows a preview, and only then asks for the reason and the urgent flag.
2. **Stock controller** (group) — Approve / Reject.
3. **Bookkeeping** (group) — books in QBO, taps Booked / Reject.
4. **Finance manager** (group) — approves and picks the payment route: **Cash Advance** or **A/P**, or rejects.
5. Then it branches:
   - **Not urgent** → General manager → Board of director → Approved.
   - **Urgent** → approved straight after Finance; GM and Board are notified only.
6. Approved POs are posted to the **Approved POs** group (price-free), and additionally to the **Cash Advance** group when Finance chose that route.
7. **Approved POs group** — receives two PDFs and forwards the order to the supplier.
8. **Receiving** (stock controller group) — `/receive <po>` produces a file of the not-yet-received lines. Quantities are entered per line **per lot**, with lot number, expiry and the supplier's invoice number.
9. The PO **closes** once every line is fully received or cancelled.

Approval is no longer terminal — a PO waits for delivery before it closes. Urgent POs still skip GM/Board *approval*, but never skip receiving.

## The two approved PDFs

| File | Contents | Forward? |
|---|---|---|
| `PO_<no>.pdf` | Supplier code, the supplier's own item name, pack, qty, price, total | **Yes** — this is the order |
| `PO_<no>_approval.pdf` | The same order plus every sign-off, the stock count, price changes and justifications | **No** — internal |

Sending both, clearly named, removes the risk that someone forwards the internal file and the supplier sees your approval trail, your reference prices and your rejection reasons. Verified: the order copy contains no CML code, no approval trail and no reference price.

There is no ordering gate — nobody taps "Ordered". The trade-off is that `ordered_at` is not captured, so the system cannot tell "ordered, not yet delivered" from "nobody sent it", and supplier lead time is not measurable.

## Post-approval stages

### Stock count (stage 1)
The stock controller must enter units on hand before passing the PO on — `📊 Enter stock on hand` sends a small Excel file. `#N/A` is a permitted answer where a count is not possible, and it never coerces to 0: zero means "none in stock", which is the strongest possible argument *for* the order. Counts land in `Stock_Counts` (append-only) and appear as a **Stock** column on the order table of every approver's PDF, next to the quantity requested — the count exists so Finance, GM and the Board can judge whether the amount asked for is reasonable, so it has to be on their copy, not just the stock controller's.

### Price confirmation (stage 2)
Bookkeeping confirms the price with the supplier before Finance, GM and the Board approve, so the approvers see the real figure and nothing needs re-approving later. `💲 Confirm / update prices` sends a file with the master price and a blank confirmed-price column; a blank line keeps the master price. `ref_price` keeps the master figure permanently, so approved-versus-confirmed stays visible on PDF page 2 for the life of the PO.

### Supplier codes
The order PDF prints the **supplier's code (column H) and the supplier's item name**, never the internal CML code. The supplier code appears on that copy only — it is the supplier's own reference and means nothing to an approver, so approver copies carry the CML code alone and give the width to the item name. The ordering group keeps the order copy, so a delivery note can still be mapped back to a line. Column H may be blank — nothing is blocked, but the approved-PO card says how many lines are identified by the supplier's item name alone.

### Chasing approvals
When GM or the Board approves, Finance is notified. In practice the FM chases approvals, so `/pending` lists every PO awaiting a decision, which stage it sits at, and how many days it has been there.

### Price visibility
Every supplier-confirmed price that differs from the master list is shown, not only those above tolerance — bookkeeping confirms each price before the approvers see the PO, so a 5% rise is precisely what they are being asked to approve.

It appears on **page 1 only**, under the unit price in the order table, so the change sits with the item it belongs to. A legend under the table explains the `<<` mark, used for changes at or above `OTHER_PRICE_TOLERANCE_PCT`. Page 2 stays a pure approval trail. The supplier copy and the stock controller's price-blind copy show neither.

### One clock
Every timestamp the bot writes comes from `clock.py` in `TIMEZONE`. Approvals, receipts, stock counts, price confirmations, cancellations, the `Uploads` log and the generated files all agree, and expiry is judged against the local date. A container running UTC does not shift half the audit trail seven hours behind the other half.

### Working hours
Approved orders are dated for when they can actually be sent: **Mon–Sat, 08:00–17:00** (`WORK_DAYS`, `WORK_START_HOUR`, `WORK_END_HOUR`, in `TIMEZONE`). Approve at 19:00 on Friday and the PO is recorded for Saturday 08:00; approve on Saturday evening or Sunday and it rolls to Monday. Dating an after-hours approval as "now" would overstate how promptly the order went out.

`order_due` and `order_due_note` are stored on the PO and printed on page 2 as *Send to supplier*. **Public holidays are not handled** — the bot has no holiday calendar, so a Cambodian public holiday is still treated as a working day.

### Receiving
- **A receipt always belongs to a PO.** Goods arriving against no PO are a process exception handled outside the bot; recording them here would legitimise ordering without approval.
- The generated file contains **only outstanding lines**, so it shrinks with each delivery.
- One row per line **per lot** — a line arriving as two lots with different expiry dates uses both its rows. This is the one place the PO validator's duplicate-code rule is deliberately off.
- **Ambiguous dates are rejected, never guessed.** `03/15/2027` blocks with a message asking for `15-Mar-2027`.
- **Over-receipt, expired and short-dated confirm rather than block.** Refusing to record what actually arrived just makes the record disagree with the shelf. Discrepancies post to the ordering group, who chase the supplier; nothing is unwound.
- `Receipts` is append-only. A correction is a **negative row with a reason**, never an edit.
- The **invoice number** is the delivery reference, since GRN/GDN paperwork is not common practice locally. `Receiving now` is deliberately not pre-filled from the invoice, so the number recorded is one that was counted.

### Cancellation (monthly)
`/outstanding` in the Finance group lists every open line across all POs with a `Remove?` column. Removal cancels **only the un-received remainder** — a line at 6-of-10 cancels 4 and stays at 6 received. Lines are marked cancelled with a mandatory reason, never deleted: deleting would erase the evidence that the item was ever ordered.

A cancelled quantity stops being expected everywhere at once: it drops off the receipt file, the 7th unit against a 10-line with 4 cancelled is flagged as over-receipt, and a PO whose whole remainder is cancelled closes. There is one definition of "still owed" — `ordered − cancelled − received` — and the review list and the receiving path both use it.

Every stage receives the PO as a 2-page PDF. Page 1 is the order (the supplier copy); page 2 is the internal approval trail. A reject at any stage returns the PO to the requester with the reason; after they fix it, the PO **re-runs from the Stock controller** with every earlier sign-off cleared.

## The Excel upload path

The requester never types an item name, a supplier or a price.

- **Blank template** — generated live from the master list and filtered to one **category** and one **supplier**, so codes from anywhere else are physically absent from the file. `Material Code` is a validated dropdown; `Qty` accepts whole numbers only. Item, Supplier and Pack fill themselves by lookup.
- **Filled template** — send it to the bot in a DM at any time. Only rows 8–19 of the `PO Request` tab are read, and only columns B (code), F (qty) and G (note). The lookup columns are never read: item, supplier, pack and price are re-resolved from the live master by code, so a broken formula or a stale Master tab cannot affect what gets ordered.
- **All-or-nothing** — if any row is blocked, nothing is submitted. The bot returns her own file with `Status` and `What to do` columns appended; she fixes it and re-uploads.
- **Maximum 12 lines per PO** (`MAX_LINES`). A larger order becomes several files, one PO each.
- **One supplier and one category per PO.** Both are *derived from the codes*, never read from the file. The template's own metadata is recorded as provenance and flagged if it disagrees, but it can never change an outcome.

### Blocking statuses

`not_found`, `duplicate_code`, `no_price`, `bad_qty`, `missing_code`, `duplicate_line`, `over_limit`, `supplier_conflict`, `mixed_category`.

The last two should be unreachable through normal use, since foreign codes aren't in the file. That is why they are worth keeping: if one ever fires, the codes came from somewhere other than the template that was issued.

### Price-blindness

`Material Code` is the only key — item names never resolve anything. The requester sees no prices at any point, including in the preview. When a cheaper supplier exists for something on the order, the bot asks *why this supplier* and names the items **without showing any figure**. That justification is PO-level (the supplier is chosen once, before any item is seen) and appears on PDF page 2 for the price-visible approvers.

### Duplicate codes

A code that appears more than once across the two master tabs cannot identify one item. Such codes are **excluded from the index entirely** — never templated, and blocked on upload. There is no first-match fallback, because a wrong material code on a price-blind PO stays invisible until reconciliation. Run `/mastercheck` to list them.

## Google Sheet

Use your existing **Supplier MasterList** sheet. Copy its ID into `SPREADSHEET_ID` and share it (Editor) with the service-account email printed in the logs on first start.

- **`Reagent Master`** (`MASTER_TAB`) — `No. / Material Code / CML Reagent / Supplier / Supplier Reagent / Price / Pack`.
- **`Other Master`** (`OTHER_MASTER_TAB`) — same shape, with `Name` and `Unit` in place of `CML Reagent` and `Pack`. Non-reagent items must be pre-registered here with a code, a supplier and a price.
- The bot **only reads** both tabs. It never writes to a priced catalogue — that would put the requester back in the price-setting seat.
- **`POs`**, **`Line_Items`**, **`Uploads`**, **`Upload_Rows`**, **`Receipts`**, **`Stock_Counts`** — created automatically.
- **`Other_Items`** — the old free-entry list. No longer read or written; keep it as a frozen record so past POs stay explainable.

### Audit trail

`Uploads` records one row per **attempt**, written before parsing decides anything, including `sha256`, the Telegram `file_id`, `sheet_count` and `max_row` taken from the workbook *before* row iteration. That last pair is the completeness check: parsed output alone can never show that a row was silently dropped.

`Upload_Rows` records every row the reader **saw**, raw values verbatim, with its status. A log of what passed says nothing about what was rejected; the gap between the `raw_*` and `matched_*` columns is the evidence. Re-uploads chain through `supersedes`, so the correction history survives.

## Deploy on Railway

1. Push to GitHub and create a Railway project from it.
2. Set the variables from `.env.example`. **Rename the master tab and set `MASTER_TAB` in the same moment** — `get_master` returns `[]` for a missing tab, and an empty master means every upload fails with everything `not_found`.
3. Get the 7 group chat IDs with `/chatid` in each group.
4. Make the bot an admin with **Pin messages** in each group, then run `/setup` once per group. It unpins whatever was there and pins that group's own instructions — each group sees only what it needs.

Typing `/` shows a menu of that chat's commands. It is published automatically on every start, scoped per chat, so the stock group is offered `/receive` and the finance group `/pending` and `/outstanding` rather than one combined list where most entries do not apply. A group whose chat ID is not yet configured is skipped, so set the IDs and redeploy before expecting its menu.
5. Deploy. Railway runs `worker: python bot.py`.

## Telegram setup notes

- Privacy mode can stay ON. Button taps always reach the bot; the only free text in a group is a rejection reason, and the bot asks the rejecter to **reply** to its message.
- Documents are only accepted in a **private chat** with the bot.

## Commands

- `/new` — create a PO (DM only)
- `/mypos` — your POs and their status
- `/setup` — post and pin this group's own command card (run once in each group)
- `/pending` — POs awaiting approval and for how long (finance group)
- `/receive <po>` — goods receipt (stock controller group)
- `/outstanding` — monthly cancellation review (finance group)
- `/mastercheck` — list codes duplicated across the master tabs
- `/chatid`, `/myid`

## Local run

```bash
pip install -r requirements.txt
export BOT_TOKEN=... SPREADSHEET_ID=... GOOGLE_CREDENTIALS_JSON='...'
export STOCK_CHAT_ID=... BOOKKEEPING_CHAT_ID=... FINANCE_CHAT_ID=... GM_CHAT_ID=... BOARD_CHAT_ID=...
python bot.py
```

The validation modules themselves import neither Telegram nor gspread, but the
suite also covers the working-hours rules in `flow.py`, so install the
requirements first:

```bash
pip install -r requirements.txt pytest && python -m pytest -q
```

## Files

- `bot.py` — handlers + approval state machine
- `flow.py` — stage order, transitions, keyboards, summary text
- `sheets.py` — Google Sheets storage (gspread), including the upload audit tabs
- `pdf.py` — PO PDF generation (fpdf2 + HarfBuzz; `fonts/Battambang.ttf`)
- `config.py` — environment-variable config
- `upload_validate.py` — pure validation, no I/O
- `upload_excel.py` — template generation, workbook reading, validation report
- `upload_handlers.py` — Telegram wiring for the upload path
- `receipt_validate.py` / `receipt_excel.py` — goods receipt, pure + Excel layers
- `side_excel.py` — stock count, price confirmation, cancellation review
- `post_handlers.py` — Telegram wiring for the post-approval stages
- `group_handlers.py` — per-group command cards, pinning, and the finance chase list
- `clock.py` — the single timezone-aware clock every timestamp comes from
- `test_upload_validate.py`, `test_receipt_validate.py` — 48 tests

## Before first use

The master list contains at least one duplicated code (`ZD 3001-0104-2`, two different products at different prices). Affected items are unorderable by design until that is corrected. Run `/mastercheck`, fix the collisions, and re-run after populating `Other Master` — that is when new ones get introduced.
