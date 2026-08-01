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
- **`POs`**, **`Line_Items`**, **`Uploads`**, **`Upload_Rows`** — created automatically.
- **`Other_Items`** — the old free-entry list. No longer read or written; keep it as a frozen record so past POs stay explainable.

### Audit trail

`Uploads` records one row per **attempt**, written before parsing decides anything, including `sha256`, the Telegram `file_id`, `sheet_count` and `max_row` taken from the workbook *before* row iteration. That last pair is the completeness check: parsed output alone can never show that a row was silently dropped.

`Upload_Rows` records every row the reader **saw**, raw values verbatim, with its status. A log of what passed says nothing about what was rejected; the gap between the `raw_*` and `matched_*` columns is the evidence. Re-uploads chain through `supersedes`, so the correction history survives.

## Deploy on Railway

1. Push to GitHub and create a Railway project from it.
2. Set the variables from `.env.example`. **Rename the master tab and set `MASTER_TAB` in the same moment** — `get_master` returns `[]` for a missing tab, and an empty master means every upload fails with everything `not_found`.
3. Get the 7 group chat IDs with `/chatid` in each group.
4. Deploy. Railway runs `worker: python bot.py`.

## Telegram setup notes

- Privacy mode can stay ON. Button taps always reach the bot; the only free text in a group is a rejection reason, and the bot asks the rejecter to **reply** to its message.
- Documents are only accepted in a **private chat** with the bot.

## Commands

- `/new` — create a PO (DM only)
- `/mypos` — your POs and their status
- `/mastercheck` — list codes duplicated across the master tabs
- `/chatid`, `/myid`

## Local run

```bash
pip install -r requirements.txt
export BOT_TOKEN=... SPREADSHEET_ID=... GOOGLE_CREDENTIALS_JSON='...'
export STOCK_CHAT_ID=... BOOKKEEPING_CHAT_ID=... FINANCE_CHAT_ID=... GM_CHAT_ID=... BOARD_CHAT_ID=...
python bot.py
```

Validation logic has no Telegram or Sheets dependency, so the tests run anywhere:

```bash
pip install pytest && python -m pytest test_upload_validate.py -q
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
- `test_upload_validate.py` — 17 tests

## Before first use

The master list contains at least one duplicated code (`ZD 3001-0104-2`, two different products at different prices). Affected items are unorderable by design until that is corrected. Run `/mastercheck`, fix the collisions, and re-run after populating `Other Master` — that is when new ones get introduced.
