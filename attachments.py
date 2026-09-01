"""Supporting documents attached to a PO -- pure rules, no I/O.

The requester may attach the evidence behind a purchase: a quotation, a spec
sheet, a photo of the pump that failed. Management then approves against the
document rather than against a number someone typed.

Everything here is a decision, not an action, so it can be tested without a
Telegram token or a Drive credential. The I/O lives in `drive.py` (archive)
and `attach_handlers.py` (Telegram).

Two rules are worth stating because they are the whole point of the module:

  * **Who sees a file is decided here, once.** `delivery_stages` filters the
    configured list against a hard ban, so a mistyped env var cannot walk a
    supplier's price into the price-blind stock controller's group, or park a
    competitor's quotation in the group whose job is to forward documents to
    the supplier.

  * **A file is fingerprinted before it is delivered.** `sha256` goes on the
    record with the file, so the copy the Board saw can be proved to be the
    copy on file -- which is the only reason an attachment is worth anything
    to an auditor a year later.
"""
import hashlib
import os
import re

# What a requester may attach. Deliberately short: documents and pictures.
# Archives and anything executable are refused -- not because the bot would
# run them, but because a .zip is an unreviewable box and the approvers are
# being asked to review what is in front of them.
ALLOWED_EXT = {
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
}

# Stages that can never receive an attachment, whatever the configuration says.
# Mirrored in Config.ATTACH_NEVER; kept here too so the pure layer can be
# tested and reasoned about without importing config.
NEVER = ("stock", "approved", "cash")


def extension(name):
    return os.path.splitext(str(name or ""))[1].lower()


def mime_for(name):
    return ALLOWED_EXT.get(extension(name), "application/octet-stream")


def check(name, size, already, max_count, max_bytes):
    """May this file be attached? Returns (ok, message).

    `message` is shown to the requester verbatim, so it says what to do rather
    than what went wrong.
    """
    if already >= max_count:
        return False, (f"That is already {max_count} files, which is the limit. "
                       "Tap Done, or start again if you attached the wrong ones.")
    ext = extension(name)
    if not ext:
        return False, ("I cannot tell what kind of file that is. Send a PDF, a "
                       "photo, or an Excel or Word file.")
    if ext not in ALLOWED_EXT:
        return False, (f"{ext} files are not accepted. Send a PDF, a photo "
                       "(JPG/PNG), or an Excel or Word file.")
    if size and size > max_bytes:
        return False, (f"That file is {size / 1048576:.1f} MB, over the "
                       f"{max_bytes // 1048576} MB limit. Send a smaller copy "
                       "-- a scan at 150 dpi is usually enough to read.")
    return True, ""


def digest(data):
    return hashlib.sha256(data).hexdigest()


_UNSAFE = re.compile(r"[^A-Za-z0-9._\- ]+")


def archive_name(po_no, seq, original):
    """Name the file takes in the Drive archive.

    The PO number leads so a folder sorts usefully and a file that escapes its
    folder still says what it belongs to. The requester's own name is kept
    after it: `quotation_borey.pdf` tells a reviewer more than `att_2.pdf`.
    """
    stem, ext = os.path.splitext(str(original or "file"))
    stem = _UNSAFE.sub("_", stem)
    # Dots are legal in a name but a run of them is not: `..` is a path
    # traversal in every filesystem this name might later be written to, and
    # Drive is not the only place a copy of it can end up.
    stem = re.sub(r"\.{2,}", "_", stem).strip("_. ") or "file"
    ext = extension(original) or ""
    return f"PO_{po_no}_{seq}_{stem[:60]}{ext}"


def delivery_stages(category, configured):
    """Which stage groups receive the attachments for a PO of this category.

    The ban is applied here, not at the call site, so there is one place that
    decides and one place to test. An empty result is a valid answer: it means
    nothing is delivered, which is what a misconfiguration should degrade to.
    """
    order = ["book", "fin", "gm", "board"]
    allowed = [s for s in configured if s not in NEVER]
    return [s for s in order if s in allowed]


def caption(po_no, index, total, filename, requester=""):
    who = f" -- from {requester}" if requester else ""
    return (f"\U0001f4ce PO #{po_no} -- supporting document {index}/{total}"
            f"{who}\n{filename}\nInternal. Do not forward to the supplier.")


def summary_line(count):
    """One line for the approver's card."""
    if not count:
        return "Supporting documents: none attached"
    return f"Supporting documents: {count} attached"
