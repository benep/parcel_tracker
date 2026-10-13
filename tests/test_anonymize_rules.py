"""Tester mode of the anonymiser: rules of the v0.3.3 final review (invented mails only)."""

import email
import html
import os
import re
import subprocess
import sys
import urllib.parse
from email import policy
from email.message import EmailMessage
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "anonymize_mail.py"
DATE = "Tue, 18 Jul 2023 08:02:46 +0000"
SHOP = "Paketflitzer <tracking@paketflitzer.example>"
HERMES = "Hermes <noreply@paketankuendigung.myhermes.de>"


def _mail(sender: str, subject: str, text: str, to: str = "pakete@postfach.example") -> bytes:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = DATE
    msg.set_content(text)
    return bytes(msg)


def _call(*args, **kwargs):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *map(str, args)],
        capture_output=True, text=True, stdin=subprocess.DEVNULL, encoding="utf-8",
        env={**os.environ, "PYTHONUTF8": "1"}, **kwargs,
    )


def _run(tmp_path: Path, mails, *args):
    """Anonymise ``mails`` without personal values: (process, [(file name, message)])."""
    src = tmp_path / "mails"
    src.mkdir()
    for i, raw in enumerate(mails):
        (src / f"mail{i}.eml").write_bytes(raw)
    done = _call(src, *(args or ("--ohne-angaben",)))
    assert done.returncode == 0, done.stderr
    written = sorted((src / "anonymisiert").glob("*.eml"))
    return done, [
        (p.name, email.message_from_bytes(p.read_bytes(), policy=policy.default)) for p in written
    ]


def _text(msg: EmailMessage) -> str:
    return msg.get_body(("plain",)).get_content()


def _dump(msg: EmailMessage) -> str:
    """Everything readable in a written mail, lower case."""
    raw = bytes(msg)
    chunks = [raw.decode("utf-8", "replace"), *(f"{key}: {value}" for key, value in msg.items())]
    for part in msg.walk():
        if not part.is_multipart():
            payload = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
            chunks += [payload, html.unescape(payload)]
    text = "\n".join(chunks)
    return (text + urllib.parse.unquote(text)).lower()


def _absent(msg: EmailMessage, *secrets: str) -> None:
    dump = _dump(msg)
    for secret in secrets:
        assert secret not in dump, secret


# ----- 1: address blocks -----

def test_address_blocks_with_other_labels_and_postcode_layouts(tmp_path):
    raw = _mail(
        SHOP,
        "Ihr Paket ist unterwegs",
        "Ihr Paket ist unterwegs.\n\n"
        "Ship To: \n\nErika Sonnenschein\nAm Alten Hafen 3\nANDERSWO NW 67890\nDE\n\n"
        "Lieferanschrift\nErika Sonnenschein\nAm Alten Hafen 3\nAnderswo, 67890\n\n"
        "Deliver to:\nErika Sonnenschein\nAm Alten Hafen 3\n67890\nAnderswo\n"
        "Sendungsnummer: JJD000390007812345678\n\n"
        "Delivery\nErika Sonnenschein\nAm Alten Hafen 3\n67890 Anderswo\nEnde\n",
    )
    _, [(_, msg)] = _run(tmp_path, [raw])
    text = _text(msg)
    block = "Max Mustermann\nMusterstraße 1\n12345 Musterstadt\n"
    assert "Ship To: \n" + block + "DE\n" in text
    assert "Lieferanschrift\n" + block + "\nDeliver to:\n" in text
    # the city on its own line below the postcode goes too, the next label stays
    assert "Deliver to:\n" + block + "Sendungsnummer: JJD" in text
    assert "Delivery\n" + block + "Ende\n" in text
    _absent(msg, "erika", "sonnenschein", "alten hafen", "anderswo", "67890")


# ----- 2: private senders -----

def test_private_sender_names_in_sentences_and_label_lines(tmp_path):
    raw = _mail(
        HERMES,
        "Deine Sendung ist unterwegs",
        "Dein Paket von Erika Sonnenschein mit der Sendungsnummer H1234567890123456789 kommt.\n"
        "Ihre Sendung von Bodo Ballermann, die Sie erwarten, ist unterwegs.\n"
        "Wir haben ein Paket von Clara Kirschbaum.\n"
        "Absender: Dieter Donnerwetter\n"
        "Versender: Holzwurm Möbel GmbH\n"
        "Dein Paket von Zalando.\n"
        "Die Sendung von Montag, dem 3. Juli, kommt morgen.\n"
        "Noch ein Gruß an Bodo Ballermann.\n",
    )
    _, [(_, msg)] = _run(tmp_path, [raw])
    text = _text(msg)
    assert re.search(r"Dein Paket von Beispielversender mit der Sendungsnummer H\d{19} kommt", text)
    assert "Ihre Sendung von Beispielversender, die Sie erwarten, ist unterwegs.\n" in text
    assert "Wir haben ein Paket von Beispielversender.\n" in text
    assert "Absender: Beispielversender\n" in text
    assert "Versender: Holzwurm Möbel GmbH\n" in text
    assert "Dein Paket von Zalando.\n" in text
    assert "Die Sendung von Montag, dem 3. Juli, kommt morgen.\n" in text
    assert "Noch ein Gruß an Beispielversender.\n" in text  # learnt: replaced everywhere
    _absent(msg, "sonnenschein", "ballermann", "kirschbaum", "donnerwetter")


