"""Where a delivered parcel was left ("Briefkasten", "Nachbar"), as the delivery mail says.

Only fixed words are taken (``mail.base.LETTER_BOX`` …): never the drop-off place the
recipient chose ("Garage") and never a neighbour's name.
"""

from email import policy
from email.message import EmailMessage
from email.parser import BytesParser

import pytest

from custom_components.parcel_tracker.mail import parse_mail
from custom_components.parcel_tracker.mail.apply import apply_update
from custom_components.parcel_tracker.mail.base import sent_at
from custom_components.parcel_tracker.models import ParcelStatus
from custom_components.parcel_tracker.notification import build_notification

from .conftest import FIXTURES, load_mail

AMAZON = "180_order_update_zugestellt_hausbewohner.eml"
ORDER = "AMZ99999999999999180"
SENTENCE = "Die Sendung wurde einem Hausbewohner übergeben."


def _variant(name: str, *swaps: tuple[str, str]):
    """The fixture with parts of its text replaced."""
    text = (FIXTURES / "mail" / name).read_text(encoding="utf-8")
    for old, new in swaps:
        assert old in text, old
        text = text.replace(old, new)
    return BytesParser(policy=policy.default).parsebytes(text.encode("utf-8"))


def _dhl(subject: str) -> EmailMessage:
    msg = EmailMessage(policy=policy.default)
    msg["From"] = "DHL Paket <noreply@dhl.de>"
    msg["Subject"] = subject
    msg["Date"] = "Tue, 04 Nov 2025 13:57:48 +0100"
    msg.set_content("Wir haben sie bei Nowak (Musterstraße) abgegeben.\n"
                    "Ihre Sendungsnummer\n00340999999999990180\n")
    return msg


# ----- Amazon -----
def test_amazon_names_the_place_in_its_html_part():
    [u] = parse_mail(load_mail(AMAZON)).updates
    assert (u.number, u.status, u.delivery_place) == (
        ORDER, ParcelStatus.DELIVERED, "Hausbewohner",
    )


@pytest.mark.parametrize(
    ("sentence", "place"),
    [
        ("Die Sendung wurde im Briefkasten hinterlegt", "Briefkasten"),
        ("Die Sendung wurde im Briefkasten zugestellt.", "Briefkasten"),
        ("Dein Paket wurde an deinen Briefkasten zugestellt.", "Briefkasten"),
        ("Die Sendung wurde bei der Haustür oder auf der Veranda hinterlegt.", "Haustür"),
        ("Die Sendung wurde bei der Haustür oder auf der Veranda zugestellt.", "Haustür"),
        ("Die Sendung wurde einer Empfangsperson übergeben.", "Empfangsperson"),
        ("Die Sendung wurde zugestellt.", None),
    ],
)
def test_amazon_sentences_of_the_place(sentence, place):
    [u] = parse_mail(_variant(AMAZON, (SENTENCE, sentence))).updates
    assert (u.status, u.delivery_place) == (ParcelStatus.DELIVERED, place)


def test_amazon_reads_the_text_part_first():
    # (the HTML part says "Hausbewohner")
    in_text = "Heute zugestellt\nDie Sendung wurde im Briefkasten hinterlegt\n"
    [u] = parse_mail(_variant(AMAZON, ("Heute zugestellt\n", in_text))).updates
    assert u.delivery_place == "Briefkasten"


def test_only_a_delivery_has_a_place():
    msg = _variant(AMAZON, ("Subject: Zugestellt:", "Subject: In Zustellung:"))
    [u] = parse_mail(msg).updates
    assert (u.status, u.delivery_place) == (ParcelStatus.OUT_FOR_DELIVERY, None)


# ----- DHL: the subject tells it -----
def test_dhl_letter_box_mail_names_the_letter_box():
    [u] = parse_mail(load_mail("158_dhl_zustellung_liegt_im_briefkasten.eml")).updates
    assert (u.status, u.delivery_place) == (ParcelStatus.DELIVERED, "Briefkasten")


@pytest.mark.parametrize(
    ("subject", "place"),
    [
        ("Ihre Beispiel GmbH Sendung liegt im Briefkasten", "Briefkasten"),
        ("Ihre Beispiel GmbH Sendung liegt am gewünschten Ablageort", "Ablageort"),
        ("Ihre Beispiel GmbH Sendung wurde an den gewünschten Ablageort zugestellt", "Ablageort"),
        ("Ihre Beispiel GmbH Sendung liegt nebenan", "Nachbar"),
        ("Ihre Beispiel GmbH Sendung wurde zugestellt", None),
    ],
)
def test_dhl_subjects_of_the_place(subject, place):
    [u] = parse_mail(_dhl(subject)).updates
    assert (u.status, u.delivery_place) == (ParcelStatus.DELIVERED, place)
    # (the neighbour's name in the text is never read)
    assert "Nowak" not in repr(u)


def test_dhl_pickup_has_no_delivery_place():
    [u] = parse_mail(_dhl("Ihre Beispiel GmbH Sendung wurde an Packstation 123 zugestellt")).updates
    assert (u.status, u.delivery_place) == (ParcelStatus.AWAITING_PICKUP, None)


# ----- Hermes -----
def test_hermes_delivery_at_the_door_names_the_door():
    [u] = parse_mail(load_mail("169_hermes_erfolgreiche_haustuerzustellung.eml")).updates
    assert (u.status, u.delivery_place) == (ParcelStatus.DELIVERED, "Haustür")


# ----- on the parcel -----
def test_the_place_is_the_parcels_place_and_that_of_its_delivery_line():
    parcels: dict = {}
    msg = load_mail(AMAZON)
    [update] = parse_mail(msg).updates
    apply_update(parcels, update, sent_at(msg))
    parcel = parcels[ORDER]
    assert (parcel.status, parcel.result.status_text) == (ParcelStatus.DELIVERED, "Zugestellt")
    assert parcel.result.location == "Hausbewohner"
    assert (parcel.result.events[0].text, parcel.result.events[0].location) == (
        "Zugestellt", "Hausbewohner",
    )
    # The notification never names it.
    _, message = build_notification(parcel, ParcelStatus.OUT_FOR_DELIVERY, sent_at(msg))
    assert "Hausbewohner" not in message


def test_a_delivery_mail_without_a_place_keeps_the_place():
    parcels: dict = {}
    msg = load_mail(AMAZON)
    [update] = parse_mail(msg).updates
    apply_update(parcels, update, sent_at(msg))
    [again] = parse_mail(_variant(AMAZON, (SENTENCE, "Die Sendung wurde zugestellt."))).updates
    apply_update(parcels, again, sent_at(msg))
    assert parcels[ORDER].result.location == "Hausbewohner"
