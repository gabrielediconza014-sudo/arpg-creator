import os
import re
import ipaddress
import logging
from html import escape
from html.parser import HTMLParser
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
logger = logging.getLogger(__name__)

# Emergent managed email proxy. This is a CONSTANT — never read it from
# os.environ, so it survives deployment.
EMAIL_BASE_URL = "https://integrations.emergentagent.com"
EMAIL_KEY = os.environ.get("EMERGENT_EMAIL_KEY", "")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
RESEND_FROM = os.environ.get("RESEND_FROM", "")
EMAIL_FROM_NAME = os.environ["EMAIL_FROM_NAME"]

_SHORTENERS = ("bit.ly", "tinyurl.com", "t.co", "is.gd", "cutt.ly", "goo.gl", "rebrand.ly")
_CRED_ASK = ("reply with your password", "reply with the code", "send your password", "cvv",
             "send us your password", "enter your password below", "confirm your card number",
             "your full card number", "seed phrase", "recovery phrase", "verify your card",
             "social security number", "confirm your bank details")
_HOSTISH = re.compile(r"\b(?:https?://)?((?:[a-z0-9-]+\.)+[a-z]{2,})", re.I)


def _host_ok(host: str) -> bool:
    if not host or "xn--" in host:
        return False
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass
    return not any(host == s or host.endswith("." + s) for s in _SHORTENERS)


def _same_site(shown: str, real: str) -> bool:
    return shown == real or real.endswith("." + shown) or shown.endswith("." + real)


class _EmailScan(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags, self.urls, self.anchors = set(), [], []
        self._href, self._text = None, []

    def handle_starttag(self, tag, attrs):
        self.tags.add(tag.lower())
        self.urls += [v for k, v in attrs if k.lower() in ("href", "src") and v]
        if tag.lower() == "a":
            self._href = dict((k.lower(), v) for k, v in attrs).get("href")
            self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self._href is not None:
            self.anchors.append((self._href, "".join(self._text)))
            self._href, self._text = None, []


def _assert_safe_email(subject: str, html: str) -> None:
    scan = _EmailScan()
    scan.feed(html)
    if scan.tags & {"form", "input", "textarea", "select"}:
        raise ValueError("No forms or input fields in email (G2)")
    body = f"{subject}\n{html}".lower()
    for p in _CRED_ASK:
        if p in body:
            raise ValueError(f"Email asks the recipient for credentials: {p!r} (G2)")
    for url in scan.urls:
        low = url.strip().lower()
        if low.startswith(("mailto:", "tel:", "cid:", "#")):
            continue
        if not low.startswith("https://"):
            raise ValueError(f"Email links/assets must be absolute https: {url!r} (G3)")
        host = urlparse(low).hostname or ""
        if not _host_ok(host) or urlparse(low).username is not None:
            raise ValueError(f"Shortened, numeric-host or credential-bearing URL: {url!r} (G3)")
    for href, text in scan.anchors:
        real = urlparse(href.strip().lower()).hostname or ""
        if not real:
            continue
        for m in _HOSTISH.finditer(text):
            if not _same_site(m.group(1).lower(), real):
                raise ValueError(f"Anchor text {m.group(1)!r} ≠ real link host {real!r} (G3)")


async def send_email(*, to: str, subject: str, html: str) -> str | None:
    _assert_safe_email(subject, html)
    payload = {"to": [to], "subject": subject, "html": html, "from_name": EMAIL_FROM_NAME}
    if RESEND_API_KEY:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
                json={"from": f"{EMAIL_FROM_NAME} <{RESEND_FROM}>", "to": [to], "subject": subject, "html": html},
            )
        if resp.status_code >= 400:
            logger.error(f"Resend send failed: {resp.status_code} {resp.text}")
            return None
        return resp.json().get("id")
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{EMAIL_BASE_URL}/api/v1/email/send",
                headers={"X-Email-Key": EMAIL_KEY},
                json=payload,
            )
        resp.raise_for_status()
        return resp.json().get("id")
    except httpx.HTTPStatusError as e:
        try:
            code = e.response.json().get("code", "")
        except ValueError:
            code = ""
        if code in ("insufficient_credits", "integration_disabled", "daily_limit_reached"):
            logger.warning(f"Email paused: {code} ({e.response.status_code}): {e.response.json().get('message', '')}")
            return None
        logger.error(f"Email send failed: {e.response.status_code} {e.response.text}")
        return None
    except Exception as e:
        logger.error(f"Email send error: {str(e)}")
        return None