def test_private_name_in_from_display_and_subject_is_replaced_and_learnt(tmp_path):
    private = _mail(
        "Gundula Gartenzwerg <versand@kleinerladen.example>",
        "Fritz Feuerstein hat dir ein Paket geschickt",
        "Dein Paket ist unterwegs.\n\nViele Grüße\nGundula Gartenzwerg\n"
        "Danke an Fritz Feuerstein.\n",
    )
    subject = _mail(
        SHOP, "Paket für Wilma Wolkenbruch ist unterwegs", "Bald da, Wilma Wolkenbruch.\n"
    )
    shop = _mail("Holzwurm Shop <versand@holzwurm-shop.example>", "Deine Bestellung", "Danke.\n")
    carrier = _mail("DHL Paket <noreply@dhl.de>", "Ihr DHL Paket kommt", "Bald.\n")
    _, [(name, first), (_, second), (_, third), (_, fourth)] = _run(
        tmp_path, [private, subject, shop, carrier]
    )
    assert name == "001_kleinerladen.example.eml"
    assert str(first["From"]) == "Beispielversender <versand@kleinerladen.example>"
    assert str(first["Subject"]) == "Beispielversender hat dir ein Paket geschickt"
    assert "Viele Grüße\nBeispielversender\nDanke an Beispielversender.\n" in _text(first)
    assert str(second["Subject"]) == "Paket für Max Mustermann ist unterwegs"
    assert "Bald da, Max Mustermann.\n" in _text(second)
    assert str(third["From"]) == "Holzwurm Shop <versand@holzwurm-shop.example>"
    assert str(fourth["From"]) == "DHL Paket <noreply@dhl.de>"
    assert str(fourth["Subject"]) == "Ihr DHL Paket kommt"
    for msg in (first, second):
        _absent(msg, "gundula", "gartenzwerg", "feuerstein", "wilma", "wolkenbruch")


# ----- 3: neighbours and recipients -----

def test_neighbour_and_recipient_names(tmp_path):
    raw = _mail(
        "DHL Paket <noreply@dhl.de>",
        "Ihr Paket wurde zugestellt",
        "Zugestellt an Nachbar: Bodo Ballermann\n"
        "Ihr Paket wurde beim Nachbarn Herrn Kirschbaum abgegeben.\n"
        "Empfangen von: D. DONNERWETTER\n"
        "Unterschrift: Feuerstein\n"
        "Das Paket wurde entgegengenommen von Gundula Gartenzwerg.\n"
        "Die Sendung wurde übergeben an Erika Sonnenschein\n"
        "Das Paket wurde abgegeben bei Wilma Wolkenbruch\n"
        "Die Sendung wurde übergeben an DHL.\n"
        "Eine Unterschrift ist erforderlich.\n"
        "Das Paket wurde an den Nachbarn abgegeben.\n"
        "Übergeben an Empfänger\n",
    )
    _, [(_, msg)] = _run(tmp_path, [raw])
    text = _text(msg)
    for line in (
        "Zugestellt an Nachbar: Beispielnachbar\n",
        "Ihr Paket wurde beim Nachbarn Herrn Beispielnachbar abgegeben.\n",
        "Empfangen von: Max Mustermann\n",
        "Unterschrift: Max Mustermann\n",
        "Das Paket wurde entgegengenommen von Max Mustermann.\n",
        "Die Sendung wurde übergeben an Max Mustermann\n",
        "Das Paket wurde abgegeben bei Beispielnachbar\n",
        "Die Sendung wurde übergeben an DHL.\n",
        "Eine Unterschrift ist erforderlich.\n",
        "Das Paket wurde an den Nachbarn abgegeben.\n",
        "Übergeben an Empfänger\n",
    ):
        assert line in text, line
    _absent(msg, "ballermann", "kirschbaum", "donnerwetter", "feuerstein", "gartenzwerg",
            "sonnenschein", "wolkenbruch")


# ----- 4: generic mode -----

def test_amazon_name_and_place_line_above_the_order_number(tmp_path):
    raw = _mail(
        '"Amazon.de" <shipment-tracking@amazon.de>',
        "In Zustellung: „Tastatur...“",
        "Paket befindet sich in Zustellung!\n\nAnkunft heute 18h – 22h\n"
        "Jörg – Beispielhausen\nBestellnr. ‫302-1234567-7654321\n\n"
        "Lieferung nach Beispielhausen für Jörg.\nHeute – morgen\nLieferung verfolgen\n",
    )
    _, [(_, msg)] = _run(tmp_path, [raw])
    text = _text(msg)
    assert re.search(
        r"Ankunft heute 18h – 22h\nMax – Musterstadt\nBestellnr\. \d{3}-\d{7}-\d{7}\n", text
    )
    assert "Lieferung nach Musterstadt für Max.\n" in text
    assert "Heute – morgen\nLieferung verfolgen\n" in text  # no order number below: stays
    _absent(msg, "jörg", "beispielhausen", "302-1234567")


def test_salutation_without_punctuation_and_names_learnt_from_it(tmp_path):
    raw = _mail(
        SHOP,
        "Ihre Bestellung",
        "Guten Tag Jörg Probst\n\nIhre Bestellung ist unterwegs.\n"
        "Danke, Herr Probst, für den Einkauf. Jörgs Paket kommt.\n\nHallo zusammen,\nbis bald.\n",
    )
    _, [(_, msg)] = _run(tmp_path, [raw])
    text = _text(msg)
    assert "Guten Tag Max Mustermann\n\nIhre Bestellung ist unterwegs.\n" in text
    assert "Danke, Herr Mustermann, für den Einkauf. Max Paket kommt.\n" in text
    assert "bis bald.\n" in text
    _absent(msg, "jörg", "probst")


