"""
reMarkable cloud uploader.

Uploads a PDF to the reMarkable cloud so it syncs automatically to the device.

Two strategies, tried in order:

  1. rmapi (Go CLI) - preferred. Handles both old and new reMarkable sync
     protocols reliably. Install from: https://github.com/juruen/rmapi
     or the maintained fork: https://github.com/joagonca/rmapi

  2. REST API (pure Python fallback) - uses the older reMarkable v1 REST API
     directly. Works without installing Go tools but may break if reMarkable
     forces all accounts onto their new sync protocol (sync 1.5).

-- Setup (rmapi) -------------------------------------------------------
  macOS:   brew install rmapi   (or download binary from GitHub releases)
  Linux:   download binary from GitHub releases, put on PATH
  Windows: download .exe from GitHub releases, put on PATH

  First run: rmapi
  It will prompt you to visit https://my.remarkable.com/device/desktop/connect
  and enter the displayed code. This registers rmapi as a device.

-- Setup (REST API fallback) -------------------------------------------
  No install needed. On first use, call register_device() with a one-time
  code from https://my.remarkable.com/device/desktop/connect
  The device token is saved to ~/.polarisfolio_rm_token for reuse.

-- Usage ---------------------------------------------------------------
  uploader = RemarkableUploader()
  uploader.upload("My Planner", "/path/to/planner.pdf")

  # Upload into a folder
  uploader.upload("My Planner", "/path/to/planner.pdf", folder="/PolarisFolio")
"""

import os
import uuid
import json
import hashlib
import zipfile
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TOKEN_FILE = os.path.expanduser("~/.polarisfolio_rm_token")

# reMarkable REST API v1 endpoints (old protocol)
AUTH_HOST   = "https://webapp-prod.cloud.remarkable.engineering"
STORAGE_HOST = "https://document-storage-production-dot-remarkable-production.appspot.com"

DEVICE_DESC = "desktop-linux"
USER_AGENT  = "PolarisFolioClone/1.0"


# ---------------------------------------------------------------------------
# Strategy 1: rmapi subprocess
# ---------------------------------------------------------------------------

def _rmapi_available() -> bool:
    return shutil.which("rmapi") is not None


