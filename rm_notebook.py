"""
reMarkable notebook → page image.

This is the genuinely new piece for the "ask Claude about a handwritten page"
feature: given a document bundle pulled off the device by
`RemarkableUploader.download` (a `.rmdoc`/`.zip` archive, or an unpacked
directory), find the *latest* page and render its handwriting to a PNG that can
be sent to Claude's vision API.

A reMarkable notebook page is stored as a reMarkable-lines (`.rm`) file, not an
image, so rendering needs a `.rm` reader. We use the `rmc` CLI (from the
`rmscene` ecosystem) to convert the page to SVG, then rasterise the SVG to PNG.

Install the renderers once on the host:

    pip install rmc            # provides the `rmc` CLI + rmscene
    pip install cairosvg       # SVG → PNG  (preferred; needs libcairo)
    # or, if cairo is unavailable:
    pip install svglib         # pure-Python SVG → PNG via reportlab

If neither rasteriser is installed we fall back to handing the SVG straight to
Claude as text is not possible (vision needs a bitmap), so a clear RuntimeError
is raised naming the missing dependency.
"""

import os
import json
import glob
import zipfile
import tempfile
import subprocess
import shutil


class NotebookError(RuntimeError):
    """Raised when a notebook can't be unpacked, parsed, or rendered."""


# ─────────────────────────────────────────────────────────────────────────────
# Unpacking
# ─────────────────────────────────────────────────────────────────────────────

def _unpack(archive_path: str, work_dir: str) -> str:
    """Unpack a .rmdoc/.zip into work_dir (or copy a directory). Returns the
    directory that holds the document's files."""
    if os.path.isdir(archive_path):
        return archive_path
    if zipfile.is_zipfile(archive_path):
        with zipfile.ZipFile(archive_path) as z:
            z.extractall(work_dir)
        return work_dir
    raise NotebookError(f"unrecognised notebook archive: {archive_path}")


def _find_content_file(root: str) -> str:
    """Locate the document's `<uuid>.content` JSON within `root` (searched
    recursively — some archives nest the bundle a level down)."""
    matches = glob.glob(os.path.join(root, "**", "*.content"), recursive=True)
    if not matches:
        raise NotebookError("no .content file in notebook archive")
    # The top-level document content sits highest in the tree (fewest path
    # separators); sub-documents, if any, are deeper.
    matches.sort(key=lambda p: p.count(os.sep))
    return matches[0]


# ─────────────────────────────────────────────────────────────────────────────
# Page selection
# ─────────────────────────────────────────────────────────────────────────────

def _latest_page_id(content_path: str) -> str:
    """Return the UUID of the last page in reading order.

    Handles both the v6 `cPages.pages` shape (list of {"id": ...} with optional
    `deleted` markers) and the older flat `pages` list of UUID strings.
    """
    try:
        with open(content_path) as f:
            content = json.load(f)
    except (OSError, ValueError) as e:
        raise NotebookError(f"could not read {content_path}: {e}")

    # v6: {"cPages": {"pages": [{"id": "...", "deleted": {...}?}, ...]}}
    cpages = (content.get("cPages") or {}).get("pages")
    if cpages:
        live = [p for p in cpages if isinstance(p, dict) and p.get("id")
                and "deleted" not in p]
        if not live:
            live = [p for p in cpages if isinstance(p, dict) and p.get("id")]
        if live:
            return live[-1]["id"]

    # Older: {"pages": ["uuid", "uuid", ...]}
    pages = content.get("pages")
    if pages:
        return pages[-1]

    raise NotebookError("notebook has no pages")


def _page_rm_file(content_path: str, page_id: str) -> str:
    """Path to a page's `.rm` file: `<doc-uuid>/<page-uuid>.rm` beside the
    `.content` file."""
    base = content_path[: -len(".content")]          # strip extension → doc dir
    rm = os.path.join(base, f"{page_id}.rm")
    if os.path.exists(rm):
        return rm
    # Fallback: search the bundle for the page file by name.
    hits = glob.glob(os.path.join(os.path.dirname(content_path), "**",
                                  f"{page_id}.rm"), recursive=True)
    if hits:
        return hits[0]
    raise NotebookError(f"page file {page_id}.rm not found in archive")


# ─────────────────────────────────────────────────────────────────────────────
# Rendering
# ─────────────────────────────────────────────────────────────────────────────

def _rm_to_svg(rm_path: str, svg_path: str) -> None:
    if shutil.which("rmc") is None:
        raise NotebookError(
            "the 'rmc' renderer is not installed — run `pip install rmc` "
            "to convert reMarkable .rm pages to images")
    try:
        result = subprocess.run(["rmc", "-t", "svg", "-o", svg_path, rm_path],
                                capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        raise NotebookError("rmc: rendering timed out")
    if result.returncode != 0 or not os.path.exists(svg_path):
        err = result.stderr.strip() or result.stdout.strip()
        raise NotebookError(f"rmc failed to render page: {err}")


def _svg_to_png(svg_path: str, png_path: str, scale: float = 2.0) -> None:
    """Rasterise SVG → PNG. Prefer cairosvg; fall back to svglib+reportlab."""
    try:
        import cairosvg
        cairosvg.svg2png(url=svg_path, write_to=png_path, scale=scale,
                         background_color="white")
        return
    except ImportError:
        pass

    try:
        from svglib.svglib import svg2rlg
        from reportlab.graphics import renderPM
    except ImportError:
        raise NotebookError(
            "no SVG rasteriser available — install one with "
            "`pip install cairosvg` (preferred) or `pip install svglib`")

    drawing = svg2rlg(svg_path)
    if drawing is None:
        raise NotebookError("svglib could not parse the rendered SVG")
    renderPM.drawToFile(drawing, png_path, fmt="PNG", dpi=int(72 * scale),
                        bg=0xFFFFFF)


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def latest_page_image(archive_path: str, out_png: str) -> str:
    """
    Render the latest page of a downloaded reMarkable document to `out_png`.

    `archive_path` is the `.rmdoc`/`.zip` (or unpacked directory) produced by
    `RemarkableUploader.download`. Returns `out_png` on success; raises
    NotebookError if the bundle can't be unpacked, has no pages, or the
    renderers aren't installed.
    """
    with tempfile.TemporaryDirectory() as work:
        root = _unpack(archive_path, work)
        content_path = _find_content_file(root)
        page_id = _latest_page_id(content_path)
        rm_path = _page_rm_file(content_path, page_id)

        svg_path = os.path.join(work, "page.svg")
        _rm_to_svg(rm_path, svg_path)
        os.makedirs(os.path.dirname(os.path.abspath(out_png)), exist_ok=True)
        _svg_to_png(svg_path, out_png)
    return out_png
