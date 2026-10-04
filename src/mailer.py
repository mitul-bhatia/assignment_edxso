"""Email transports. The sender depends on this small interface, not on SMTP.

A transport either delivers, raises DefiniteFailure (the message provably did not
leave: bad credentials, refused recipient, no connection) or raises UncertainFailure
(it may have been accepted, e.g. a timeout after DATA). The distinction matters:
only a definite failure is safe to retry without risking a duplicate.
"""

from __future__ import annotations

import os
import smtplib
import socket
import ssl
from email.message import EmailMessage
from email.utils import make_msgid

from .common import load_env


class DefiniteFailure(RuntimeError):
    """Not delivered, certainly. Safe to retry."""


class UncertainFailure(RuntimeError):
    """Delivery state unknown. Must be checked by a person before any retry."""


class DryRunTransport:
    name = "dry_run"

    def send(self, to: str, subject: str, body: str, *, reply_to: str | None = None) -> str | None:
        return None  # nothing leaves the machine


class SMTPTransport:
    """Plain SMTP with STARTTLS. Works with Gmail (app password), Outlook, SES, Mailgun..."""

    name = "smtp"

    def __init__(self) -> None:
        load_env()
        self.host = os.environ.get("SMTP_HOST", "")
        self.port = int(os.environ.get("SMTP_PORT", "587"))
        self.user = os.environ.get("SMTP_USER", "")
        self.password = os.environ.get("SMTP_PASSWORD", "")
        self.sender = os.environ.get("SMTP_FROM", self.user)
        if not (self.host and self.user and self.password and self.sender):
            raise DefiniteFailure("SMTP is not configured (SMTP_HOST, SMTP_USER, SMTP_PASSWORD, SMTP_FROM)")

    def build(self, to: str, subject: str, body: str, reply_to: str | None) -> EmailMessage:
        message = EmailMessage()
        message["From"], message["To"], message["Subject"] = self.sender, to, subject
        message["Message-ID"] = make_msgid(domain=self.sender.rsplit("@", 1)[-1])
        message["List-Unsubscribe"] = f"<mailto:{self.sender}?subject=unsubscribe>"
        if reply_to:
            message["Reply-To"] = reply_to
        message.set_content(body)
        return message

    def send(self, to: str, subject: str, body: str, *, reply_to: str | None = None) -> str | None:
        message = self.build(to, subject, body, reply_to)
        try:
            with smtplib.SMTP(self.host, self.port, timeout=30) as smtp:
                smtp.ehlo()
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
                smtp.login(self.user, self.password)
                smtp.send_message(message)
        except (smtplib.SMTPAuthenticationError, smtplib.SMTPConnectError, smtplib.SMTPHeloError,
                smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError,
                ConnectionRefusedError, socket.gaierror) as exc:
            raise DefiniteFailure(f"{type(exc).__name__}: {exc}"[:300]) from None
        except (smtplib.SMTPException, TimeoutError, OSError) as exc:
            raise UncertainFailure(f"{type(exc).__name__}: {exc}"[:300]) from None
        return message["Message-ID"]
