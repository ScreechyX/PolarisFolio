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
from pdf_generator import build_answer_pdf

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


def _read_handwriting(png_path: str, api_key: str, model: str = MODEL) -> dict:
    """Send the page image to Claude and return {task, transcription, answer}."""
    import anthropic

    client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
    resp = client.messages.create(
        model=model,
        max_tokens=8000,
        thinking={"type": "adaptive"},  # messy handwriting + task detection
        system=_SYSTEM_PROMPT,
        output_config={"format": {"type": "json_schema", "schema": _RESULT_SCHEMA}},
        messages=[{
            "role": "user",
            "content": [_image_block(png_path), {"type": "text", "text": _USER_TEXT}],
        }],
    )
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
                          model: str = MODEL, upload: bool = True) -> dict:
    """
    Full round trip: download `notebook`, read its latest page with Claude,
    render the reply to a PDF, and (optionally) upload that PDF back to `folder`.

    Returns a dict describing the result (task, transcription, answer, pdf_path,
    display_name, uploaded). Raises on hard failures (no rmapi, download failed,
    no readable page, API error) so the caller can surface them.
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

        png_path = os.path.join(work, "latest_page.png")
        rm_notebook.latest_page_image(archive, png_path)

        result = _read_handwriting(png_path, api_key, model=model)

    gen = datetime.now(timezone.utc)
    display_name = f"{notebook} Answer {gen:%Y-%m-%d %H%M}"
    pdf_path = os.path.join(
        pdf_dir, f"claude_answer_{gen:%Y%m%d_%H%M%S}.pdf")
    build_answer_pdf(
        result["answer"], pdf_path, task=result["task"],
        transcription=result["transcription"], title=notebook, generated_at=gen)

    uploaded = False
    if upload:
        uploaded = uploader.upload(display_name, pdf_path, folder=folder, force=True)

    return {
        **result,
        "pdf_path": pdf_path,
        "display_name": display_name,
        "uploaded": uploaded,
    }