def _rmapi_upload(display_name: str, pdf_path: str, folder: str = None) -> bool:
    """
    Uploads a PDF via rmapi CLI.
    Returns True on success, False on failure.
    """
    target = f"{folder}/{display_name}" if folder else display_name

    # Ensure target folder exists
    if folder:
        try:
            subprocess.run(
                ["rmapi", "mkdir", folder],
                capture_output=True, timeout=30
            )
        except Exception:
            pass

    # Upload the PDF
    try:
        result = subprocess.run(
            ["rmapi", "put", pdf_path, target],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode == 0:
            print(f"  rmapi: uploaded '{display_name}' successfully")
            return True
        else:
            print(f"  rmapi error: {result.stderr.strip()}")
            return False
    except subprocess.TimeoutExpired:
        print("  rmapi: upload timed out")
        return False
    except Exception as e:
        print(f"  rmapi: unexpected error - {e}")
        return False


# ---------------------------------------------------------------------------
# Strategy 2: REST API (pure Python)
# ---------------------------------------------------------------------------

def _load_token() -> str | None:
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            return f.read().strip() or None
    return None


def _save_token(token: str):
    with open(TOKEN_FILE, "w") as f:
        f.write(token)
    os.chmod(TOKEN_FILE, 0o600)


def register_device(one_time_code: str) -> str:
    """
    Registers this app as a reMarkable device using a one-time code.
    Get the code from: https://my.remarkable.com/device/desktop/connect

    Returns and saves the device token for future use.
    """
    payload = {
        "code": one_time_code,
        "deviceDesc": DEVICE_DESC,
        "deviceID": str(uuid.uuid4()),
    }
    resp = requests.post(
        f"{AUTH_HOST}/token/json/2/device/new",
        json=payload,
        headers={"User-Agent": USER_AGENT},
        timeout=15,
    )
    resp.raise_for_status()
    device_token = resp.text.strip()
    _save_token(device_token)
    print(f"  Device registered. Token saved to {TOKEN_FILE}")
    return device_token


def _get_user_token(device_token: str) -> str:
    """Exchanges the device token for a short-lived user token."""
    resp = requests.post(
        f"{AUTH_HOST}/token/json/2/user/new",
        headers={
            "Authorization": f"Bearer {device_token}",
            "User-Agent": USER_AGENT,
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.text.strip()


def _build_document_zip(pdf_path: str, doc_id: str) -> bytes:
    """
    Packages a PDF into the zip format the reMarkable cloud expects.
    The zip contains:
      - {id}.pdf          - the PDF itself
      - {id}.content      - JSON metadata
      - {id}.pagedata     - blank page tags
    """
    with open(pdf_path, "rb") as f:
        pdf_bytes = f.read()

    content_meta = {
        "extraMetadata": {},
        "fileType": "pdf",
        "lastOpenedPage": 0,
        "lineHeight": -1,
        "margins": 100,
        "pageCount": 0,
        "textScale": 1,
        "transform": {},
    }

    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp_path = tmp.name

    with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{doc_id}.pdf", pdf_bytes)
        zf.writestr(f"{doc_id}.content", json.dumps(content_meta))
        zf.writestr(f"{doc_id}.pagedata", "")

    with open(tmp_path, "rb") as f:
        zip_bytes = f.read()

    os.unlink(tmp_path)
    return zip_bytes


def _rest_upload(
    display_name: str,
    pdf_path: str,
    folder: str = None,
) -> bool:
    """
    Uploads a PDF using the reMarkable v1 REST API directly.
    Returns True on success, False on failure.
    """
    device_token = _load_token()
    if not device_token:
        print("  REST API: no device token found.")
        print("  Run register_device() with a code from:")
        print("  https://my.remarkable.com/device/desktop/connect")
        return False

    try:
        user_token = _get_user_token(device_token)
    except Exception as e:
        print(f"  REST API: failed to get user token - {e}")
        return False

    auth_headers = {
        "Authorization": f"Bearer {user_token}",
        "User-Agent": USER_AGENT,
    }

    # Resolve folder ID if specified
    parent_id = ""
    if folder:
        parent_id = _find_or_create_folder(folder, auth_headers) or ""

    # Generate document ID
    doc_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    # Upload metadata
    metadata = [{
        "ID": doc_id,
        "Type": "DocumentType",
        "VissibleName": display_name,
        "Version": 1,
        "Parent": parent_id,
        "ModifiedClient": now,
    }]

    try:
        resp = requests.put(
            f"{STORAGE_HOST}/document-storage/json/2/upload/request",
            json=metadata,
            headers=auth_headers,
            timeout=15,
        )
        resp.raise_for_status()
        upload_info = resp.json()
    except Exception as e:
        print(f"  REST API: upload request failed - {e}")
        return False

    if not upload_info:
        print("  REST API: empty upload response")
        return False

    upload_url = upload_info[0].get("BlobURLPut", "")
    if not upload_url:
        print("  REST API: no upload URL in response")
        return False

    # Build and upload zip
    try:
        zip_bytes = _build_document_zip(pdf_path, doc_id)
        resp = requests.put(
            upload_url,
            data=zip_bytes,
            headers={"Content-Type": "application/octet-stream"},
            timeout=60,
        )
        resp.raise_for_status()
    except Exception as e:
        print(f"  REST API: zip upload failed - {e}")
        return False

    # Update status to mark upload complete
    status = [{
        "ID": doc_id,
        "Version": 1,
        "ModifiedClient": now,
        "Type": "DocumentType",
        "VissibleName": display_name,
        "Parent": parent_id,
        "Success": True,
    }]

    try:
        resp = requests.put(
            f"{STORAGE_HOST}/document-storage/json/2/upload/update-status",
            json=status,
            headers=auth_headers,
            timeout=15,
        )
        resp.raise_for_status()
        print(f"  REST API: uploaded '{display_name}' successfully (ID: {doc_id[:8]}...)")
        return True
    except Exception as e:
        print(f"  REST API: status update failed - {e}")
        return False


def _find_or_create_folder(folder_path: str, auth_headers: dict) -> str | None:
    """
    Finds an existing folder by name or creates it.
    Returns the folder ID, or None on failure.
    Simple implementation: only supports top-level folders.
    """
    folder_name = folder_path.strip("/").split("/")[0]

    try:
        resp = requests.get(
            f"{STORAGE_HOST}/document-storage/json/2/docs",
            headers=auth_headers,
            timeout=15,
        )
        resp.raise_for_status()
        docs = resp.json()

        for doc in docs:
            if doc.get("Type") == "CollectionType" and doc.get("VissibleName") == folder_name:
                return doc["ID"]

        # Create it
        folder_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        metadata = [{
            "ID": folder_id,
            "Type": "CollectionType",
            "VissibleName": folder_name,
            "Version": 1,
            "Parent": "",
            "ModifiedClient": now,
        }]

        resp = requests.put(
            f"{STORAGE_HOST}/document-storage/json/2/upload/request",
            json=metadata,
            headers=auth_headers,
            timeout=15,
        )
        resp.raise_for_status()

        status = [{
            "ID": folder_id,
            "Version": 1,
            "ModifiedClient": now,
            "Type": "CollectionType",
            "VissibleName": folder_name,
            "Parent": "",
            "Success": True,
        }]

        resp = requests.put(
            f"{STORAGE_HOST}/document-storage/json/2/upload/update-status",
            json=status,
            headers=auth_headers,
            timeout=15,
        )
        resp.raise_for_status()
        print(f"  REST API: created folder '{folder_name}' ({folder_id[:8]}...)")
        return folder_id

    except Exception as e:
        print(f"  REST API: folder lookup/create failed - {e}")
        return None


# ---------------------------------------------------------------------------
# Main uploader class
# ---------------------------------------------------------------------------

class RemarkableUploader:
    """
    Uploads PDFs to the reMarkable cloud.

    Tries rmapi first (if installed), falls back to REST API.
    Both strategies sync the file to the device automatically via Wi-Fi.
    """

    def __init__(self, prefer_rmapi: bool = True):
        self.prefer_rmapi = prefer_rmapi
        self._check_setup()

    def _check_setup(self):
        has_rmapi = _rmapi_available()
        has_token = _load_token() is not None

        if has_rmapi:
            print("  reMarkable uploader ready (using rmapi)")
        elif has_token:
            print("  reMarkable uploader ready (using REST API fallback)")
        else:
            print("  reMarkable uploader: not yet configured.")
            print()
            print("  Option A - install rmapi (recommended):")
            print("    macOS:  brew install rmapi")
            print("    Linux:  download from github.com/juruen/rmapi/releases")
            print("    Then run 'rmapi' once to authenticate.")
            print()
            print("  Option B - REST API (no install):")
            print("    1. Visit https://my.remarkable.com/device/desktop/connect")
            print("    2. Copy the 8-letter code")
            print("    3. Call: RemarkableUploader.setup(code='xxxxxxxx')")

    @staticmethod
    def setup(code: str):
        """Register via REST API using a one-time code from my.remarkable.com."""
        print(f"Registering with code: {code}")
        token = register_device(code)
        print("Setup complete. You can now upload PDFs.")
        return token

    def upload(
        self,
        display_name: str,
        pdf_path: str,
        folder: str = None,
    ) -> bool:
        """
        Upload a PDF to reMarkable cloud.

        Args:
            display_name: The name shown on the device (no .pdf extension needed)
            pdf_path: Local path to the PDF file
            folder: Optional folder path on the device (e.g. "/PolarisFolio")

        Returns:
            True if upload succeeded, False otherwise
        """
        if not os.path.exists(pdf_path):
            print(f"  Error: file not found - {pdf_path}")
            return False

        size_kb = os.path.getsize(pdf_path) / 1024
        print(f"\nUploading '{display_name}' ({size_kb:.0f} KB)...")

        if self.prefer_rmapi and _rmapi_available():
            print("  Strategy: rmapi")
            if _rmapi_upload(display_name, pdf_path, folder):
                return True
            print("  rmapi failed, trying REST API fallback...")

        print("  Strategy: REST API")
        return _rest_upload(display_name, pdf_path, folder)

    def list_documents(self) -> list[dict]:
        """
        Lists documents in the reMarkable cloud (REST API only).
        Returns a list of document metadata dicts.
        """
        device_token = _load_token()
        if not device_token:
            print("No device token. Run setup() first or use rmapi.")
            return []

        try:
            user_token = _get_user_token(device_token)
            resp = requests.get(
                f"{STORAGE_HOST}/document-storage/json/2/docs",
                headers={
                    "Authorization": f"Bearer {user_token}",
                    "User-Agent": USER_AGENT,
                },
                timeout=15,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            print(f"Failed to list documents: {e}")
            return []


# ---------------------------------------------------------------------------
# Quick test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    if len(sys.argv) >= 2 and sys.argv[1] == "setup":
        if len(sys.argv) < 3:
            print("Usage: python rm_uploader.py setup <8-letter-code>")
            print("Get code from: https://my.remarkable.com/device/desktop/connect")
        else:
            RemarkableUploader.setup(sys.argv[2])
        sys.exit(0)

    uploader = RemarkableUploader()

    # Test: upload the generated test planner
    test_pdf = os.path.join(os.path.dirname(__file__), "test_planner.pdf")
    if os.path.exists(test_pdf):
        success = uploader.upload(
            display_name="PolarisFolio Test Planner",
            pdf_path=test_pdf,
            folder="/PolarisFolio",
        )
        print(f"\nResult: {'success' if success else 'failed'}")
    else:
        print(f"Test PDF not found at {test_pdf}")
        print("Run pdf_generator.py first to generate it.")
