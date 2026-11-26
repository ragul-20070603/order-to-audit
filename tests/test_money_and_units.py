from decimal import Decimal

import pytest

import core


def test_gst_split_intra_and_inter():
    assert core.gst_split(750000, 500, False) == (18750, 18750, 0)
    assert core.gst_split(750000, 500, True) == (0, 0, 37500)


def test_gst_split_odd_paise_always_adds_up():
    for taxable in (101, 333, 9999, 123457):
        c, s, i = core.gst_split(taxable, 1800, False)
        assert c + s == core.rnd(Decimal(taxable) * 1800 / 10000) and i == 0


def test_money_has_no_float_drift():
    assert core.paise("0.1") + core.paise("0.2") == core.paise("0.3")
    assert core.paise("2.50") == 250
    assert str(core.rs(787500)) == "7875.00"


def test_quantity_round_trip():
    assert core.to_milli("10.5") == 10500
    assert core.fmt_qty(40000) == "40" and core.fmt_qty(1200) == "1.2"
    with pytest.raises(ValueError):
        core.to_milli("0.0001")


def test_unit_conversion():
    assert core.convert(500, "g", "kg") == Decimal("0.5")
    assert core.convert(2, "l", "ml") == 2000
    assert core.convert(3, "carton", "packet") == 60
    assert core.convert(40, "packet", "carton") == 2
    with pytest.raises(core.BusinessError):
        core.convert(1, "kg", "l")
    with pytest.raises(core.BusinessError):
        core.convert(1, "piece", "packet")


def test_financial_year():
    assert core.fy_of("2026-11-26") == "2026-27"
    assert core.fy_of("2027-03-31") == "2026-27"
    assert core.fy_of("2027-04-01") == "2027-28"


def test_amount_in_words():
    assert core.amount_in_words(787500) == "Rupees Seven Thousand Eight Hundred Seventy Five Only"
    assert core.amount_in_words(12345650) == "Rupees One Lakh Twenty Three Thousand Four Hundred Fifty Six and Fifty Paise Only"
