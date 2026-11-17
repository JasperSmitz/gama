import pytest

from gama.metadata.genres import delimited_round_trips, has_duplicates, has_empty, key, parse_field


def test_empty():
    g = parse_field([])
    assert (g.style, g.values, g.components, g.delimiter) == ("empty", (), (), None)


def test_native_multi():
    g = parse_field(["Rock", "Progressive Rock"])
    assert g.style == "native" and g.components == ("Rock", "Progressive Rock")


@pytest.mark.parametrize("value,delim,comps", [
    ("Rock;Progressive Rock", ";", ("Rock", "Progressive Rock")),
    ("Rock; Progressive Rock; Jazz", "; ", ("Rock", "Progressive Rock", "Jazz")),
    ("Rock ; Pop", " ; ", ("Rock", "Pop")),
])
def test_delimited_keeps_spacing(value, delim, comps):
    g = parse_field([value])
    assert (g.style, g.delimiter, g.components, g.values) == ("delimited", delim, comps, (value,))
    assert delimited_round_trips(g)


@pytest.mark.parametrize("value", ["Rock;Pop; Jazz", " Rock;Pop", "Rock;Pop "])
def test_irregular_delimited_does_not_round_trip(value):
    assert not delimited_round_trips(parse_field([value]))


def test_mixed():
    g = parse_field(["Rock;Pop", "Jazz"])
    assert g.style == "mixed"


@pytest.mark.parametrize("value", ["AC/DC", "R&B/Soul", "Rock, Pop", "Rock/Post-Punk"])
def test_slash_and_comma_never_split(value):
    g = parse_field([value])
    assert g.style == "native" and g.components == (value,)


def test_resolver_only_changes_components():
    g = parse_field(["(17)", "Jazz"], resolve=lambda v: "Rock" if v == "(17)" else v)
    assert g.values == ("(17)", "Jazz") and g.components == ("Rock", "Jazz")


def test_key():
    assert key("  Progressive   ROCK ") == key("progressive rock")
    assert key("Café") == key("Café")
    assert key("STRASSE") == key("straße")


def test_duplicates_and_empty():
    assert has_duplicates(parse_field(["Rock", "rock"]))
    assert not has_duplicates(parse_field(["Rock", "Pop"]))
    assert has_empty(parse_field(["Rock", ""]))
    assert has_empty(parse_field(["Rock;;Pop"]))
