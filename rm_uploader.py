"""
reMarkable uploader — uses rmapi CLI (ddvk fork, sync 1.5).

Install: download rmapi-linux-amd64 from github.com/ddvk/rmapi/releases
and place it on PATH. On first run as the app user, run `rmapi` once to
authenticate with your reMarkable account.
"""

import os
import shutil
import tempfile
import subprocess


def _rmapi_available() -> bool:
    return shutil.which("rmapi") is not None


class RemarkableUploader:

    def __init__(self, settings: dict = None):
        self.folder = (settings or {}).get("rm_folder", "/PolarisFolio").strip()

    def upload(self, display_name: str, pdf_path: str, folder: str = None,
               force: bool = True) -> bool:
        """
        Upload pdf_path to the reMarkable.

        rmapi names the document after the uploaded file's basename, so we stage
        a copy named exactly `display_name` to control how it appears on device.

        `force=True` overwrites a same-named document (used for the manual
        "working" planner). `force=False` never overwrites an existing document
        — used by the daily scheduler so each dated planner is its own doc and
        prior days' handwritten notes are left untouched.
        """
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

        safe_name = (display_name or "PolarisFolio").replace("/", "-").strip()
        with tempfile.TemporaryDirectory() as td:
            staged = os.path.join(td, f"{safe_name}.pdf")
            try:
                shutil.copy2(pdf_path, staged)
            except Exception as e:
                print(f"  staging error: {e}")
                return False

            cmd = ["rmapi", "put"]
            if force:
                cmd.append("--force")
            cmd += [staged, target_folder]
            try:
                result = subprocess.run(cmd, capture_output=True, text=True,
                                        timeout=120, env=env)
                if result.returncode == 0:
                    print(f"  rmapi: uploaded '{safe_name}' to {target_folder}")
                    return True
                err = result.stderr.strip() or result.stdout.strip()
                # Without --force, an existing same-day doc is expected — not fatal
                if not force and "exist" in err.lower():
                    print(f"  rmapi: '{safe_name}' already on device, skipping (notes preserved)")
                    return True
                print(f"  rmapi error: {err}")
                return False
            except subprocess.TimeoutExpired:
                print("  rmapi: upload timed out")
                return False
            except Exception as e:
                print(f"  rmapi: unexpected error - {e}")
                return False
