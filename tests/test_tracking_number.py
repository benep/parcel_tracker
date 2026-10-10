"""A shop order gets its carrier number by hand (service ``set_tracking_number``).

A parcel already tracked under that number is folded into the order; otherwise the order
is asked about under the number from now on. Either way one entry is left, and the shop's
mails, the carrier's mails and the carrier's answers all end up in it.
"""

from datetime import timedelta

import pytest
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.parcel_tracker.const import CARRIER_OTHER, DOMAIN
from custom_components.parcel_tracker.coordinator import ParcelCoordinator, TrackingRejected
from custom_components.parcel_tracker.detect import UnsupportedNumber
from custom_components.parcel_tracker.mail.apply import apply_update, fold_parcel
from custom_components.parcel_tracker.mail.base import MailUpdate
from custom_components.parcel_tracker.models import Parcel, ParcelStatus, TrackingEvent
from custom_components.parcel_tracker.store import DuplicateParcel, ParcelStore

from .test_coordinator import FakeCarrier as Possible
from .test_order_closing import (
    DHL,
    EBAY_ORDER,
    NOW,
    ORDER,
    OTHER_ORDER,
    TITLE,
    FakeCarrier,
    _carrier_parcel,
    _days,
    _order,
    _result,
)

GLS = "99999999901"
GLS_CHECKED = GLS + "7"  # as GLS mails and links show it: with a check digit
OUT = ParcelStatus.OUT_FOR_DELIVERY
TRANSIT = ParcelStatus.IN_TRANSIT
DELIVERED = ParcelStatus.DELIVERED


def _open_parcel(status=OUT, **fields) -> Parcel:
    parcel = _carrier_parcel(status=status, delivered_at=None, **fields)
    parcel.next_poll_at = NOW + timedelta(minutes=30)
    return parcel


# ----- 1. folding a parcel into the order -----
def test_fold_parcel_takes_the_result_of_a_parcel_that_is_further():
    order = _order(changed=NOW - _days(1))
    parcel = _open_parcel()
    theirs = parcel.result
    parcels = {ORDER: order, DHL: parcel}

    assert fold_parcel(parcels, order, parcel) is True
    assert list(parcels) == [ORDER]
    assert (order.tracking_ref, order.tracking_carrier) == (DHL, "dhl")
    assert order.result == theirs
    assert order.last_change_at == NOW
    assert (order.name, order.carrier, order.number, order.mail_title) == (
        TITLE, "amazon", ORDER, TITLE,
    )
    # The carrier is asked again when the parcel's own turn would have come: no extra call.
    assert (order.last_poll_at, order.next_poll_at) == (NOW, NOW + timedelta(minutes=30))
    assert order.unannounced_from is None


def test_fold_parcel_keeps_the_result_of_an_order_that_is_ahead():
    order = _order(status=DELIVERED, changed=NOW - _days(1))
    own = order.result
    parcel = _open_parcel(status=TRANSIT)
    parcel.track17, parcel.track17_next_at = True, NOW + _days(1)
    parcels = {ORDER: order, DHL: parcel}

    assert fold_parcel(parcels, order, parcel) is False
    assert list(parcels) == [ORDER]
    assert (order.status, order.result.status_text) == (DELIVERED, own.status_text)
    assert order.last_change_at == NOW - _days(1)
    assert (order.tracking_ref, order.tracking_carrier) == (DHL, "dhl")
    assert (order.track17, order.track17_next_at) == (True, None)  # delivered: 17track ends


def test_fold_parcel_keeps_the_order_while_the_carrier_knows_nothing():
    order = _order()
    parcel = Parcel(DHL, "dhl", "manual", "Oma", NOW, NOW, last_error="missing_key")
    parcels = {ORDER: order, DHL: parcel}

    assert fold_parcel(parcels, order, parcel) is False
    assert order.status is TRANSIT
    assert order.last_error == "missing_key"
    assert order.name == TITLE  # the order's own name stays


def test_fold_parcel_gives_an_unnamed_order_the_name_of_the_parcel():
    order = _order(title=None)
    parcel = _open_parcel(name="Oma")
    fold_parcel({ORDER: order, DHL: parcel}, order, parcel)
    assert order.name == "Oma"