def test_name_of_the_to_header_is_learnt(tmp_path):
    raw = _mail(
        SHOP, "Ihre Bestellung", "Vielen Dank, Jörg!\nProbst steht auf dem Paket.\n",
        to="Jörg Probst <jp@privatpost.example>",
    )
    _, [(_, msg)] = _run(tmp_path, [raw])
    assert "Vielen Dank, Max!\nMustermann steht auf dem Paket.\n" in _text(msg)
    _absent(msg, "jörg", "probst")


def test_postcode_and_city_lines_without_a_label(tmp_path):
    raw = _mail(
        SHOP,
        "Ihre Bestellung",
        "Wir liefern an:\nErika Sonnenschein\nAm Alten Hafen 3\n67890 Anderswo\n\n"
        "Abholung: Paketshop Kiosk, Sägeweg 3, 98765 Spanstadt\n"
        "Der Laden im Sägeweg hat bis 18 Uhr auf, Am Alten Hafen ist gesperrt.\n"
        "Erika Sonnenschein in 67890 Anderswo hat 20000 Punkte gesammelt.\n\n"
        "Holzwurm Möbel GmbH\nSpanplatz 12\n98765 Spanstadt\n",
    )
    _, [(_, msg)] = _run(tmp_path, [raw])
    text = _text(msg)
    assert "Wir liefern an:\nMax Mustermann\nMusterstraße 1\n12345 Musterstadt\n" in text
    assert "Abholung: Paketshop Kiosk, Musterstraße 1, 12345 Musterstadt\n" in text
    assert "Der Laden im Sägeweg hat bis 18 Uhr auf, Musterstraße ist gesperrt.\n" in text
    assert "Max Mustermann in 12345 Musterstadt hat 20000 Punkte gesammelt.\n" in text
    assert "Holzwurm Möbel GmbH\nMusterstraße 1\n12345 Musterstadt\n" in text
    _absent(msg, "erika", "sonnenschein", "alten hafen", "anderswo", "67890", "spanstadt",
            "98765", "spanplatz")


def test_without_values_the_flag_is_required(tmp_path):
    src = tmp_path / "mails"
    src.mkdir()
    (src / "mail0.eml").write_bytes(_mail(SHOP, "Ihre Bestellung", "Hallo Jörg,\ndanke.\n"))
    done = _call(src)
    assert done.returncode == 2
    assert done.stdout == ""
    assert "--ohne-angaben" in done.stderr and "--name" in done.stderr
    assert "Ohne eigene Angaben" in done.stderr
    assert not (src / "anonymisiert").exists()
    done = _call(src, "--ohne-angaben")
    assert done.returncode == 0, done.stderr
    assert "ACHTUNG" in done.stdout
    assert "Namen und Orte können stehen bleiben" in done.stdout
    assert (src / "anonymisiert" / "001_paketflitzer.example.eml").is_file()
    # a value makes the flag unnecessary, and the warning goes
    done = _call(src, "--name", "Jörg Probst")
    assert done.returncode == 0, done.stderr
    assert "ACHTUNG" not in done.stdout


def test_without_a_stdin_nothing_is_asked(tmp_path):
    """Started without a console (pythonw, a service), a script has no stdin: it is None."""
    src = tmp_path / "mails"
    src.mkdir()
    (src / "mail0.eml").write_bytes(_mail(SHOP, "Ihre Bestellung", "Hallo Jörg,\ndanke.\n"))
    code = (
        "import runpy, sys\n"
        "sys.stdin, sys.argv = None, sys.argv[1:]\n"
        "runpy.run_path(sys.argv[0], run_name='__main__')\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code, str(SCRIPT), str(src)],
        capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    assert done.returncode == 2
    assert done.stdout == ""
    assert "Ohne eigene Angaben" in done.stderr
    assert not (src / "anonymisiert").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="needs a pseudo terminal")
def test_terminal_with_only_empty_answers_needs_the_flag_too(tmp_path):
    import pty

    src = tmp_path / "mails"
    src.mkdir()
    (src / "mail0.eml").write_bytes(_mail(SHOP, "Ihre Bestellung", "Hallo Jörg,\ndanke.\n"))
    master, slave = pty.openpty()
    try:
        os.write(master, b"\n\n\n\n\n\n")
        done = subprocess.run(
            [sys.executable, str(SCRIPT), str(src)],
            stdin=slave, capture_output=True, text=True, encoding="utf-8", timeout=60,
            env={**os.environ, "PYTHONUTF8": "1"},
        )
    finally:
        os.close(slave)
        os.close(master)
    assert done.returncode == 2
    assert "Vor- und Nachname" in done.stdout  # the questions were asked
    assert "--ohne-angaben" in done.stderr
    assert not (src / "anonymisiert").exists()


# ----- 5: forwarded and private mails -----

