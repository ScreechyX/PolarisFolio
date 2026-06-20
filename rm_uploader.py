"""
reMarkable uploader — uses rmapi CLI (ddvk fork, sync 1.5).

Install: download rmapi-linux-amd64 from github.com/ddvk/rmapi/releases
and place it on PATH. On first run as the app user, run `rmapi` once to
authenticate with your reMarkable account.
"""

import os
import re
import shutil
import tempfile
import subprocess

# Dated auto-planner docs are named "PolarisFolio YYYY-MM-DD" (see scheduler.py).
_DATED_DOC_RE = re.compile(r"^PolarisFolio (\d{4}-\d{2}-\d{2})$")


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

    def update_in_place(self, display_name: str, pdf_path: str,
                        folder: str = None, content_only: bool = True) -> bool:
        """
        Replace the PDF of the persistent yearly planner *in place*, preserving
        the document ID and every handwritten annotation layer.

        `content_only=True`  → `rmapi put --content-only` swaps only the PDF on an
                               existing same-named document, keeping all ink. Use
                               when the page geometry is unchanged.
        `content_only=False` → `rmapi put --force` deletes and recreates the
                               document (new ID, ink dropped). Use for the very
                               first upload, or when the page geometry changed
                               (see pdf_generator.yearly_geometry_signature) and
                               an in-place swap would misalign annotations.

        Returns True on success.
        """
        if not _rmapi_available():
            print("  rmapi not found on PATH.")
            return False
        if not os.path.exists(pdf_path):
            print(f"  Error: file not found - {pdf_path}")
            return False

        target_folder = (folder or self.folder or "/PolarisFolio").rstrip("/")
        home = os.path.expanduser("~")
        env = {**os.environ, "HOME": home, "XDG_CONFIG_HOME": os.path.join(home, ".config")}

        try:
            subprocess.run(["rmapi", "mkdir", target_folder],
                           capture_output=True, text=True, timeout=30, env=env)
        except Exception:
            pass

        safe_name = (display_name or "PolarisFolio").replace("/", "-").strip()
        mode = "in place (notes preserved)" if content_only else "recreate"
        size_kb = os.path.getsize(pdf_path) / 1024
        print(f"\nSyncing '{safe_name}' ({size_kb:.0f} KB) to {target_folder} — {mode}...")

        with tempfile.TemporaryDirectory() as td:
            staged = os.path.join(td, f"{safe_name}.pdf")
            try:
                shutil.copy2(pdf_path, staged)
            except Exception as e:
                print(f"  staging error: {e}")
                return False

            flag = "--content-only" if content_only else "--force"
            cmd = ["rmapi", "put", flag, staged, target_folder]
            try:
                result = subprocess.run(cmd, capture_output=True, text=True,
                                        timeout=180, env=env)
            except subprocess.TimeoutExpired:
                print("  rmapi: sync timed out")
                return False
            except Exception as e:
                print(f"  rmapi: unexpected error - {e}")
                return False

            if result.returncode == 0:
                print(f"  rmapi: synced '{safe_name}' ({mode})")
                return True

            err = (result.stderr.strip() or result.stdout.strip())
            # --content-only requires the document to already exist; if it does
            # not yet (or rmapi can't match it), fall back to creating it once.
            if content_only and ("not found" in err.lower()
                                 or "exist" in err.lower()
                                 or "no such" in err.lower()):
                print(f"  rmapi: no existing doc to update, creating it — {err}")
                return self.update_in_place(display_name, pdf_path, folder,
                                            content_only=False)
            print(f"  rmapi error: {err}")
            return False

    def download(self, doc_name: str, dest_dir: str, folder: str = None) -> str:
        """
        Download a document from the reMarkable into `dest_dir` via `rmapi get`.

        Authentication is the same rmapi token the rest of this class already
        uses — rmapi's cloud-token auth (stored under ~/.config/rmapi after the
        one-time `rmapi` login) covers `get` just as it covers `put`/`ls`/`rm`,
        so there is no separate REST path to add for downloads.

        `rmapi get` writes an archive named after the document — a `.rmdoc`
        (a zip, on the ddvk sync-1.5 fork) or a plain `.zip` on older builds —
        into the working directory. We run it inside `dest_dir` and return the
        path to whatever archive it produced, or None on failure. The archive
        is the raw document bundle (`.content`, `.metadata`, and the per-page
        `.rm` handwriting files); see rm_notebook.latest_page_image to turn its
        newest page into an image.
        """
        if not _rmapi_available():
            print("  rmapi not found on PATH.")
            return None

        target_folder = (folder or self.folder or "/PolarisFolio").rstrip("/")
        doc_path = f"{target_folder}/{doc_name}"
        home = os.path.expanduser("~")
        env = {**os.environ, "HOME": home, "XDG_CONFIG_HOME": os.path.join(home, ".config")}

        os.makedirs(dest_dir, exist_ok=True)
        before = set(os.listdir(dest_dir))

        print(f"\nDownloading '{doc_path}' via rmapi to {dest_dir}...")
        try:
            result = subprocess.run(["rmapi", "get", doc_path],
                                    capture_output=True, text=True, timeout=120,
                                    env=env, cwd=dest_dir)
        except subprocess.TimeoutExpired:
            print("  rmapi: download timed out")
            return None
        except Exception as e:
            print(f"  rmapi: unexpected error - {e}")
            return None

        if result.returncode != 0:
            err = result.stderr.strip() or result.stdout.strip()
            print(f"  rmapi get error: {err}")
            return None

        # rmapi names the output after the document; find whatever it created.
        new = [f for f in os.listdir(dest_dir) if f not in before]
        archives = [f for f in new if f.lower().endswith((".rmdoc", ".zip"))]
        if not archives:
            # Fall back to any new entry (some builds drop a directory).
            archives = new
        if not archives:
            print("  rmapi get produced no output file")
            return None
        # Prefer an exact name match, else the most recently modified new entry.
        path = os.path.join(dest_dir, f"{doc_name}.rmdoc")
        if not os.path.exists(path):
            path = os.path.join(dest_dir, f"{doc_name}.zip")
        if not os.path.exists(path):
            archives.sort(key=lambda f: os.path.getmtime(os.path.join(dest_dir, f)),
                          reverse=True)
            path = os.path.join(dest_dir, archives[0])
        print(f"  rmapi: downloaded to {os.path.basename(path)}")
        return path

    def prune_old_dated(self, keep: int = 5, folder: str = None) -> int:
        """
        Delete old dated auto-planner docs, keeping only the `keep` most recent.

        Targets only documents named "PolarisFolio YYYY-MM-DD" in `folder`, so
        the manually generated working planner and any other files are left
        alone. Returns the number of documents removed.
        """
        if keep < 0 or not _rmapi_available():
            return 0

        target_folder = (folder or self.folder or "/PolarisFolio").rstrip("/")
        home = os.path.expanduser("~")
        env = {**os.environ, "HOME": home, "XDG_CONFIG_HOME": os.path.join(home, ".config")}

        try:
            result = subprocess.run(["rmapi", "ls", target_folder],
                                    capture_output=True, text=True, timeout=30, env=env)
        except Exception as e:
            print(f"  rmapi: prune list error - {e}")
            return 0
        if result.returncode != 0:
            return 0

        dated = []  # (date_str, doc_name)
        for line in result.stdout.splitlines():
            # rmapi ls lines look like "[f]\tPolarisFolio 2026-06-17"
            name = line.split("\t", 1)[-1].strip() if "\t" in line else line.strip()
            name = re.sub(r"^\[[df]\]\s*", "", name).strip()
            m = _DATED_DOC_RE.match(name)
            if m:
                dated.append((m.group(1), name))

        # Newest first; keep the first `keep`, delete the rest.
        dated.sort(reverse=True)
        to_delete = dated[keep:]
        removed = 0
        for _, name in to_delete:
            doc_path = f"{target_folder}/{name}"
            try:
                rm = subprocess.run(["rmapi", "rm", doc_path],
                                    capture_output=True, text=True, timeout=30, env=env)
                if rm.returncode == 0:
                    print(f"  rmapi: pruned old planner '{name}'")
                    removed += 1
                else:
                    err = rm.stderr.strip() or rm.stdout.strip()
                    print(f"  rmapi: could not prune '{name}' - {err}")
            except Exception as e:
                print(f"  rmapi: prune error on '{name}' - {e}")
        return removed
