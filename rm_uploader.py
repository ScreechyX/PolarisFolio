"""
reMarkable uploader.

Delivers a PDF to the reMarkable device via email (Send to reMarkable).

-- Setup -------------------------------------------------------------------
  1. On your reMarkable device: Settings → Send to reMarkable → note your
     personal @remarkable.com address.
  2. In PolarisFolio Settings, enter:
       - reMarkable email  (e.g. yourname@remarkable.com)
       - SMTP host / port / username / password  (Gmail, Outlook, etc.)

-- Usage -------------------------------------------------------------------
  uploader = RemarkableUploader(settings)
  uploader.upload("My Planner", "/path/to/planner.pdf")
"""

import os
import smtplib
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


class RemarkableUploader:
    """
    Sends a PDF to the reMarkable cloud via the "Send to reMarkable" email feature.
    Requires SMTP credentials and the device's personal @remarkable.com address.
    """

    def __init__(self, settings: dict):
        self.rm_email = settings.get("rm_email", "").strip()
        self.smtp_host = settings.get("smtp_host", "").strip()
        self.smtp_port = int(settings.get("smtp_port", 587) or 587)
        self.smtp_user = settings.get("smtp_user", "").strip()
        self.smtp_pass = settings.get("smtp_pass", "").strip()

    def _configured(self) -> bool:
        return bool(self.rm_email and self.smtp_host and self.smtp_user and self.smtp_pass)

    def upload(self, display_name: str, pdf_path: str, folder: str = None) -> bool:
        if not self._configured():
            print("  Email upload: not configured. Add reMarkable email and SMTP settings.")
            return False

        if not os.path.exists(pdf_path):
            print(f"  Error: file not found - {pdf_path}")
            return False

        size_kb = os.path.getsize(pdf_path) / 1024
        print(f"\nUploading '{display_name}' ({size_kb:.0f} KB) via email...")

        try:
            msg = MIMEMultipart()
            msg["From"] = self.smtp_user
            msg["To"] = self.rm_email
            msg["Subject"] = display_name

            msg.attach(MIMEText("Sent by PolarisFolio.", "plain"))

            filename = os.path.basename(pdf_path)
            with open(pdf_path, "rb") as f:
                part = MIMEApplication(f.read(), Name=filename)
            part["Content-Disposition"] = f'attachment; filename="{filename}"'
            msg.attach(part)

            with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
                server.starttls()
                server.login(self.smtp_user, self.smtp_pass)
                server.sendmail(self.smtp_user, self.rm_email, msg.as_string())

            print(f"  Email: sent '{display_name}' to {self.rm_email}")
            return True

        except Exception as e:
            print(f"  Email upload failed: {e}")
            return False
