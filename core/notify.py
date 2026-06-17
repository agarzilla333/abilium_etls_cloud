"""Email the monthly run summary.

Sends via SMTP when SMTP_* env vars are set; otherwise logs the summary (handy
for dry-runs / local). Swap in SendGrid or the Gmail API here if preferred.
"""
from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage
from typing import List

from .runner import RunResult

log = logging.getLogger("notify")


def _body(results: List[RunResult], failures: List[str]) -> str:
    lines = ["Monthly ETL run complete.", ""]
    for r in results:
        lines.append(f"• {r.client_name} — {r.report} ({r.during}): {r.url}")
    if failures:
        lines += ["", "FAILURES:"]
        lines += [f"• {f}" for f in failures]
    return "\n".join(lines)


def send_summary(results: List[RunResult], failures: List[str]) -> None:
    subject = f"Abilium ETL — {len(results)} sheet(s), {len(failures)} failure(s)"
    body = _body(results, failures)

    host = os.environ.get("SMTP_HOST")
    to_addr = os.environ.get("SUMMARY_EMAIL")
    if not host or not to_addr:
        log.info("email not configured; summary follows:\n%s\n%s", subject, body)
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ.get("SMTP_FROM", to_addr)
    msg["To"] = to_addr
    msg.set_content(body)

    with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", "587"))) as s:
        s.starttls()
        user, pwd = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASSWORD")
        if user and pwd:
            s.login(user, pwd)
        s.send_message(msg)
    log.info("summary emailed to %s", to_addr)
