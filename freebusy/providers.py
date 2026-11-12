"""Which provider an address probably belongs to, and the servers to try.

A guess from the domain, never a decision: the owner can name another provider
or give their own servers during ``/freebusy connect``. Custom domains (your
own domain hosted at iCloud or Fastmail) cannot be told apart by name, so they
get the generic guess plus a ``/.well-known/caldav`` probe of the domain.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Provider:
    key: str
    name: str
    domains: list[str] = field(default_factory=list)
    caldav: Optional[str] = None            # base URL; None = no CalDAV connector for it in this version
    imap: Optional[tuple[str, int]] = None
    smtp: Optional[tuple[str, int]] = None
    app_passwords: str = ""                 # where the owner makes an app password
    note: str = ""                          # what is not available yet, said plainly


PROVIDERS = {
    "icloud": Provider("icloud", "iCloud", ["icloud.com", "me.com", "mac.com"], "https://caldav.icloud.com/",
                       ("imap.mail.me.com", 993), ("smtp.mail.me.com", 587),
                       "account.apple.com, Sign-In and Security, App-Specific Passwords"),
    "fastmail": Provider("fastmail", "Fastmail", ["fastmail.com", "fastmail.fm", "messagingengine.com"], "https://caldav.fastmail.com/",
                         ("imap.fastmail.com", 993), ("smtp.fastmail.com", 465),
                         "Fastmail Settings, Privacy & Security, Manage app passwords (give it calendars and mail)"),
    "zoho": Provider("zoho", "Zoho", ["zoho.com", "zohomail.com", "zoho.eu"], "https://calendar.zoho.com/",
                     ("imap.zoho.com", 993), ("smtp.zoho.com", 465),
                     "Zoho Accounts, Security, App Passwords"),
    "nextcloud": Provider("nextcloud", "Nextcloud", [], None, None, None,
                          "Nextcloud Settings, Security, Devices & sessions, Create new app password",
                          "Give your Nextcloud address: /freebusy connect server caldav https://cloud.example.com/remote.php/dav/"),
    "gmail": Provider("gmail", "Google", ["gmail.com", "googlemail.com"], None, ("imap.gmail.com", 993), ("smtp.gmail.com", 465),
                      "myaccount.google.com/apppasswords (needs 2-Step Verification)",
                      "Gmail mail works now over IMAP with an app password. Google Calendar and Google sign-in come in 0.4."),
    "microsoft": Provider("microsoft", "Microsoft", ["outlook.com", "hotmail.com", "live.com", "msn.com"], None, None, None, "",
                          "Microsoft no longer accepts app passwords for Outlook.com mail, and its calendar has no CalDAV. "
                          "Microsoft Graph (calendar and mail, with your own app registration) comes in 0.4. You can still "
                          "connect another calendar here and keep holds in the local feed."),
}


def domain_of(address: str) -> str:
    return address.rsplit("@", 1)[-1].strip().lower()


def guess(address: str) -> Provider:
    d = domain_of(address)
    for p in PROVIDERS.values():
        if d in p.domains:
            return p
    return Provider("other", d, [d], f"https://{d}/", (f"imap.{d}", 993), (f"smtp.{d}", 465), "your provider's security settings",
                    "Your own domain: if it is hosted at iCloud, Fastmail or Zoho, reply provider icloud (or fastmail, zoho).")