def test_fold_parcel_an_assumed_delivery_gives_way_to_the_carrier():
    order = _order(status=DELIVERED)
    order.assumed_delivered = True
    parcel = _open_parcel(status=TRANSIT)
    assert fold_parcel({ORDER: order, DHL: parcel}, order, parcel) is True
    assert (order.status, order.assumed_delivered) == (TRANSIT, False)


def test_fold_parcel_a_problem_of_the_carrier_always_counts():
    order = _order(status=OUT)
    parcel = _open_parcel(status=ParcelStatus.EXCEPTION)
    assert fold_parcel({ORDER: order, DHL: parcel}, order, parcel) is True
    assert order.status is ParcelStatus.EXCEPTION


@pytest.mark.parametrize(
    ("waiting", "expected"),
    [(TRANSIT, TRANSIT), (None, None)],  # the order's own old status, or nothing to tell
)
def test_fold_parcel_takes_over_an_announcement_that_still_waits(waiting, expected):
    order = _order()
    order.unannounced_from = ParcelStatus.PRE_TRANSIT if waiting is None else None
    parcel = _open_parcel()
    parcel.unannounced_from = waiting
    fold_parcel({ORDER: order, DHL: parcel}, order, parcel)
    assert order.unannounced_from is expected


def test_fold_parcel_moves_17track_and_the_delivery_code_along():
    order = _order()
    parcel = _open_parcel()
    parcel.track17, parcel.track17_carrier = True, 7041
    parcel.track17_next_at = NOW + _days(1)
    parcel.track17_result = _result(OUT)
    parcel.delivery_code, parcel.delivery_code_day = "1234", NOW.date()
    fold_parcel({ORDER: order, DHL: parcel}, order, parcel)
    assert (order.track17, order.track17_carrier, order.track17_next_at) == (
        True, 7041, NOW + _days(1),
    )
    assert order.track17_result is parcel.track17_result
    assert (order.delivery_code, order.delivery_code_day) == ("1234", NOW.date())


SHIPPED = TrackingEvent(NOW - _days(2), "Versendet", None)
ORDERED = TrackingEvent(NOW - _days(3), "Bestellt", None)


def _order_with_history(**fields) -> Parcel:
    order = _order(**fields)
    order.result.events = [SHIPPED, ORDERED]
    return order


def test_fold_parcel_joins_both_histories():
    order = _order_with_history()
    parcel = _open_parcel()
    announced = TrackingEvent(NOW - _days(2) + timedelta(hours=1), "Elektronisch angekündigt", None)
    arrived = parcel.result.events[0]
    parcel.result.events = [arrived, announced]
    fold_parcel({ORDER: order, DHL: parcel}, order, parcel)
    # Newest first, the shop's "Versendet" and "Bestellt" between and after the carrier's.
    assert order.result.events == [arrived, announced, SHIPPED, ORDERED]
    assert order.status is OUT

    # Also when the order's own status stays.
    order = _order_with_history(status=DELIVERED)
    parcel = _open_parcel(status=TRANSIT)
    fold_parcel({ORDER: order, DHL: parcel}, order, parcel)
    assert order.result.events == [parcel.result.events[0], SHIPPED, ORDERED]
    assert order.status is DELIVERED


async def test_the_carrier_adds_to_the_history_of_an_order(hass, hass_storage, freezer):
    freezer.move_to(NOW)
    scans = [TrackingEvent(NOW - timedelta(hours=h), f"Scan {h}", "Bonn") for h in range(7)]
    dhl = FakeCarrier(_result(OUT, "In Zustellung", events=scans[2:]))
    coord = await _coord(hass, hass_storage, [_order_with_history()], dhl=dhl)

    order = await coord.async_set_tracking(ORDER, DHL, "dhl")
    assert order.result.events == [*scans[2:], SHIPPED, ORDERED]
    # The next answer shows the carrier's newest five: nothing told before is lost.
    dhl.answer = _result(OUT, "In Zustellung", events=scans[:5])
    order.next_poll_at = None
    await coord.async_refresh()
    await hass.async_block_till_done()
    assert order.result.events == [*scans, SHIPPED, ORDERED]
    # A mail of the carrier or of the shop keeps all of it too.
    apply_update(coord.store.parcels, MailUpdate(DHL, "dhl", DELIVERED, NOW), NOW)
    assert order.result.events[1:] == [*scans, SHIPPED, ORDERED]
    assert order.result.events[0].text == "Zugestellt"