def _app_link(label: str) -> str:
    origin = os.environ.get("APP_ORIGIN", "").strip()
    if not origin:
        return ""
    host = origin.split("//")[-1].strip("/")
    if not host:
        return ""
    return (
        f'<a href="https://{host}/app" style="display:inline-block;background:#FF5722;color:#ffffff;'
        f'text-decoration:none;font-weight:700;font-size:15px;padding:13px 26px;border-radius:999px;">{label}</a>'
    )


def _shell(title_color: str, inner: str) -> str:
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#FFF9F4;padding:32px 12px;">'
        '<tr><td align="center"><table role="presentation" width="560" cellpadding="0" cellspacing="0" '
        'style="background:#ffffff;border-radius:20px;border:2px solid rgba(30,30,47,0.08);overflow:hidden;">'
        '<tr><td style="background:#FF5722;padding:26px 32px;font-family:Arial,sans-serif;">'
        '<span style="color:#ffffff;font-weight:800;font-size:24px;">ARPG</span>'
        '<span style="color:#FFEB3B;font-weight:700;font-size:24px;">.</span>'
        "</td></tr>"
        f'<tr><td style="padding:32px;font-family:Arial,sans-serif;color:#1E1E2F;font-size:15px;line-height:1.6;">{inner}</td></tr>'
        '<tr><td style="padding:18px 32px;font-family:Arial,sans-serif;font-size:12px;color:#5A5A72;'
        'background:#FFF3EC;">Sent by ARPG. We never ask for your password or card details by email.</td></tr>'
        "</table></td></tr></table>"
    )


def welcome_email_html(name: str, is_creator: bool) -> tuple[str, str]:
    """Server-side template only (G4). Returns (subject, html)."""
    safe_name = escape(name or "there")
    link = _app_link("Open ARPG")
    if is_creator:
        subject = "Welcome back, Creator"
        inner = (
            f'<h2 style="margin:0 0 14px;color:#FF5722">Welcome back, Creator!</h2>'
            + f"<p>Hi {safe_name}, welcome back to <strong>ARPG</strong>.</p>"
            + "<p>Your <strong>all-access Creator pass</strong> is active: every feature is unlocked and free for you, forever. "
            + "No payment, no limits — just tell ARPG what to build and it creates realistic builds, full Luau scripts, "
            + "RemoteEvents and whole world edits in Roblox Studio.</p>"
            + (f'<p style="margin:24px 0">{link}</p>' if link else "")
            + "<p style=\"margin:18px 0 0\">See you in Studio,<br/>The ARPG team</p>"
        )
        return subject, _shell("#FF5722", inner)
    subject = "Welcome to ARPG — your free week starts now"
    inner = (
        f'<h2 style="margin:0 0 14px;color:#00BCD4">Welcome to ARPG!</h2>'
        + f"<p>Hi {safe_name}, your account is ready. For the next <strong>7 days you can use ARPG for free</strong>: "
        + "describe what you want — a village, a dungeon, a tycoon with RemoteEvents — and the ARPG architect builds it "
        + "directly in Roblox Studio with the ARPG Builder plugin.</p>"
        + "<p>After your free week, <strong>ARPG Pro costs €0.99 per month</strong>. If you don't subscribe, "
        + "the AI builder pauses until you activate it — your builds and history stay safe.</p>"
        + (f'<p style="margin:24px 0">{link}</p>' if link else "")
        + "<p style=\"margin:18px 0 0\">Happy building,<br/>The ARPG team</p>"
    )
    return subject, _shell("#00BCD4", inner)


async def send_welcome_email(user: dict) -> str | None:
    subject, html = welcome_email_html(user.get("name") or "", bool(user.get("is_creator")))
    email_id = await send_email(to=user["email"], subject=subject, html=html)
    if email_id:
        logger.info(f"Welcome email sent to {user['email']} ({subject})")
    return email_id
