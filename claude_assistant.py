"""
"Ask Claude about a handwritten page."

Pulls the latest page of a reMarkable notebook (named "Claude" by default),
sends the rendered handwriting to Claude's vision API to read and act on it,
then renders the reply to a PDF and uploads it back to the device.

Keyword task-switching: the writer chooses what Claude does by starting the page
with a keyword — answer / summarize / translate / cleanup / expand / explain /
todo (default: answer). Claude detects the keyword from the transcription and
performs that task.

Pieces reused:
  • rm_uploader.RemarkableUploader  — download the notebook + upload the answer
  • rm_notebook.latest_page_image   — newest page → PNG (the new bit)
  • pdf_generator.build_answer_pdf   — render the answer as a planner-style PDF

The orchestration here is synchronous/blocking (network + reportlab); call it
from a thread (see app.py's `asyncio.to_thread` wrapper) so the web server
stays responsive.
"""

import os
import base64
import json
import tempfile
from datetime import datetime, timezone

from rm_uploader import RemarkableUploader
import rm_notebook
from pdf_generator import build_answers_pdf

MODEL = "claude-opus-4-8"

# Keyword tasks the writer can switch into by starting the page with the word.
TASKS = ["answer", "summarize", "translate", "cleanup", "expand", "explain", "todo"]

_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "task": {"type": "string", "enum": TASKS},
        "transcription": {"type": "string"},
        "answer": {"type": "string"},
    },
    "required": ["task", "transcription", "answer"],
    "additionalProperties": False,
}

_SYSTEM_PROMPT = """\
You read a photo of a handwritten note from a reMarkable tablet and respond.

1. Transcribe the handwriting as faithfully as you can into `transcription`.

2. Determine the TASK. The writer selects a task by starting the note with a
   keyword (case-insensitive), optionally followed by ":". Recognised keywords:
   - answer:    answer the question(s) written on the page.
   - summarize: summarise the note concisely.
   - translate: translate the note (to English unless another target language is
                named, e.g. "translate to French").
   - cleanup:   rewrite the note cleanly — fix grammar and spelling, keep the
                meaning and voice.
   - expand:    expand the note into fuller prose; flesh out the ideas.
   - explain:   explain the concept(s) in the note clearly.
   - todo:      turn the note into a structured to-do / action-item list.
   If no keyword is present, default the task to "answer".

3. Put the result in `answer` and set `task` to the detected task. Write only
   the result — do not echo the keyword or add meta-commentary like "Here is...".
   Use plain text: short paragraphs, or simple "- " bullet lines for lists. Do
   not use markdown headings or tables. If the page is blank or unreadable, set
   task to "answer" and say so plainly in `answer`.
"""

_USER_TEXT = ("Here is the latest handwritten page from my Claude notebook. "
              "Read it and respond following your instructions.")


def _image_block(png_path: str) -> dict:
    with open(png_path, "rb") as f:
        data = base64.standard_b64encode(f.read()).decode("utf-8")
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": data},
    }


def _supports_adaptive_thinking(model: str) -> bool:
    """Adaptive thinking is available on the Opus 4.6+/Sonnet 4.6/Fable families
    but not on Haiku or the Claude 3.x models — sending it there 400s."""
    m = (model or "").lower()
    return "haiku" not in m and not m.startswith("claude-3")


def _read_handwriting(png_path: str, api_key: str, model: str = MODEL) -> dict:
    """Send the page image to Claude and return {task, transcription, answer}."""
    import anthropic

    client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
    kwargs = dict(
        model=model,
        max_tokens=8000,
        system=_SYSTEM_PROMPT,
        output_config={"format": {"type": "json_schema", "schema": _RESULT_SCHEMA}},
        messages=[{
            "role": "user",
            "content": [_image_block(png_path), {"type": "text", "text": _USER_TEXT}],
        }],
    )
    if _supports_adaptive_thinking(model):
        kwargs["thinking"] = {"type": "adaptive"}  # helps with messy handwriting
    resp = client.messages.create(**kwargs)
    if resp.stop_reason == "refusal":
        raise RuntimeError("Claude declined to process this page.")

    text = next((b.text for b in resp.content if b.type == "text"), None)
    if not text:
        raise RuntimeError("Claude returned no text response.")
    data = json.loads(text)

    task = data.get("task", "answer")
    if task not in TASKS:
        task = "answer"
    return {
        "task": task,
        "transcription": data.get("transcription", "").strip(),
        "answer": (data.get("answer") or "").strip() or "(no answer)",
    }


