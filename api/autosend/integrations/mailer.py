"""
integrations/mailer.py

Outbound transactional email (currently just signup email verification -
see web/signup_router.py and web/account_router.py). Platform-wide SMTP
credentials (Mailtrap), not per-organisation - see storage/schema.py's
platform_email_settings table docstring for why this mirrors
meta_platform_settings rather than being another per-org credential.
Reads credentials fresh from the DB on every send rather than caching,
unlike clients.py's WhatsApp/PCO client registry - transactional email
here is low-volume enough that the per-send DB read is cheap, and it means
an edited credential takes effect immediately, no app restart needed.
"""

from __future__ import annotations

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import urlsplit

from autosend import storage


class MailerNotConfigured(Exception):
    pass


# Brand colour/font match signup.html and sqladmin/layout.html - kept as a
# plain string constant rather than importing anything from the Jinja
# templates, since this has to render standalone in an email client, not
# through the app's own template engine.
_BRAND_PRIMARY = "#06B6D4"


def render_verification_email(verify_url: str, *, welcome: bool = False) -> tuple[str, str]:
    """Returns (text_body, html_body) for the signup/resend verification
    email - one shared template so web/signup_router.py's initial send and
    web/account_router.py's resend endpoint can't drift out of sync with
    each other. `welcome` distinguishes the first-signup wording from a
    plain resend; the link/expiry/button are identical either way.
    logo_url is derived from verify_url's own scheme+host rather than
    hardcoded, so this renders correctly whether it's sent from
    dev.kryx.co.za or kryx.co.za."""
    parts = urlsplit(verify_url)
    logo_url = f"{parts.scheme}://{parts.netloc}/static/logo_web.png"
    intro = (
        "Welcome to Kryx! Please confirm your email address to finish setting "
        "up your organisation."
        if welcome
        else "Please confirm your email address."
    )

    text_body = (
        f"{intro}\n\n"
        f"Verify your email address:\n{verify_url}\n\n"
        "This link expires in 24 hours. You can keep using Kryx while "
        "unverified, but your organisation can't be activated until you "
        "confirm your address."
    )

    html_body = f"""\
<!DOCTYPE html>
<html>
  <body style="margin:0;padding:0;background-color:#f1f5f9;font-family:'Inter',Arial,sans-serif;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#f1f5f9;padding:32px 16px;">
      <tr>
        <td align="center">
          <table role="presentation" width="100%" style="max-width:480px;background-color:#ffffff;border-radius:12px;border:1px solid #e2e8f0;border-top:3px solid {_BRAND_PRIMARY};overflow:hidden;">
            <tr>
              <td style="padding:32px 32px 8px 32px;text-align:center;">
                <img src="{logo_url}" alt="Kryx" style="height:56px;width:auto;margin-bottom:16px;">
                <h1 style="margin:0;font-size:20px;font-weight:700;color:#0f172a;">Confirm your email address</h1>
              </td>
            </tr>
            <tr>
              <td style="padding:8px 32px 24px 32px;text-align:center;">
                <p style="margin:0 0 24px 0;font-size:14px;line-height:1.6;color:#475569;">{intro}</p>
                <a href="{verify_url}" style="display:inline-block;background-color:{_BRAND_PRIMARY};color:#ffffff;text-decoration:none;font-weight:600;font-size:14px;padding:12px 28px;border-radius:10px;">Verify Email Address</a>
                <p style="margin:24px 0 0 0;font-size:12px;color:#94a3b8;">This link expires in 24 hours.</p>
              </td>
            </tr>
            <tr>
              <td style="padding:16px 32px;background-color:#f8fafc;border-top:1px solid #e2e8f0;text-align:center;">
                <p style="margin:0;font-size:11px;color:#94a3b8;">
                  If the button doesn't work, copy and paste this link:<br>
                  <a href="{verify_url}" style="color:{_BRAND_PRIMARY};word-break:break-all;">{verify_url}</a>
                </p>
              </td>
            </tr>
          </table>
          <p style="margin:16px 0 0 0;font-size:11px;color:#94a3b8;">Powered by Kryx Automation</p>
        </td>
      </tr>
    </table>
  </body>
</html>
"""
    return text_body, html_body


def send_email(to_address: str, subject: str, text_body: str, html_body: str | None = None) -> None:
    settings = storage.get_platform_email_settings()
    if not settings:
        raise MailerNotConfigured("platform_email_settings has no row configured yet")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings["from_address"]
    msg["To"] = to_address
    msg.attach(MIMEText(text_body, "plain"))
    if html_body:
        msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP(settings["smtp_host"], settings["smtp_port"], timeout=10) as smtp:
        smtp.starttls()
        if settings["smtp_username"]:
            smtp.login(settings["smtp_username"], settings["smtp_password"])
        smtp.send_message(msg)