def test_fold_parcel_keeps_the_day_of_the_shop_while_the_carrier_names_none():
    day = NOW.date() + _days(1)
    order = _order(first=day, last=day + _days(1))
    parcel = _open_parcel()
    assert fold_parcel({ORDER: order, DHL: parcel}, order, parcel) is True
    assert (order.status, order.result.eta_date, order.result.eta_latest) == (
        OUT, day, day + _days(1),
    )
    # Not once it arrived: that day no longer holds.
    order = _order(first=day)
    parcel = _carrier_parcel()
    fold_parcel({ORDER: order, DHL: parcel}, order, parcel)
    assert (order.status, order.result.eta_date) == (DELIVERED, None)


# ----- 2. the coordinator -----
class FakeGls(FakeCarrier):
    key = "gls"
    name = "GLS"


async def _coord(hass, hass_storage, parcels, **carriers) -> ParcelCoordinator:
    hass_storage[DOMAIN] = {
        "version": 1,
        "key": DOMAIN,
        "data": {"parcels": [p.to_dict() for p in parcels], "message_ids": []},
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"postcode": "10115"})
    entry.add_to_hass(hass)
    store = ParcelStore(hass)
    await store.async_load()
    return ParcelCoordinator(hass, entry, store, carriers)


async def test_a_tracked_parcel_is_folded_into_the_order(hass, hass_storage, freezer):
    freezer.move_to(NOW)
    dhl = FakeCarrier(_result(DELIVERED))
    coord = await _coord(hass, hass_storage, [_order(), _open_parcel()], dhl=dhl)

    order = await coord.async_set_tracking(ORDER.lower(), "0034 0999 9999 9999 9901", "auto")
    assert list(coord.store.parcels) == [ORDER]
    assert (order.number, order.tracking_ref, order.tracking_carrier) == (ORDER, DHL, "dhl")
    assert order.status is OUT
    assert dhl.calls == 0  # the parcel was asked about a moment ago

    # From now on both kinds of mail end up in the one entry.
    parcels = coord.store.parcels
    carrier_mail = MailUpdate(DHL, "dhl", DELIVERED, NOW)
    assert apply_update(parcels, carrier_mail, NOW).parcel is order
    shop_mail = MailUpdate(ORDER, "amazon", DELIVERED, NOW, title=TITLE)
    apply_update(parcels, shop_mail, NOW)
    assert list(parcels) == [ORDER]
    # And the number cannot come into the list a second time.
    with pytest.raises(DuplicateParcel):
        await coord.async_add(DHL, "auto", None)


async def test_without_a_parcel_the_order_is_asked_about_right_away(hass, hass_storage, freezer):
    freezer.move_to(NOW)
    dhl = FakeCarrier(_result(OUT, "In Zustellung"))
    coord = await _coord(hass, hass_storage, [_order(carrier="ebay", number=EBAY_ORDER)], dhl=dhl)

    order = await coord.async_set_tracking(EBAY_ORDER, DHL, "auto")
    assert dhl.calls == 1
    assert (order.carrier, order.tracking_ref) == ("ebay", DHL)
    assert order.tracking_carrier == "dhl"  # "Automatisch" found it: asked directly from now on
    assert order.status is OUT
    assert order.last_error is None


@pytest.mark.parametrize("assumed", [False, True])
async def test_a_delivered_order_is_not_asked_about(hass, hass_storage, freezer, assumed):
    freezer.move_to(NOW)
    stored = _order(status=DELIVERED, changed=NOW - _days(1))
    stored.assumed_delivered = assumed
    dhl = FakeCarrier(_result(TRANSIT))
    coord = await _coord(hass, hass_storage, [stored], dhl=dhl)
    order = await coord.async_set_tracking(ORDER, DHL, "dhl")
    assert (order.tracking_ref, order.status, dhl.calls) == (DHL, DELIVERED, 0)
    assert order.last_change_at == NOW - _days(1)  # stays in the list no longer than before
    # A mail of the carrier still finds it (and may reopen what was only assumed).
    apply_update(coord.store.parcels, MailUpdate(DHL, "dhl", OUT, NOW), NOW)
    assert list(coord.store.parcels) == [ORDER]
    assert order.status is (OUT if assumed else DELIVERED)