def ask_about_latest_page(api_key: str = None, notebook: str = "Claude",
                          folder: str = "/PolarisFolio", pdf_dir: str = None,
                          model: str = MODEL, upload: bool = True,
                          prior_entries: list = None,
                          doc_name: str = None,
                          only_if_changed_from: str = None) -> dict:
    """
    Full round trip: download `notebook`, read its latest page with Claude, then
    rebuild a single running answer log (newest first) and upload it back to
    `folder` under a fixed name (default "<notebook> Answers"), replacing the
    previous version so all answers live in one document.

    `prior_entries` is the list of previously-logged answers (newest first, as
    returned by database.get_claude_answers); the new answer is prepended. The
    caller is responsible for persisting the returned `entry` to the log.

    `only_if_changed_from` enables the auto-watch's change detection:
      - None  → always answer (the manual "Ask Claude now" button).
      - ""    → no baseline yet: record the current page hash but do NOT answer,
                so the watch only fires on *future* writing.
      - hash  → answer only if the latest page differs from this hash.
    When skipped, returns {"skipped": True, "hash": <current>} without an API
    call.

    Returns a dict (task, transcription, answer, created_at, entry, pdf_path,
    display_name, uploaded, hash, skipped). Raises on hard failures (no rmapi,
    download failed, no readable page, API error) so the caller can surface them.
    """
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("No Anthropic API key configured.")

    pdf_dir = pdf_dir or os.path.expanduser("~/polarisfolio_pdfs")
    os.makedirs(pdf_dir, exist_ok=True)
    uploader = RemarkableUploader({"rm_folder": folder})

    with tempfile.TemporaryDirectory() as work:
        archive = uploader.download(notebook, work, folder=folder)
        if not archive:
            raise RuntimeError(
                f"Could not download notebook '{notebook}' from {folder}. "
                f"Check the notebook name and that rmapi is authenticated.")

        page_hash = rm_notebook.latest_page_hash(archive)
        # Change-detection gate for the auto-watch.
        if only_if_changed_from is not None and (
                only_if_changed_from == "" or page_hash == only_if_changed_from):
            return {"skipped": True, "hash": page_hash, "task": None,
                    "transcription": "", "answer": "", "uploaded": False}

        png_path = os.path.join(work, "latest_page.png")
        rm_notebook.latest_page_image(archive, png_path)

        result = _read_handwriting(png_path, api_key, model=model)

    gen = datetime.now(timezone.utc)
    entry = {
        "created_at": gen.isoformat(),
        "task": result["task"],
        "transcription": result["transcription"],
        "answer": result["answer"],
    }

    # Newest first: this answer, then everything already logged.
    entries = [entry] + list(prior_entries or [])
    display_name = doc_name or f"{notebook} Answers"
    # One stable local file — the combined log is rebuilt and overwritten each run.
    pdf_path = os.path.join(pdf_dir, "claude_answers.pdf")
    build_answers_pdf(entries, pdf_path, title=notebook)

    uploaded = False
    if upload:
        # force=True replaces the single answer-log document in place.
        uploaded = uploader.upload(display_name, pdf_path, folder=folder, force=True)

    return {
        **result,
        "created_at": gen.isoformat(),
        "entry": entry,
        "pdf_path": pdf_path,
        "display_name": display_name,
        "uploaded": uploaded,
        "hash": page_hash,
        "skipped": False,
    }
