"""Card: a shop order gets its carrier number by hand (run in Node)."""

import json
import shutil
import subprocess

import pytest

from custom_components.parcel_tracker.card_install import BUNDLED_CARD

NODE = shutil.which("node")
ORDER = "AMZ99991565342587125"
DHL = "00340999999999999911"

SCRIPT = r"""
const fs = require("fs");
const vm = require("vm");
const defined = {};
const sandbox = {
  HTMLElement: class {},
  customElements: { get: (n) => defined[n], define: (n, c) => { defined[n] = c; } },
  window: {},
};
vm.createContext(sandbox);
vm.runInContext(
  fs.readFileSync(process.argv[1], "utf8")
    + "\n;globalThis.__can = canSetTracking;",
  sandbox,
);
const Card = defined["parcel-tracker-card"];
const card = new Card();
card.setConfig({});
const calls = [];
let fail = null;
card._hass = {
  states: {}, entities: {},
  callService: async (...args) => { calls.push(args); if (fail) throw fail; },
};
const err = { innerHTML: "" };
card._root = { getElementById: () => err };
let renders = 0;
card._renderList = () => { renders += 1; };

const order = { number: "AMZ99991565342587125", carrier: "amazon" };
const DHL = "00340999999999999911";
const out = {
  can: {
    amazon: sandbox.__can(order, "in_transit"),
    ebay: sandbox.__can({ number: "EBAY990000000001", carrier: "ebay" }, "pre_transit"),
    withNumber: sandbox.__can({ ...order, tracking_ref: DHL }, "in_transit"),
    carrier: sandbox.__can({ number: DHL, carrier: "dhl" }, "in_transit"),
    delivered: sandbox.__can(order, "delivered"),
    assumed: sandbox.__can({ ...order, assumed_delivered: true }, "delivered"),
  },
  line: {
    none: card._refHtml({ number: DHL, carrier: "dhl" }, "in_transit"),
    dhl: card._refHtml({ ...order, tracking_ref: DHL, tracking_carrier: "dhl" }, "in_transit"),
    auto: card._refHtml({ ...order, tracking_ref: DHL, tracking_carrier: null }, "delivered"),
  },
  plain: card._refHtml(order, "in_transit"),
  actions: card._actionsHtml(order),
  delivered: card._refHtml(order, "delivered"),
};

// One element per selector, as far as _wireActions needs it.
const els = {};
const item = {
  querySelector(q) {
    els[q] = els[q] || { value: "", on: {}, addEventListener(name, fn) { this.on[name] = fn; } };
    return els[q];
  },
};
const el = (a) => els[`[data-a="${a}"]`];
card._wireActions(item, order);

el("ref").on.click();
out.opened = { state: { ...card._refs.get(order.number) }, focus: card._pendingFocus, renders };
el("ref-input").value = " 0034 0999 9999 9999 9911 ";
el("ref-input").on.input();
el("ref-carrier").value = "gls";
el("ref-carrier").on.change();
out.typed = { ...card._refs.get(order.number) };
out.editor = card._refHtml(order, "in_transit");
// A mail gave the order its number meanwhile: nothing left to type.
out.overtaken = card._refHtml({ ...order, tracking_ref: DHL }, "in_transit");

(async () => {
  // An empty field never reaches the integration.
  card._refs.get(order.number).text = "  ";
  await el("ref-save").on.click();
  out.empty = { calls: calls.length, error: err.innerHTML, editing: card._refs.has(order.number) };

  card._refs.get(order.number).text = " 0034 0999 9999 9999 9911 ";
  fail = { translation_key: "tracking_in_use" };
  await el("ref-save").on.click();
  out.refused = { error: err.innerHTML, editing: card._refs.has(order.number) };

  fail = null;
  let prevented = false;
  const sent = calls.length;
  // Enter twice while the call is on its way: sent once.
  el("ref-input").on.keydown({ key: "Enter", preventDefault() { prevented = true; } });
  el("ref-input").on.keydown({ key: "Enter", preventDefault() {} });
  await new Promise((done) => setTimeout(done, 0));
  out.saved = {
    calls: calls.length - sent,
    call: calls[calls.length - 1].slice(0, 3), prevented,
    error: err.innerHTML, editing: card._refs.has(order.number), focus: card._pendingFocus,
  };

  el("ref").on.click();
  el("ref-input").on.keydown({ key: "Escape", preventDefault() {} });
  out.escaped = { editing: card._refs.has(order.number), focus: card._pendingFocus };
  el("ref").on.click();
  el("ref-cancel").on.click();
  out.cancelled = { editing: card._refs.has(order.number), focus: card._pendingFocus };
  console.log(JSON.stringify(out));
})();
"""