async def test_gls_numbers_are_stored_without_the_check_digit(hass, hass_storage, freezer):
    freezer.move_to(NOW)
    gls = FakeGls(_result(TRANSIT))
    coord = await _coord(hass, hass_storage, [_order()], gls=gls)
    order = await coord.async_set_tracking(ORDER, GLS_CHECKED, "gls")
    assert (order.tracking_ref, order.tracking_carrier) == (GLS, "gls")
    # Typed in either form, it is the number of that order.
    for number in (GLS, GLS_CHECKED):
        with pytest.raises(DuplicateParcel):
            await coord.async_add(number, "gls", None)


@pytest.mark.parametrize(
    ("stored", "typed", "carrier"),
    [(GLS, GLS_CHECKED, "gls"), (GLS_CHECKED, GLS, "auto"), (GLS_CHECKED, GLS_CHECKED, "gls")],
)
async def test_a_gls_parcel_is_found_under_either_form(
    hass, hass_storage, freezer, stored, typed, carrier
):
    freezer.move_to(NOW)
    parcel = _open_parcel(carrier="gls")
    parcel.number = stored
    gls = FakeGls()
    coord = await _coord(hass, hass_storage, [_order(), parcel], gls=gls)
    order = await coord.async_set_tracking(ORDER, typed, carrier)
    assert list(coord.store.parcels) == [ORDER]
    # Stored the way GLS mails name it, so they find the order.
    assert (order.tracking_ref, order.status, gls.calls) == (GLS, OUT, 0)
    mail = MailUpdate(GLS, "gls", DELIVERED, NOW)
    assert apply_update(coord.store.parcels, mail, NOW).parcel is order


async def test_a_17track_registration_keeps_the_number_it_was_made_for(
    hass, hass_storage, freezer
):
    freezer.move_to(NOW)
    parcel = _open_parcel(carrier="gls")
    parcel.number, parcel.track17 = GLS_CHECKED, True
    coord = await _coord(hass, hass_storage, [_order(), parcel], gls=FakeGls())
    order = await coord.async_set_tracking(ORDER, GLS_CHECKED, "gls")
    assert (order.tracking_ref, order.track17) == (GLS_CHECKED, True)


async def test_a_parcel_whose_carrier_nobody_found_takes_the_chosen_one(
    hass, hass_storage, freezer
):
    freezer.move_to(NOW)
    parcel = Parcel(GLS_CHECKED, None, "auto", None, NOW, NOW, last_error="carrier_not_found")
    gls = FakeGls(_result(OUT, "In Zustellung"))
    coord = await _coord(hass, hass_storage, [_order(), parcel], gls=gls)
    order = await coord.async_set_tracking(ORDER, GLS_CHECKED, "gls")
    assert list(coord.store.parcels) == [ORDER]
    assert (order.tracking_ref, order.tracking_carrier) == (GLS, "gls")
    assert (order.status, order.last_error, gls.calls) == (OUT, None, 1)  # asked right away


