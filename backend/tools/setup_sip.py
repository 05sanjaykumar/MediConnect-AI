#!/usr/bin/env python
"""Set up SIP registration on the Twilio account, so a free softphone can call
the agent over WiFi instead of over the telephone network.

Why this exists: the number we own is American, so dialling it from India is an
international call the caller pays for. A SIP call never touches the telephone
network — it is data over WiFi, free to the caller, and billed to the Twilio
balance at a fraction of a PSTN minute. It also needs no Indian number, which
Twilio will not sell this account anyway.

Creates, if they don't already exist:
  * a SIP domain with registration enabled, pointed at the same TwiML webhook
    a phone call uses — the agent cannot tell the difference
  * a credential list, and one credential per caller
  * the two mappings Twilio needs: one to let the credential register, one to
    let it place calls

Run it again after the ngrok URL changes; it updates the domain in place.
Credentials are written to backend/.env, never printed.

    python tools/setup_sip.py                 # set up / update
    python tools/setup_sip.py --add 9840012347  # add another caller
    python tools/setup_sip.py --show          # what exists right now
"""

import argparse
import os
import pathlib
import re
import secrets
import string
import sys

from dotenv import load_dotenv
from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client

BACKEND = pathlib.Path(__file__).resolve().parents[1]
ENV = BACKEND / ".env"
load_dotenv(ENV)

ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
PUBLIC_HOST = (os.getenv("PUBLIC_HOST") or "").replace("https://", "").rstrip("/")

CREDENTIAL_LIST_NAME = "MediConnect callers"


def client() -> Client:
    if not (ACCOUNT_SID and AUTH_TOKEN):
        sys.exit("TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN missing from backend/.env")
    return Client(ACCOUNT_SID, AUTH_TOKEN)


def strong_password() -> str:
    """Twilio wants 12+ characters with upper, lower and a digit."""
    alphabet = string.ascii_letters + string.digits
    while True:
        candidate = "".join(secrets.choice(alphabet) for _ in range(16))
        if (any(c.islower() for c in candidate) and any(c.isupper() for c in candidate)
                and any(c.isdigit() for c in candidate)):
            return candidate


def write_env(**values) -> None:
    """Record settings in backend/.env — gitignored, and never printed."""
    text = ENV.read_text() if ENV.exists() else ""
    for key, value in values.items():
        line = f"{key}={value}"
        if re.search(rf"^{key}=.*$", text, flags=re.M):
            text = re.sub(rf"^{key}=.*$", line, text, flags=re.M)
        else:
            if not text.endswith("\n"):
                text += "\n"
            text += line + "\n"
    ENV.write_text(text)


def find_domain(c: Client):
    for d in c.sip.domains.list(limit=20):
        if d.friendly_name == "MediConnect AI":
            return d
    return None


def find_credential_list(c: Client):
    for cl in c.sip.credential_lists.list(limit=20):
        if cl.friendly_name == CREDENTIAL_LIST_NAME:
            return cl
    return None


def show(c: Client) -> int:
    domains = c.sip.domains.list(limit=20)
    if not domains:
        print("No SIP domains on this account.")
        return 0
    for d in domains:
        print(f"domain      : {d.domain_name}")
        print(f"  friendly  : {d.friendly_name}")
        print(f"  voice_url : {d.voice_url}")
        print(f"  register  : {d.sip_registration}")
    for cl in c.sip.credential_lists.list(limit=20):
        creds = c.sip.credential_lists(cl.sid).credentials.list(limit=50)
        print(f"credentials : {cl.friendly_name} -> {[x.username for x in creds]}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--add", metavar="USERNAME",
                        help="add another caller; use a patient's phone number "
                             "so the agent greets them by name")
    parser.add_argument("--show", action="store_true")
    args = parser.parse_args()

    c = client()
    if args.show:
        return show(c)

    if not PUBLIC_HOST:
        sys.exit("PUBLIC_HOST is empty in backend/.env — start ngrok first.")
    voice_url = f"https://{PUBLIC_HOST}/api/twilio/voice"

    # ---- credential list ------------------------------------------------
    cred_list = find_credential_list(c)
    if cred_list is None:
        cred_list = c.sip.credential_lists.create(friendly_name=CREDENTIAL_LIST_NAME)
        print(f"created credential list {cred_list.sid}")
    else:
        print(f"credential list already exists ({cred_list.sid})")

    if args.add:
        password = strong_password()
        c.sip.credential_lists(cred_list.sid).credentials.create(
            username=args.add, password=password)
        write_env(**{f"SIP_PASSWORD_{args.add}": password})
        print(f"added caller {args.add}; password written to backend/.env "
              f"as SIP_PASSWORD_{args.add}")
        return 0

    # ---- domain ---------------------------------------------------------
    domain = find_domain(c)
    if domain is None:
        # Must be globally unique across all of Twilio.
        base = f"mediconnect-{secrets.token_hex(3)}"
        try:
            domain = c.sip.domains.create(
                domain_name=f"{base}.sip.twilio.com",
                friendly_name="MediConnect AI",
                voice_url=voice_url,
                voice_method="POST",
                sip_registration=True,
            )
        except TwilioRestException as exc:
            sys.exit(f"Twilio refused to create the SIP domain [{exc.code}]: {exc.msg}")
        print(f"created SIP domain {domain.domain_name}")
    else:
        domain = c.sip.domains(domain.sid).update(
            voice_url=voice_url, voice_method="POST", sip_registration=True)
        print(f"updated SIP domain {domain.domain_name} -> {voice_url}")

    # ---- the two mappings Twilio needs ----------------------------------
    # One lets the credential REGISTER (so the phone can be reached at all),
    # the other lets it place calls. Having only the first is the usual reason
    # a softphone registers fine and then gets 403 on dialling.
    registrations = c.sip.domains(domain.sid).auth.registrations
    calls = c.sip.domains(domain.sid).auth.calls
    for label, mapping in (("registration", registrations), ("calls", calls)):
        existing = [m.sid for m in mapping.credential_list_mappings.list(limit=20)]
        if existing:
            print(f"  {label} mapping already present")
            continue
        mapping.credential_list_mappings.create(credential_list_sid=cred_list.sid)
        print(f"  {label} mapping created")

    # ---- a first caller --------------------------------------------------
    creds = c.sip.credential_lists(cred_list.sid).credentials.list(limit=50)
    if not creds:
        username = "9840012346"          # the seeded patient Sanjay Kumar
        password = strong_password()
        c.sip.credential_lists(cred_list.sid).credentials.create(
            username=username, password=password)
        write_env(**{f"SIP_PASSWORD_{username}": password})
        print(f"created caller {username}; password in backend/.env")
    else:
        print(f"callers already set up: {[x.username for x in creds]}")

    write_env(SIP_DOMAIN=domain.domain_name)
    print(f"\nSIP domain : {domain.domain_name}")
    print( "Passwords  : backend/.env (SIP_PASSWORD_<username>)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