@pytest.fixture(scope="module")
def card():
    if NODE is None:
        pytest.skip("node is not installed")
    done = subprocess.run(
        [NODE, "-e", SCRIPT, str(BUNDLED_CARD)], capture_output=True, text=True, check=True
    )
    return json.loads(done.stdout)


def test_link_only_at_a_shop_order_without_a_carrier_number(card):
    assert card["can"] == {
        "amazon": True, "ebay": True, "withNumber": False, "carrier": False,
        # Delivered for sure: nothing left to ask. Closed without a delivery mail: still open.
        "delivered": False, "assumed": True,
    }
    link = (
        f'<button type="button" class="link" data-a="ref" data-number="{ORDER}">'
        "Sendungsnummer hinzufügen</button>"
    )
    assert card["plain"] == f'<div class="detail note">{link}</div>'
    assert card["delivered"] == ""


def test_the_row_of_buttons_stays_as_it_is(card):
    """A fourth button made the row wrap into two lines: the link stands under the history."""
    assert 'data-a="ref' not in card["actions"]
    assert card["actions"].count("<button") == 2 and card["actions"].count("<a ") == 1


def test_the_link_opens_an_editor_in_place(card):
    assert card["opened"] == {
        "state": {"text": "", "carrier": "auto"},
        "focus": {"number": ORDER, "a": "ref-input"},
        "renders": 1,
    }
    # What is typed survives a redraw of the list (every sensor update draws it anew).
    assert card["typed"] == {"text": " 0034 0999 9999 9999 9911 ", "carrier": "gls"}
    editor = card["editor"]
    assert 'role="group" aria-label="Sendungsnummer hinzufügen"' in editor
    assert 'data-a="ref-input"' in editor and 'value=" 0034 0999 9999 9999 9911 "' in editor
    assert '<option value="auto">Automatisch</option>' in editor
    assert '<option value="gls" selected>GLS</option>' in editor
    assert [f'value="{key}"' in editor for key in ("dhl", "dpd", "hermes", "ups")] == [True] * 4
    assert 'value="other"' not in editor  # an order's number is asked at a carrier
    # Two rows on purpose: number and carrier, then the two buttons.
    assert editor.count('<div class="actions">') == 2
    second = editor.rindex('<div class="actions">')
    assert editor.index('data-a="ref-carrier"') < second < editor.index('data-a="ref-save"')
    assert 'data-a="ref-cancel"' in editor
    assert 'data-a="ref-input"' not in card["overtaken"]


def test_saving_calls_the_service_with_number_and_carrier(card):
    assert card["empty"] == {"calls": 0, "error": "Gib eine Sendungsnummer ein.", "editing": True}
    assert card["refused"] == {
        "error": "Diese Sendungsnummer gehört schon zu einer anderen Bestellung.",
        "editing": True,  # stays open: the number can be corrected
    }
    assert card["saved"] == {
        "calls": 1,
        "call": [
            "parcel_tracker", "set_tracking_number",
            {"number": ORDER, "tracking_number": "0034 0999 9999 9999 9911", "carrier": "gls"},
        ],
        "prevented": True,
        "error": "",
        "editing": False,
        "focus": {"number": ORDER, "a": "toggle"},
    }


def test_escape_and_abbrechen_close_the_editor(card):
    back = {"editing": False, "focus": {"number": ORDER, "a": "ref"}}
    assert card["escaped"] == back
    assert card["cancelled"] == back


def test_an_order_shows_the_carrier_number_it_is_asked_about_under(card):
    assert card["line"] == {
        "none": "",  # a carrier's parcel shows its number as its name already
        "dhl": f'<div class="detail note">Sendungsnummer {DHL} · DHL</div>',
        "auto": f'<div class="detail note">Sendungsnummer {DHL}</div>',  # also once delivered
    }


def test_every_refusal_of_the_service_has_a_german_text():
    card = BUNDLED_CARD.read_text(encoding="utf-8")
    translations = BUNDLED_CARD.parents[1] / "translations" / "de.json"
    messages = json.loads(translations.read_text(encoding="utf-8"))["exceptions"]
    for key in ("not_an_order", "tracking_set", "tracking_in_use", "carrier_required",
                "not_mergeable"):
        assert f'{key}: "{messages[key]["message"]}"' in card