async def test_what_is_refused(hass, hass_storage, freezer):
    freezer.move_to(NOW)
    taken = _order(OTHER_ORDER, "Anderes")
    taken.tracking_ref, taken.tracking_carrier = "00340999999999999902", "dhl"
    other = Parcel("LX999999999DE", CARRIER_OTHER, "manual", None, NOW, NOW, track17=True)
    coord = await _coord(
        hass, hass_storage, [_order(), taken, _open_parcel(), other], dhl=FakeCarrier()
    )

    async def reason(order, number, carrier="auto"):
        with pytest.raises(TrackingRejected) as err:
            await coord.async_set_tracking(order, number, carrier)
        return err.value.reason

    assert await reason(DHL, "00340999999999999903") == "not_an_order"
    assert await reason(OTHER_ORDER, DHL) == "tracking_set"
    assert await reason(ORDER, "00340999999999999902") == "tracking_in_use"
    assert await reason(ORDER, OTHER_ORDER) == "not_mergeable"  # an order itself
    assert await reason(ORDER, ORDER) == "not_mergeable"
    assert await reason(ORDER, "LX999999999DE") == "not_mergeable"  # shown from 17track alone
    with pytest.raises(KeyError):
        await coord.async_set_tracking("AMZ99999999999999909", DHL, "auto")
    for number in ("TBA999999999", " - "):
        with pytest.raises(UnsupportedNumber):
            await coord.async_set_tracking(ORDER, number, "auto")
    with pytest.raises(ValueError, match="unknown_carrier"):
        await coord.async_set_tracking(ORDER, DHL, "hermes")  # not set up here
    # Nothing of all that changed a parcel.
    assert coord.store.parcels[ORDER].tracking_ref is None
    assert len(coord.store.parcels) == 4


async def test_automatisch_needs_a_number_one_carrier_is_sure_about(hass, hass_storage, freezer):
    freezer.move_to(NOW)
    maybe = Possible()  # takes every run of digits, sure about none
    maybe.answers = [_result(TRANSIT)]
    known = Parcel("99999999999903", "fake", "auto", None, NOW, NOW, result=_result(OUT))
    coord = await _coord(hass, hass_storage, [_order(), known], fake=maybe)
    # 12 digits may be DHL's or GLS's with its check digit: asking the wrong one forever
    # would find nothing, so the carrier has to be chosen.
    for number in ("ABC999", "999999999017"):
        with pytest.raises(TrackingRejected) as err:
            await coord.async_set_tracking(ORDER, number, "auto")
        assert err.value.reason == "carrier_required"
    assert (coord.store.parcels[ORDER].tracking_ref, maybe.calls) == (None, 0)
    # A parcel in the list has its carrier already: nothing to choose.
    order = await coord.async_set_tracking(ORDER, known.number, "auto")
    assert (order.tracking_ref, order.tracking_carrier, order.status) == (known.number, "fake", OUT)


async def test_ups_numbers_are_sure_by_their_form(hass, hass_storage, freezer):
    freezer.move_to(NOW)
    coord = await _coord(hass, hass_storage, [_order()])  # UPS is not set up
    order = await coord.async_set_tracking(ORDER, "1Z9999999999999999", "auto")
    # Stored for UPS, which is asked once its credentials are entered.
    assert (order.tracking_carrier, order.last_error) == ("ups", "missing_key")


# ----- 3. the service -----
async def test_service_folds_the_parcel_and_its_sensor_goes(hass, hass_storage, freezer):
    freezer.move_to(NOW)
    await hass.config.async_set_time_zone("Europe/Berlin")
    hass_storage[DOMAIN] = {
        "version": 1,
        "key": DOMAIN,
        "data": {
            "parcels": [_order().to_dict(), _open_parcel().to_dict()],
            "message_ids": [],
        },
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"postcode": "10115"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(f"sensor.paket_{DHL}").state == "out_for_delivery"

    async def call(**data):
        await hass.services.async_call(DOMAIN, "set_tracking_number", data, blocking=True)
        await hass.async_block_till_done()

    await call(number=ORDER, tracking_number=DHL)
    assert hass.states.get(f"sensor.paket_{DHL}") is None
    assert er.async_get(hass).async_get(f"sensor.paket_{DHL}") is None
    state = hass.states.get(f"sensor.paket_{ORDER.lower()}")
    assert state.state == "out_for_delivery"
    assert (state.attributes["tracking_ref"], state.attributes["tracking_carrier"]) == (DHL, "dhl")

    for data, key in (
        ({"number": ORDER, "tracking_number": "00340999999999999902"}, "tracking_set"),
        ({"number": DHL, "tracking_number": "00340999999999999902"}, "not_tracked"),
        ({"number": ORDER, "tracking_number": "TBA999999999"}, "amazon"),
    ):
        with pytest.raises(ServiceValidationError) as err:
            await call(**data)
        assert err.value.translation_key == key
