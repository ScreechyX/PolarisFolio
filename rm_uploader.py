"""
reMarkable uploader — uses rmapi CLI (ddvk fork, sync 1.5).

Install: download rmapi-linux-amd64 from github.com/ddvk/rmapi/releases
and place it on PATH. On first run as the app user, run `rmapi` once to
authenticate with your reMarkable account.
"""

import os
import shutil
import subprocess


def _rmapi_available() -> bool:
    return shutil.which("rmapi") is not None


class RemarkableUploader:

    def __init__(self, settings: dict = None):
        self.folder = (settings or {}).get("rm_folder", "/PolarisFolio").strip()

    def upload(self, display_name: str, pdf_path: str, folder: str = None) -> bool:
        if not _rmapi_available():
            print("  rmapi not found on PATH.")
            return False

        if not os.path.exists(pdf_path):
            print(f"  Error: file not found - {pdf_path}")
            return False

        target_folder = (folder or self.folder or "/PolarisFolio").rstrip("/")
        size_kb = os.path.getsize(pdf_path) / 1024
        print(f"\nUploading '{display_name}' ({size_kb:.0f} KB) via rmapi to {target_folder}...")

        home = os.path.expanduser("~")
        env = {**os.environ, "HOME": home, "XDG_CONFIG_HOME": os.path.join(home, ".config")}

        # Ensure folder exists
        try:
            subprocess.run(["rmapi", "mkdir", target_folder],
                           capture_output=True, text=True, timeout=30, env=env)
        except Exception:
            pass

        try:
            result = subprocess.run(
                ["rmapi", "put", "--force", pdf_path, target_folder],
                capture_output=True, text=True, timeout=60, env=env,
            )
            if result.returncode == 0:
                print(f"  rmapi: uploaded '{display_name}' to {target_folder}")
                return True
            else:
                print(f"  rmapi error: {result.stderr.strip() or result.stdout.strip()}")
                return False
        except subprocess.TimeoutExpired:
            print("  rmapi: upload timed out")
            return False
        except Exception as e:
            print(f"  rmapi: unexpected error - {e}")
            return False