def test_forwarded_mail_of_a_private_sender_with_an_own_domain(tmp_path):
    by_subject = _mail(
        "Erika Sonnenschein <erika@kleinefirma.example>",
        "Fwd: Ihre Bestellung ist unterwegs",
        "Schau mal.\n\nIhre Bestellung ist unterwegs.\n",
    )
    by_block = _mail(
        "Erika Sonnenschein <erika@kleinefirma.example>",
        "Paketinfo",
        "-------- Weitergeleitete Nachricht --------\n"
        "Von: Holzwurm Shop <versand@holzwurm-shop.example>\n"
        "Gesendet: Dienstag, 18. Juli 2023 10:02\n"
        "An: erika@kleinefirma.example\nBetreff: Ihre Bestellung\n\n"
        "Ihre Bestellung ist unterwegs.\n",
    )
    # "Von:" as a field of the mail itself (UPS) is no quoted header block
    plain = _mail(
        "UPS Quantum View <pkginfo@ups.com>",
        "UPS Versandbenachrichtigung",
        "Sendungsdetails\n\nVon: Holzwurm Möbel GmbH\n\nKontrollnummer: 1Z999AA10123456784\n",
    )
    done, [(one, first), (two, second), (three, third)] = _run(
        tmp_path, [by_subject, by_block, plain]
    )
    assert (one, two, three) == ("001_privat.eml", "002_privat.eml", "003_ups.com.eml")
    for msg in (first, second):
        assert str(msg["From"]) == "Max Mustermann <max@example.org>"
        _absent(msg, "erika", "sonnenschein", "kleinefirma")
    assert str(first["Subject"]) == "Fwd: Ihre Bestellung ist unterwegs"
    assert str(third["From"]) == "UPS Quantum View <pkginfo@ups.com>"
    assert "2 Mail(s) kommen von einer privaten Adresse" in done.stdout


@pytest.mark.parametrize("prefix", ["WG:", "Fwd:", "Fw:", "FW:", "AW:", "Re:"])
def test_every_forward_and_reply_prefix_marks_the_sender_as_private(tmp_path, prefix):
    raw = _mail("Erika Sonnenschein <erika@kleinefirma.example>", f"{prefix} Ihr Paket", "Text\n")
    _, [(name, msg)] = _run(tmp_path, [raw])
    assert name == "001_privat.eml"
    assert str(msg["From"]) == "Max Mustermann <max@example.org>"


# ----- 7 to 9: phone numbers, drop-off permissions, encoded sender addresses -----

def test_phone_number_with_three_separators(tmp_path):
    raw = _mail(SHOP, "Rückruf", "Rückruf unter 0211 / 123 45 67 oder (0211) 123-45-67.\n")
    _, [(_, msg)] = _run(tmp_path, [raw])
    assert "Rückruf unter +49 000 0000000 oder +49 000 0000000.\n" in _text(msg)
    _absent(msg, "0211", "123 45", "123-45")


def test_drop_off_permission_lines(tmp_path):
    raw = _mail(
        "DHL Paket <noreply@dhl.de>",
        "Ihr Paket kommt",
        "Abstellgenehmigung: Hinter dem Gartenhaus rechts\n"
        "Abstell-Okay: Carport am Seiteneingang\n"
        "Abstellerlaubnis\nUnter dem Vordach\n\nBis bald.\n",
    )
    _, [(_, msg)] = _run(tmp_path, [raw])
    text = _text(msg)
    assert text.count("Ablageort: Garage\n") == 3
    assert "Bis bald.\n" in text
    _absent(msg, "gartenhaus", "seiteneingang", "vordach")


def test_sender_address_that_carries_the_recipient_is_replaced_as_a_whole(tmp_path):
    raw = _mail(
        "Holzwurm Shop <bounce+joerg.probst=gmx.de@holzwurm-shop.example>",
        "Deine Bestellung",
        "Antwort an bounce+joerg.probst=gmx.de@holzwurm-shop.example\n",
    )
    _, [(name, msg)] = _run(tmp_path, [raw])
    assert name == "001_holzwurm-shop.example.eml"
    assert str(msg["From"]) == "Holzwurm Shop <absender@holzwurm-shop.example>"
    assert "Antwort an max@example.org\n" in _text(msg)
    _absent(msg, "gmx", "bounce", "probst", "joerg")


# ----- 10: stale results -----

def test_stale_results_are_cleared_before_writing(tmp_path):
    src = tmp_path / "mails"
    src.mkdir()
    for i in range(3):
        (src / f"mail{i}.eml").write_bytes(_mail(SHOP, f"Paket {i}", "Unterwegs.\n"))
    done = _call(src, "--ohne-angaben", "--zip")
    assert done.returncode == 0, done.stderr
    out = src / "anonymisiert"
    assert len(list(out.glob("*.eml"))) == 3 and (out / "paket-tracker-beispiele.zip").is_file()
    (out / "notiz.txt").write_text("bleibt", encoding="utf-8")
    foreign = _mail(SHOP, "Fremde Mail", "Nicht vom Skript geschrieben.\n")
    (out / "009_fremd.eml").write_bytes(foreign)
    (src / "mail1.eml").unlink()
    (src / "mail2.eml").unlink()
    done = _call(src, "--ohne-angaben")
    assert done.returncode == 0, done.stderr
    assert sorted(p.name for p in out.iterdir()) == [
        "001_paketflitzer.example.eml", "009_fremd.eml", "notiz.txt",
    ]
    assert (out / "009_fremd.eml").read_bytes() == foreign


# ----- found in the real-world check: layouts of HTML mails, senders named in sentences -----

