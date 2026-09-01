"""Google Drive archive for PO supporting documents.

Telegram holds the file, and `file_id` in the sheet points at it. That is
enough right up to the day someone clears the chat history, and then the
`sha256` on the record can still prove a file is not the original but cannot
produce the original. This module closes that gap: a second copy in a folder
the company owns.

Three things it deliberately does NOT do:

  * **It never gates a PO.** Every failure here returns "" and logs a warning.
    A purchase order that cannot reach the Finance manager because Google had
    a bad minute is a worse outcome than an archive with a hole in it, and the
    hole is visible -- `drive_url` is blank on the row.

  * **It never reaches outside its folder.** The scope is `drive.file`, which
    grants access to files this application created plus whatever was
    explicitly shared with it. The one shared thing is
    `ATTACHMENTS_FOLDER_ID`. Everything else in that Drive -- and it is a
    personal Drive holding unrelated confidential work -- is invisible to this
    credential, enforced by Google rather than by the care of this code.

  * **It never deletes.** There is no delete path in this module at all. A
    superseded attachment stays where it is; the sheet records which row
    replaced it.

A service account has no Drive storage quota of its own, so files MUST be
created inside a folder owned by a real account and shared with the service
account. That is why `ATTACHMENTS_FOLDER_ID` is required for the archive to
work at all, and why a blank one is a supported configuration rather than an
error.
"""
import io
import json
import logging
import threading

from config import Config

log = logging.getLogger("po_bot.drive")

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
FOLDER_MIME = "application/vnd.google-apps.folder"

_lock = threading.Lock()
_service = None
_folders = {}       # (parent_id, name) -> folder id
_unavailable = None  # set to a reason string once, so we warn once not once per file


def available():
    return bool(Config.ATTACHMENTS_FOLDER_ID) and _unavailable is None


def _client():
    """Built lazily and only if a folder is configured, so a deployment that
    does not use the archive never needs the Drive libraries to import
    cleanly."""
    global _service, _unavailable
    if _service is not None:
        return _service
    from googleapiclient.discovery import build
    from google.oauth2.service_account import Credentials
    info = json.loads(Config.GOOGLE_CREDENTIALS_JSON)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    _service = build("drive", "v3", credentials=creds, cache_discovery=False)
    return _service


def _subfolder(name, parent):
    """Find-or-create one folder under `parent`.

    `files.list` under `drive.file` only ever returns files this application
    created, which is exactly the set of subfolders we are looking for -- so
    the search cannot accidentally match something of the owner's that happens
    to share a name.
    """
    key = (parent, name)
    with _lock:
        if key in _folders:
            return _folders[key]
    svc = _client()
    safe = name.replace("'", "\\'")
    q = (f"name = '{safe}' and mimeType = '{FOLDER_MIME}' "
         f"and '{parent}' in parents and trashed = false")
    found = svc.files().list(q=q, fields="files(id)", pageSize=1,
                             supportsAllDrives=True).execute().get("files", [])
    fid = (found[0]["id"] if found else
           svc.files().create(
               body={"name": name, "mimeType": FOLDER_MIME, "parents": [parent]},
               fields="id", supportsAllDrives=True).execute()["id"])
    with _lock:
        _folders[key] = fid
    return fid


def upload(po_no, filename, data, mime, year):
    """Archive one file. Returns its Drive URL, or "" if the archive is off or
    unreachable. Never raises."""
    global _unavailable
    if not Config.ATTACHMENTS_FOLDER_ID:
        return ""
    try:
        from googleapiclient.http import MediaIoBaseUpload
        year_id = _subfolder(str(year), Config.ATTACHMENTS_FOLDER_ID)
        po_id = _subfolder(f"PO_{po_no}", year_id)
        media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime, resumable=False)
        f = _client().files().create(
            body={"name": filename, "parents": [po_id]},
            media_body=media, fields="id, webViewLink",
            supportsAllDrives=True).execute()
        _unavailable = None
        return f.get("webViewLink") or f"https://drive.google.com/file/d/{f['id']}/view"
    except Exception as e:
        # Logged at warning, once per distinct reason. The PO carries on; the
        # blank drive_url on the sheet row is the visible record that this
        # file has only its Telegram copy.
        reason = f"{type(e).__name__}: {e}"
        if reason != _unavailable:
            _unavailable = reason
            log.warning("Drive archive unavailable (PO %s, %s): %s",
                        po_no, filename, reason)
        return ""
