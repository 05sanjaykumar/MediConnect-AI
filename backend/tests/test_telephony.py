# backend/tests/test_telephony.py
"""The phone channel.

These cover the parts of telephony that can be checked without Twilio on the
line: the TwiML handed back when a call arrives, how a caller's number is
turned into a patient, and the handshake that starts a media stream. The audio
path itself needs a real call.
"""

import asyncio
import json

import pytest

from routes import telephony
from routes.audio import _national_number, identify_caller


class FakeRequest:
    """Just enough of a Request for the webhook: a form."""

    def __init__(self, data: dict):
        self._data = data

    async def form(self):
        return self._data


class FakeWebSocket:
    """Replays the messages Twilio sends when a stream opens."""

    def __init__(self, messages: list[str]):
        self._messages = messages

    def iter_text(self):
        async def gen():
            for message in self._messages:
                yield message

        return gen()


def twiml(**form) -> str:
    form.setdefault("CallSid", "CA00000000000000000000000000000000")
    return asyncio.run(telephony.voice_webhook(FakeRequest(form)))


# ------------------------------------------------------------------- TwiML


def test_inbound_call_is_identified_by_the_number_that_dialled_us():
    xml = twiml(Direction="inbound", From="+918072348791", To="+15550001111")
    assert 'name="caller" value="+918072348791"' in xml


def test_outbound_call_is_identified_by_the_number_we_dialled():
    """On a call we placed, `From` is our own Twilio number — it identifies
    nobody. The patient is whoever we rang."""
    xml = twiml(Direction="outbound-api", From="+15550001111", To="+918072348791")
    assert 'name="caller" value="+918072348791"' in xml
    assert "+15550001111" not in xml


def test_twiml_connects_a_bidirectional_stream_to_this_server():
    xml = twiml(Direction="inbound", From="+918072348791", To="+15550001111")
    assert "<Connect>" in xml and "<Stream" in xml
    assert xml.startswith("<?xml")
    # Must be wss:// — Twilio refuses a plaintext media stream.
    assert 'url="wss://' in xml
    assert "/api/twilio/stream" in xml


def test_twiml_uses_the_configured_public_host(monkeypatch):
    monkeypatch.setattr(telephony, "PUBLIC_HOST", "https://example.ngrok-free.app/")
    xml = twiml(Direction="inbound", From="+918072348791", To="+15550001111")
    # Scheme and trailing slash are stripped, so the URL is never malformed.
    assert 'url="wss://example.ngrok-free.app/api/twilio/stream"' in xml


# -------------------------------------------------------- caller identity


@pytest.mark.parametrize(
    "number,expected",
    [
        ("+918072348791", "8072348791"),   # E.164, as a real call arrives
        ("9840012346", "9840012346"),      # already national
        ("+1 555 010 1234", "5550101234"),  # spaces and a different country
        ("web-1a2b3c4d", None),            # a browser id is not a phone number
        ("12345", None),                   # too short to be one either
    ],
)
def test_national_number_extraction(number, expected):
    assert _national_number(number) == expected


def test_phone_caller_in_e164_is_matched_to_the_seeded_patient():
    """The demo patient is stored as 9840012346; a real call would present
    +919840012346. Without normalising, every phone caller is a stranger."""
    session = identify_caller("+919840012346")
    assert session.known_at_start is True
    assert session.patient_name == "Sanjay Kumar"
    assert session.patient_id is not None


def test_unknown_phone_caller_is_not_mistaken_for_a_patient():
    session = identify_caller("+919999000011")
    assert session.known_at_start is False
    assert session.patient_id is None


def test_browser_caller_without_an_id_is_never_matched():
    session = identify_caller(None)
    assert session.known_at_start is False
    assert session.caller_phone.startswith("web-")


# ------------------------------------------------------- stream handshake


def test_stream_start_is_read_past_twilios_connected_message():
    """Twilio sends `connected` before `start`; the sids we need are in the
    second message, so the first has to be consumed and discarded."""
    websocket = FakeWebSocket([
        json.dumps({"event": "connected", "protocol": "Call"}),
        json.dumps({
            "event": "start",
            "start": {
                "streamSid": "MZ123",
                "callSid": "CA456",
                "customParameters": {"caller": "+918072348791"},
            },
        }),
    ])
    start = asyncio.run(telephony._read_stream_start(websocket))
    assert start["streamSid"] == "MZ123"
    assert start["callSid"] == "CA456"
    assert start["customParameters"]["caller"] == "+918072348791"


# ------------------------------------------------------------- configuration


def test_status_reports_what_telephony_is_missing(monkeypatch):
    monkeypatch.setattr(telephony, "TWILIO_PHONE_NUMBER", "")
    monkeypatch.setattr(telephony, "PUBLIC_HOST", "")
    status = telephony.telephony_status()
    assert status["configured"] is False
    assert status["twilio_number"] is None
    assert status["public_host"] is None


def test_status_is_complete_once_everything_is_set(monkeypatch):
    monkeypatch.setattr(telephony, "TWILIO_ACCOUNT_SID", "AC123")
    monkeypatch.setattr(telephony, "TWILIO_AUTH_TOKEN", "secret")
    monkeypatch.setattr(telephony, "TWILIO_PHONE_NUMBER", "+15550001111")
    monkeypatch.setattr(telephony, "PUBLIC_HOST", "example.ngrok-free.app")
    status = telephony.telephony_status()
    assert status["configured"] is True
    assert status["webhook_url"] == "https://example.ngrok-free.app/api/twilio/voice"
    assert status["stream_url"] == "wss://example.ngrok-free.app/api/twilio/stream"


# ------------------------------------------------------------- SIP callers


@pytest.mark.parametrize(
    "value,expected",
    [
        ("sip:9840012346@mediconnect-097e72.sip.twilio.com", "9840012346"),
        ("sips:9840012347@example.sip.twilio.com", "9840012347"),
        ("sip:sanjay@example.sip.twilio.com", "sanjay"),
        ("+918072348791", "+918072348791"),   # a PSTN call is left alone
        ("", ""),
    ],
)
def test_sip_uri_is_reduced_to_the_caller(value, expected):
    assert telephony._caller_id(value) == expected


def test_sip_call_identifies_the_patient_not_the_domain():
    """The domain half of a SIP URI contains digits. Passing the whole URI to
    the caller lookup would mix them into the phone number and match nobody."""
    xml = twiml(Direction="inbound",
                From="sip:9840012346@mediconnect-097e72.sip.twilio.com",
                To="sip:book@mediconnect-097e72.sip.twilio.com")
    assert 'name="caller" value="9840012346"' in xml
    assert "097e72" not in xml.split('name="caller"')[1][:40]


def test_sip_caller_reaches_the_same_patient_record():
    session = identify_caller(telephony._caller_id(
        "sip:9840012346@mediconnect-097e72.sip.twilio.com"))
    assert session.known_at_start is True
    assert session.patient_name == "Sanjay Kumar"