def test_sender_block_with_the_city_on_its_own_line(tmp_path):
    raw = _mail(
        "GLS Paket <no-reply@gls-pakete.de>",
        "Dein GLS Paket kommt heute!",
        "Versender\nHolzwurm Möbel GmbH\nSägeweg 3\n,\n98765\nSpanstadt\n"
        "Paketnummer\n12345678901\n"
        "Empfänger\nErika Sonnenschein\nAm Alten Hafen\n3,\n67890\nAnderswo\nZustellung\n"
        "Am Alten Hafen ist gesperrt.\n",
    )
    _, [(_, msg)] = _run(tmp_path, [raw])
    text = _text(msg)
    assert (
        "Versender\nHolzwurm Möbel GmbH\nBeispielweg 2\n00000 Beispielstadt\nPaketnummer\n" in text
    )
    assert "Empfänger\nMax Mustermann\nMusterstraße 1\n12345 Musterstadt\nZustellung\n" in text
    assert "Musterstraße ist gesperrt.\n" in text  # the street stands above its house number
    _absent(msg, "spanstadt", "98765", "sägeweg", "anderswo", "67890", "alten hafen")


def test_sender_named_in_a_carrier_sentence(tmp_path):
    raw = _mail(
        "UPS Quantum View <pkginfo@ups.com>",
        "UPS Versandbenachrichtigung",
        "Diese Nachricht kommt auf Antrag von Kirschbaum , um Sie zu informieren.\n"
        "Im Auftrag von Ballermann möchten wir Sie mit dieser Benachrichtigung informieren.\n"
        "This message was sent to you at the request of Donnerwetter to notify you.\n"
        "Auf Antrag von Holzwurm Möbel GmbH , um Sie zu informieren.\n"
        "Von: Kirschbaum\n\nKontrollnummer: 1Z999AA10123456784\n",
    )
    _, [(name, msg)] = _run(tmp_path, [raw])
    text = _text(msg)
    assert name == "001_ups.com.eml"
    assert "auf Antrag von Beispielversender , um Sie zu informieren.\n" in text
    assert "Im Auftrag von Beispielversender möchten wir Sie" in text
    assert "at the request of Beispielversender to notify you.\n" in text
    assert "Auf Antrag von Holzwurm Möbel GmbH , um Sie zu informieren.\n" in text
    assert "Von: Beispielversender\n" in text
    _absent(msg, "kirschbaum", "ballermann", "donnerwetter")


# ----- words glued together, a neighbour in front of the verb, numbers in brackets -----

def _html_mail(sender: str, subject: str, markup: str) -> bytes:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = "pakete@postfach.example"
    msg["Subject"] = subject
    msg["Date"] = DATE
    msg.set_content(f"<html><body>{markup}</body></html>", subtype="html")
    return bytes(msg)


GIVEN = ("--name", "Jonas Meier", "--street", "Lindenweg 3", "--postcode", "80331",
         "--city", "München")


def test_given_name_and_street_glued_to_the_next_word_are_replaced(tmp_path):
    """Inline elements of an HTML mail leave no space between a name and the next word."""
    glued = _html_mail(
        SHOP,
        "Ihre Bestellung",
        "<div>Herr <b>Meier</b><span>Ihre Bestellung wurde verschickt.</span></div>"
        "<div>Lieferadresse:<b>Jonas Meier</b><span>Lindenweg 3</span><span>80331 München</span>"
        "</div><div>Zustellung in <b>München</b><span>Deutschland</span></div>",
    )
    plain = _mail(
        SHOP,
        "Ihre Bestellung",
        "FrauMeierIhr Paket kommt in den LindenwegHeute.\n"
        "HERRMeier, JONAS MEIERLindenweg 3 in MÜNCHENDeutschland.\n",
    )
    done, [(_, first), (_, second)] = _run(tmp_path, [glued, plain], *GIVEN)
    markup = first.get_body(("html",)).get_content()
    assert "<p>Herr MustermannIhre Bestellung wurde verschickt.</p>" in markup
    # (house number and postcode are one run of digits there: invented as a whole)
    assert re.search(r"<p>Lieferadresse:Max MustermannMusterstraße \d{6} Musterstadt</p>", markup)
    assert "<p>Zustellung in MusterstadtDeutschland</p>" in markup
    assert "FrauMustermannIhr Paket kommt in den MusterstraßeHeute.\n" in _text(second)
    # in capitals: the last capital of a run starts the next word
    assert "HERRMustermann, Max MustermannMusterstraße 1 in MusterstadtDeutschland.\n" in (
        _text(second)
    )
    for msg in (first, second):
        _absent(msg, "jonas", "meier", "lindenweg", "80331", "münchen")
    assert "steht noch eine der eigenen Angaben" not in done.stdout


def test_learnt_name_glued_to_the_next_word_is_replaced(tmp_path):
    known = _mail(SHOP, "Ihre Bestellung", "Hallo Jonas Meier,\n\ndanke.\n")
    glued = _html_mail(
        SHOP, "Ihr Paket", "<p>Herr <b>Meier</b><span>Ihre Bestellung ist da.</span></p>"
    )
    _, [(_, first), (_, second)] = _run(tmp_path, [known, glued])
    assert "<p>Herr MustermannIhre Bestellung ist da.</p>" in (
        second.get_body(("html",)).get_content()
    )
    for msg in (first, second):
        _absent(msg, "jonas", "meier")


def test_a_short_name_is_still_no_part_of_an_ordinary_word(tmp_path):
    line = (
        "Das Paket steht daneben, die angegebene Adresse stimmt, ANGEGEBENE AUCH, "
        "angegebeneAdresse, Benedikt, Waldweg und der Buchenwald bleiben."
    )
    raw = _mail(SHOP, "Ihre Bestellung", f"Hallo Bene Wald,\n{line}\nBeneIhr Paket, HerrWald.\n")
    _, [(_, msg)] = _run(tmp_path, [raw], "--name", "Bene Wald")
    text = _text(msg)
    assert line + "\n" in text
    assert "MaxIhr Paket, HerrMustermann.\n" in text


