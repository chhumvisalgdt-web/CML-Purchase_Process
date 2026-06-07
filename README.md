# CML Purchase Order Bot

A Telegram bot that runs the CML purchase-order approval flow and stores everything in Google Sheets.

## Flow

1. **Requester** (DM with the bot) — `/new`, picks items from the master list, sets quantities, marks urgent, submits.
2. **Stock controller** (group) — Approve / Reject.
3. **Bookkeeping** (group) — receives the generated PO PDF, books in QBO, taps Booked / Reject.
4. **Finance manager** (group) — Approve / Reject.
5. Then it branches:
   - **Not urgent** → General manager (Approve) → Board of director (Approve) → Approved.
   - **Urgent** → approved straight after Finance; GM and Board are notified for information only.

The bot auto-fills unit price + supplier from your master list and computes totals. **The requester never sees prices or totals** — they pick items and quantities; pricing appears only to the Stock/Bookkeeping/Finance/GM/Board groups and on the PDF. One supplier per PO. A reject at any stage goes back to the requester with the reason; after they fix it, the PO **re-runs from the Stock controller**.

## Google Sheet

Use your existing **Supplier MasterList** sheet. Copy its ID from the URL into `SPREADSHEET_ID`, and **share it (Editor) with the service-account email** (the `client_email` in your credentials JSON — also printed in the logs on first start).

- **Master list** — your existing tab (default name `Table1`; change with `MASTER_TAB`). Columns are matched by name, so `No. / Material Code / CML Reagent / Supplier / Supplier Reagent / Price / Pack` works as-is. The requester searches by **CML Reagent**; **Price** is used for totals but never shown to the requester. The bot only reads this tab — it never edits it.
- **POs** and **Line_Items** — created automatically (in the same sheet, or in a separate `SPREADSHEET_ID` if you set `MASTER_SPREADSHEET_ID` to point the master elsewhere).

## Deploy on Railway

1. Push this folder to a GitHub repo and create a Railway project from it (same as your scheduler bot).
2. Set the environment variables from `.env.example` in Railway → Variables. Reuse your scheduler's service-account JSON for `GOOGLE_CREDENTIALS_JSON`.
3. Get the 5 group chat IDs: add the bot to each group, then send `/chatid` in that group and copy the ID into the matching variable. Group IDs start with `-100`.
4. Deploy. Railway runs the `worker` process (`python bot.py`).

## Telegram setup notes

- **Privacy mode can stay ON** (the BotFather default). Button taps always reach the bot; the only free text in a group is a rejection reason, and the bot prompts the rejecter to **reply** to its message (replies reach the bot even with privacy on).
- The requester does **not** need a chat ID — they press `/start` once and the bot remembers them.

## Commands

- `/new` — create a PO (DM only)
- `/mypos` — your POs and their status
- `/chatid` — show the current chat's ID (use in a group to get its ID)
- `/myid` — show your Telegram user ID

## Local run

```bash
pip install -r requirements.txt
export BOT_TOKEN=... SPREADSHEET_ID=... GOOGLE_CREDENTIALS_JSON='...'
export STOCK_CHAT_ID=... BOOKKEEPING_CHAT_ID=... FINANCE_CHAT_ID=... GM_CHAT_ID=... BOARD_CHAT_ID=...
python bot.py
```

## Files

- `bot.py` — handlers + approval state machine
- `flow.py` — stage order, transitions, keyboards, summary text
- `sheets.py` — Google Sheets storage (gspread)
- `pdf.py` — PO PDF generation (reportlab)
- `config.py` — environment-variable config