def test_neighbour_named_in_front_of_the_verb(tmp_path):
    raw = _mail(
        "DHL Paket <noreply@dhl.de>",
        "Ihr Paket wurde zugestellt",
        "Wir haben sie bei Nowak (Ahornallee 5) abgegeben.\n"
        "Ihr Paket wurde bei Familie Kirschbaum abgegeben.\n"
        "Die Sendung wurde bei Ihrem Nachbarn Ballermann abgegeben.\n"
        "Ein Paket haben wir bei D. Donnerwetter (2. Etage) abgegeben.\n"
        "Wir haben sie bei Ihrem Nachbarn abgegeben.\n"
        "Wir haben sie bei der Filiale abgegeben.\n"
        "Wir haben sie bei Hermes PaketShop (Hauptplatz 5) abgegeben.\n"
        "Wir haben sie bei Paketshop Wolke abgegeben.\n"
        "Bei Abholung abgegeben.\n",
    )
    _, [(_, msg)] = _run(tmp_path, [raw])
    text = _text(msg)
    for line in (
        "Wir haben sie bei Beispielnachbar (Musterstraße 1) abgegeben.\n",
        "Ihr Paket wurde bei Familie Beispielnachbar abgegeben.\n",
        "Die Sendung wurde bei Ihrem Nachbarn Beispielnachbar abgegeben.\n",
        "Ein Paket haben wir bei Beispielnachbar (2. Etage) abgegeben.\n",
        "Wir haben sie bei Ihrem Nachbarn abgegeben.\n",
        "Wir haben sie bei der Filiale abgegeben.\n",
        "Wir haben sie bei Hermes PaketShop (Hauptplatz 5) abgegeben.\n",
        "Wir haben sie bei Paketshop Wolke abgegeben.\n",
        "Bei Abholung abgegeben.\n",
    ):
        assert line in text, line
    _absent(msg, "nowak", "ahornallee", "kirschbaum", "ballermann", "donnerwetter")


def test_order_number_in_brackets_is_no_phone_number(tmp_path):
    raw = _mail(
        SHOP,
        "Stornierung von Artikeln aus Ihrer Bestellung Nr. (028-1234567-7654321)",
        "Ihre Bestellung (028-1234567-7654321) und die bei eBay (02-12345-67890).\n"
        "Paket (01234567890123), Rückruf (0211) 123-45-67 oder (0151 12345678).\n"
        "Hotline (00 49) 151 1234567, Paket (0034 0434 1610 9401 2345).\n",
    )
    _, [(_, msg)] = _run(tmp_path, [raw])
    amazon, ebay = r"\((\d{3}-\d{7}-\d{7})\)", r"\((\d{2}-\d{5}-\d{5})\)"
    subject = " ".join(str(msg["Subject"]).split())
    [in_subject] = re.findall(rf"Bestellung Nr\. {amazon}$", subject)
    text = _text(msg)
    [in_text] = re.findall(rf"Ihre Bestellung {amazon} und die bei eBay {ebay}\.\n", text)
    assert in_text[0] == in_subject != "028-1234567-7654321"  # the same number in every place
    assert in_text[1] != "02-12345-67890"
    assert re.search(
        r"Paket \(\d{14}\), Rückruf \+49 000 0000000 oder \(\+49 000 0000000\)\.\n", text
    )
    # a bracket around the area code belongs to the phone number, one around a whole number stays
    assert re.search(r"Hotline \+49 000 0000000, Paket \(0034 0\d{3}(?: \d{4}){3}\)\.\n", text)
    # whole numbers only: invented digits may contain a short group by chance
    assert not re.search(r"(?<!\d)(?:1234567-7654321|12345-67890|01234567890123)(?!\d)", text)
    assert "0434 1610 9401 2345" not in text


# ----- more than 999 mails, a script without a terminal -----

def test_a_long_run_gets_longer_numbers_and_its_results_are_cleared_too(tmp_path):
    src = tmp_path / "mails"
    src.mkdir()
    for i in range(1000):
        (src / f"mail{i:04d}.eml").write_bytes(_mail(SHOP, f"Paket {i}", "Unterwegs.\n"))
    done = _call(src, "--ohne-angaben")
    assert done.returncode == 0, done.stderr
    out = src / "anonymisiert"
    names = sorted(p.name for p in out.iterdir())
    # sorted by name is sorted by number: "1000_…" does not stand in front of "101_…"
    assert names == [f"{number:04d}_paketflitzer.example.eml" for number in range(1, 1001)]
    last = email.message_from_bytes((out / names[-1]).read_bytes(), policy=policy.default)
    assert str(last["Message-ID"]) == "<beispiel-1000@example.org>"
    assert str(last["Subject"]) == "Paket 999"
    foreign = _mail(SHOP, "Fremde Mail", "Nicht vom Skript geschrieben.\n")
    (out / "1001_fremd.eml").write_bytes(foreign)
    for path in sorted(src.glob("*.eml"))[2:]:
        path.unlink()
    done = _call(src, "--ohne-angaben")
    assert done.returncode == 0, done.stderr
    assert sorted(p.name for p in out.iterdir()) == [
        "001_paketflitzer.example.eml", "002_paketflitzer.example.eml", "1001_fremd.eml",
    ]
    assert (out / "1001_fremd.eml").read_bytes() == foreign


# ----- --keep-filenames: the results are named like their sources -----

def _named(tmp_path: Path, names) -> Path:
    src = tmp_path / "mails"
    src.mkdir()
    for i, name in enumerate(names):
        (src / name).write_bytes(_mail(SHOP, f"Paket {i}", "Hallo Jörg Probst,\nunterwegs.\n"))
    return src


def test_keep_filenames_writes_each_mail_under_the_name_of_its_source(tmp_path):
    names = ["2023-07-18_0802.eml", "2023-07-18_0802_2.eml", "Dein Paket.EML"]
    src = _named(tmp_path, names)
    before = {name: (src / name).read_bytes() for name in names}
    done = _call(src, "--keep-filenames", "--name", "Jörg Probst", "--out", tmp_path / "ziel")
    assert done.returncode == 0, done.stderr
    out = tmp_path / "ziel"
    assert sorted(p.name for p in out.iterdir()) == names
    for number, name in enumerate(names, 1):
        msg = email.message_from_bytes((out / name).read_bytes(), policy=policy.default)
        assert str(msg["Message-ID"]) == f"<beispiel-{number:03d}@example.org>"
        assert str(msg["Subject"]) == f"Paket {number - 1}"
        _absent(msg, "probst")
        assert (src / name).read_bytes() == before[name]
    # the names of the sources may be private: no word about an upload, a warning instead
    assert "nicht zum Hochladen" in done.stdout
    assert "WICHTIG" not in done.stdout


def test_keep_filenames_writes_a_second_run_over_the_first(tmp_path):
    """The results of two runs stand under the same names: a rule change shows as a diff."""
    src = _named(tmp_path, ["a.eml", "b.eml"])
    out = src / "anonymisiert"
    assert _call(src, "--ohne-angaben", "--keep-filenames").returncode == 0
    (out / "a.eml").write_bytes(b"alt")
    assert _call(src, "--ohne-angaben", "--keep-filenames").returncode == 0
    assert sorted(p.name for p in out.iterdir()) == ["a.eml", "b.eml"]
    assert b"Message-ID: <beispiel-001@example.org>" in (out / "a.eml").read_bytes()


def test_keep_filenames_refuses_the_zip_and_two_mails_with_one_name(tmp_path):
    src = _named(tmp_path, ["a.eml"])
    other = tmp_path / "mehr"
    other.mkdir()
    (other / "A.eml").write_bytes(_mail(SHOP, "Paket", "Unterwegs.\n"))
    for args, message in (
        ((src, "--zip"), "nicht zusammen mit --zip"),
        ((src, other, "--out", tmp_path / "ziel"), "zwei der Mails heißen gleich"),
    ):
        done = _call(*args, "--ohne-angaben", "--keep-filenames")
        assert done.returncode == 2 and message in done.stderr, done.stderr
        assert done.stdout == ""
    assert not (src / "anonymisiert").exists() and not (tmp_path / "ziel").exists()


# ----- names that are learnt without any given value: closing formula, private sender -----

@pytest.mark.parametrize(
    "closing",
    [
        "Viele Grüße\nLena Holzer\n",
        "Kind regards / Viele Grüße\nLena Holzer\n",
        "Mit freundlichen Grüßen\n\nLena Holzer\n",
        "> Liebe Grüße,\n> Lena Holzer\n",
        "MfG\nLena Holzer\n",
    ],
)
def test_name_below_a_closing_formula_is_replaced_as_a_whole_in_every_mail(tmp_path, closing):
    raw = _mail(SHOP, "Ihre Frage", f"Das Paket ist unterwegs.\n\n{closing}")
    other = _mail(
        SHOP, "Paket unterwegs",
        "Holzer Dübel wurde versandt.\nRückfragen beantwortet Lena Holzer.\n\n"
        "Viele Grüße\nIhr Paketflitzer Team\n",
    )
    _, [(_, first), (_, second)] = _run(tmp_path, [raw, other])
    assert closing.replace("Lena Holzer", "Max Mustermann") in _text(first)
    # only the whole name: the surname of a shop's employee may be a brand in another mail
    assert "Holzer Dübel wurde versandt.\nRückfragen beantwortet Max Mustermann.\n" in _text(second)
    # "Liebe Grüße," greets nobody: the word of the closing formula is no name
    assert "Viele Grüße\nIhr Paketflitzer Team\n" in _text(second)


@pytest.mark.parametrize(
    "closing",
    [
        "Viele Grüße\nIhr Paketflitzer Team\n",
        "Mit freundlichen Grüßen\nIhr Sendungsstatus\n",
        "Viele Grüße\nLena vom Holzwurm Webshop Team\n",
        "Viele Grüße\nFiliale\nHauptstraße\n",
        "Viele Grüße aus dem Lager, wo Ihr Paket gerade für den Versand gepackt wird\nHolz Wurm\n",
    ],
)
def test_a_team_or_a_company_below_a_closing_formula_stays(tmp_path, closing):
    raw = _mail(SHOP, "Paket unterwegs", f"Ihr Paket ist unterwegs.\n\n{closing}")
    other = _mail(SHOP, "Zugestellt", "Der Holz Wurm hat das Paket der Filiale übergeben.\n")
    _, [(_, first), (_, second)] = _run(tmp_path, [raw, other])
    assert closing in _text(first)
    assert "Der Holz Wurm hat das Paket der Filiale übergeben." in _text(second)


FORWARDED = (
    "-------- Weitergeleitete Nachricht --------\n"
    "Von: DHL <noreply@dhl.de>\nGesendet: Dienstag, 18. Juli 2023 08:00\n{to}"
    "Betreff: Ihr Paket kommt\n\nGuten Tag,\n\nIhr Paket wurde von Probst angenommen.\n"
)


@pytest.mark.parametrize(
    ("sender", "subject", "text"),
    [
        # the address carries the name: the display name is not learnt as a private sender
        ("Jörg Probst <joerg.probst@gmx.de>", "Paket", "Wo bleibt das Paket für Probst?\n"),
        ("Jörg Probst <joerg@probst.example>", "Re: Ihre Frage", "Probst wartet noch.\n"),
        ("Jörg Probst <joerg@probst.example>", "WG: Ihr Paket kommt", FORWARDED.format(to="")),
        ("Jörg Probst <jp1987@gmx.de>", "WG: Ihr Paket kommt", FORWARDED.format(to="")),
        # no display name: the name stands only in the quoted header of the forwarded mail
        ("jp1987@gmx.de", "WG: Ihr Paket kommt",
         FORWARDED.format(to="An: Jörg Probst <jp1987@gmx.de>\n")),
        ("jp1987@gmx.de", "WG: Ihr Paket kommt",
         FORWARDED.format(to='An: "Probst, Jörg" <jp1987@gmx.de>; Erika Sonne <e@gmx.de>\n')),
    ],
)
def test_person_as_sender_or_quoted_recipient_is_learnt_with_each_word(
    tmp_path, sender, subject, text,
):
    other = _mail(SHOP, "Zugestellt", "Abgeholt von Probst.\nJörg hat unterschrieben.\n")
    _, [(_, first), (_, second)] = _run(tmp_path, [_mail(sender, subject, text), other])
    _absent(first, "probst", "jörg", "joerg")
    assert "Abgeholt von Mustermann.\nMax hat unterschrieben.\n" in _text(second)


def test_person_in_the_from_header_of_a_shop_keeps_its_words(tmp_path):
    """No mail of a person: only the whole name is a private sender, a word may be a brand."""
    raw = _mail("Lena Holzer <versand@kleinerladen.example>", "Dein Paket", "Ist unterwegs.\n")
    other = _mail(SHOP, "Paket unterwegs", "Holzer Dübel wurde versandt, sagt Lena Holzer.\n")
    _, [_, (_, second)] = _run(tmp_path, [raw, other])
    assert "Holzer Dübel wurde versandt, sagt Beispielversender.\n" in _text(second)


# ----- the tester's own domain -----

OWN_SITE = (
    "Ihr Konto: https://www.probst-holzbau.example/kunden/4711 oder probst-holzbau.example.\n"
    "Verfolgen: https://paketflitzer.example/track/1 oder www.paketflitzer.example.\n"
)


def test_domain_the_mails_were_sent_to_is_replaced_in_every_mail(tmp_path):
    raw = _mail(SHOP, "Paket unterwegs", OWN_SITE, to="jp@probst-holzbau.example")
    other = _mail(HERMES, "Zugestellt", "Abgegeben bei www.probst-holzbau.example.\n")
    _, [(_, first), (_, second)] = _run(tmp_path, [raw, other])
    assert "Ihr Konto: https://example.org/… oder example.org.\n" in _text(first)
    # the shop's own hosts stay: the importer tells a shop by them
    assert "https://paketflitzer.example/… oder www.paketflitzer.example.\n" in _text(first)
    assert "Abgegeben bei example.org.\n" in _text(second)


@pytest.mark.parametrize(
    "to",
    [
        "jp1987@gmx.de",  # a provider's domain is nobody's own
        "kunden@paketflitzer.example",  # the shop wrote to itself, the tester in blind copy
    ],
)
def test_domain_of_a_provider_or_of_the_sender_is_not_the_testers_own(tmp_path, to):
    text = "Mehr auf www.gmx.de und https://paketflitzer.example/track/1.\n"
    _, [(_, msg)] = _run(tmp_path, [_mail(SHOP, "Paket unterwegs", text, to=to)])
    assert "Mehr auf www.gmx.de und https://paketflitzer.example/…\n" in _text(msg)


def test_domain_of_a_given_mail_address_is_replaced(tmp_path):
    raw = _mail(SHOP, "Paket unterwegs", OWN_SITE, to="pakete@postfach.example")
    _, [(_, msg)] = _run(tmp_path, [raw], "--email", "jp@probst-holzbau.example")
    assert "Ihr Konto: https://example.org/… oder example.org.\n" in _text(msg)
    assert "www.paketflitzer.example" in _text(msg)


def test_web_address_in_the_signature_of_a_person_is_replaced(tmp_path):
    signature = "Viele Grüße\nJörg Probst\njp@probst-holzbau.example\nwww.probst-holzbau.example\n"
    raw = _mail(
        "Jörg Probst <jp@probst-holzbau.example>", "Re: Ihre Frage",
        f"Wo bleibt mein Paket?\n\n{signature}\nMehr dazu auf www.paketflitzer.example\n",
        to="service@paketflitzer.example",
    )
    team = _mail(
        SHOP, "Paket unterwegs",
        "Ist unterwegs.\n\nViele Grüße\nIhr Paketflitzer Team\nwww.paketflitzer.example\n",
    )
    _, [(_, first), (_, second)] = _run(tmp_path, [raw, team])
    assert "Viele Grüße\nMax Mustermann\nmax@example.org\nwww.example.org\n" in _text(first)
    # only in the signature of a person: no line of the text, no signature of a team
    assert "Mehr dazu auf www.paketflitzer.example\n" in _text(first)
    assert "Viele Grüße\nIhr Paketflitzer Team\nwww.paketflitzer.example\n" in _text(second)
