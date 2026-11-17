"""Genre operations (pure). Plan section 1.3 and the M3 test list."""
import pytest

from gama.metadata.genres import (MAX_REPLACE, GenreError, apply, parse_field, validate_genre, validate_request)

REFS = {"(17)": "Rock", "(18)": "Techno"}


def resolve(v):
    return REFS.get(v, v)


def F(*values):
    return parse_field(list(values), resolve=resolve)


# --------------------------------------------------------------------------- add

def test_add_to_existing_genres_appends():
    r = apply(F("Rock", "Blues"), "add", ["Jazz"])
    assert r.changed and r.values == ("Rock", "Blues", "Jazz") and r.notes == ()


def test_add_existing_exact_is_nochange():
    r = apply(F("Rock", "Blues"), "add", ["Rock"])
    assert not r.changed and r.values == ("Rock", "Blues") and r.notes == ("already-present",)


def test_add_existing_different_case_is_nochange_with_note():
    r = apply(F("Rock", "Blues"), "add", ["  rock "])
    assert not r.changed and r.values == ("Rock", "Blues") and r.notes == ("case-differs",)


def test_add_to_empty():
    r = apply(F(), "add", ["Jazz"])
    assert r.changed and r.values == ("Jazz",)


def test_add_keeps_unrelated_components_exactly():
    f = F("rock ", "", "Pop", "Pop")
    r = apply(f, "add", ["Jazz"])
    assert r.values == ("rock ", "", "Pop", "Pop", "Jazz")


def test_add_trims_input_but_keeps_spelling():
    assert apply(F("Rock"), "add", ["  Neo-Prog  "]).values == ("Rock", "Neo-Prog")


@pytest.mark.parametrize("value,delim", [("Rock;Progressive Rock", ";"), ("Rock; Progressive Rock", "; "),
                                         ("Rock ; Progressive Rock", " ; ")])
def test_add_to_delimited_keeps_delimiter_and_spacing(value, delim):
    r = apply(F(value), "add", ["Jazz"])
    assert r.values == (f"Rock{delim}Progressive Rock{delim}Jazz",)
    assert r.components == ("Rock", "Progressive Rock", "Jazz")


def test_add_existing_to_delimited_is_nochange():
    r = apply(F("Rock;Progressive Rock"), "add", ["progressive rock"])
    assert not r.changed and r.notes == ("case-differs",)


# --------------------------------------------------------------------------- remove

def test_remove_nonexistent_is_nochange():
    r = apply(F("Rock", "Blues"), "remove", ["Jazz"])
    assert not r.changed and r.values == ("Rock", "Blues") and r.notes == ("not-present",)


def test_remove_from_empty_is_nochange():
    assert not apply(F(), "remove", ["Jazz"]).changed


def test_remove_all_case_variants_and_duplicates():
    r = apply(F("Rock", "rock", "Jazz", "ROCK ", "Rock"), "remove", ["rOcK"])
    assert r.changed and r.values == ("Jazz",)


def test_remove_last_deletes_field():
    r = apply(F("Rock"), "remove", ["Rock"])
    assert r.changed and r.values == () and r.notes == ("field-deleted",)


def test_remove_from_delimited():
    assert apply(F("Rock; Pop; Jazz"), "remove", ["pop"]).values == ("Rock; Jazz",)
    assert apply(F("Rock;Pop"), "remove", ["Pop"]).values == ("Rock",)
    r = apply(F("Rock;rock"), "remove", ["ROCK"])
    assert r.values == () and r.notes == ("field-deleted",)


def test_remove_keeps_unrelated_odd_values():
    assert apply(F("", "Pop", "pop ", "Jazz"), "remove", ["Jazz"]).values == ("", "Pop", "pop ")


# --------------------------------------------------------------------------- replace

def test_replace_sets_exact_list():
    r = apply(F("Rock", "Blues"), "replace", ["Jazz", "Fusion"])
    assert r.changed and r.values == ("Jazz", "Fusion")


def test_replace_dedupes_input_first_spelling_wins():
    r = apply(F("Rock"), "replace", ["Jazz", "jazz", "Fusion", "JAZZ "])
    assert r.values == ("Jazz", "Fusion")


def test_replace_equal_to_current_is_nochange():
    r = apply(F("Jazz", "Fusion"), "replace", ["Jazz", "Fusion"])
    assert not r.changed


def test_replace_different_order_or_case_is_a_change():
    assert apply(F("Jazz", "Fusion"), "replace", ["Fusion", "Jazz"]).changed
    assert apply(F("Jazz"), "replace", ["jazz"]).changed


def test_replace_cleans_duplicates_only_when_asked():
    assert apply(F("Rock", "rock"), "replace", ["Rock"]).values == ("Rock",)


def test_replace_keeps_delimited_convention():
    r = apply(F("Rock; Pop"), "replace", ["Jazz", "Fusion"])
    assert r.values == ("Jazz; Fusion",)
    assert apply(F("Rock;Pop"), "replace", ["Jazz"]).values == ("Jazz",)


def test_replace_on_empty():
    assert apply(F(), "replace", ["Jazz"]).values == ("Jazz",)


# --------------------------------------------------------------------------- multiple genres / numeric refs / mixed

def test_multiple_genres_on_one_track():
    f = F("Rock", "Progressive Rock", "Art Rock")
    assert apply(f, "add", ["Jazz"]).values == ("Rock", "Progressive Rock", "Art Rock", "Jazz")
    assert apply(f, "remove", ["progressive rock"]).values == ("Rock", "Art Rock")
    assert not apply(f, "add", ["ART ROCK"]).changed


def test_numeric_reference_kept_by_add_and_remove_of_others():
    f = F("(17)", "Jazz")
    assert f.components == ("Rock", "Jazz")
    r = apply(f, "add", ["Pop"])
    assert r.values == ("(17)", "Jazz", "Pop") and r.notes == ()
    assert apply(f, "remove", ["Jazz"]).values == ("(17)",)
    assert not apply(f, "add", ["rock"]).changed


def test_numeric_reference_removed_by_name():
    r = apply(F("(17)", "Jazz"), "remove", ["Rock"])
    assert r.values == ("Jazz",) and "numeric-reference-stored-as-name" not in r.notes


def test_replace_stores_name_instead_of_reference():
    r = apply(F("(17)", "Jazz"), "replace", ["Rock", "Jazz"])
    assert r.changed and r.values == ("Rock", "Jazz")
    assert "numeric-reference-stored-as-name" in r.notes
    r2 = apply(F("(17)"), "replace", ["Pop"])
    assert "numeric-reference-stored-as-name" not in r2.notes


def test_mixed_is_never_edited():
    with pytest.raises(GenreError):
        apply(F("Rock;Pop", "Jazz"), "add", ["Blues"])


# --------------------------------------------------------------------------- validation

@pytest.mark.parametrize("bad", ["", "   ", "Rock;Pop", "a\x00b", "a\nb", "tab\there", "x\x1f", "x\x7f", "Rock\n",
                                 "x" * 256, None, 17])
def test_invalid_genres(bad):
    with pytest.raises(GenreError):
        validate_genre(bad)


def test_valid_genre_limits():
    assert validate_genre("x" * 255) == "x" * 255
    assert validate_genre(" Ünïcødé – Rock/Pop, R&B ") == "Ünïcødé – Rock/Pop, R&B"


@pytest.mark.parametrize("op,genres", [("add", []), ("add", ["a", "b"]), ("remove", []), ("replace", []),
                                       ("rename", ["a"]), ("replace", [f"g{i}" for i in range(MAX_REPLACE + 1)])])
def test_invalid_requests(op, genres):
    with pytest.raises(GenreError):
        validate_request(op, genres)


def test_replace_limit_counts_after_dedupe():
    gs = [f"g{i}" for i in range(MAX_REPLACE)] + ["G0"]
    assert len(validate_request("replace", gs)) == MAX_REPLACE


def test_apply_validates_input():
    with pytest.raises(GenreError):
        apply(F("Rock"), "add", ["Rock;Pop"])


# --------------------------------------------------------------------------- idempotence (table-driven)

FIELDS = [(), ("Rock",), ("Rock", "Blues"), ("rock", "Rock", "Jazz"), ("Rock;Progressive Rock",),
          ("Rock; Pop; Jazz",), ("(17)", "Jazz"), ("", "Pop"), ("Jazz",)]
REQUESTS = [("add", ["Jazz"]), ("add", ["ROCK"]), ("add", ["Progressive Rock"]), ("remove", ["Rock"]),
            ("remove", ["jazz"]), ("remove", ["Nope"]), ("replace", ["Jazz", "Fusion"]), ("replace", ["Rock"]),
            ("replace", ["Pop", "pop", "Rock"])]


@pytest.mark.parametrize("values", FIELDS, ids=[repr(f) for f in FIELDS])
@pytest.mark.parametrize("op,genres", REQUESTS, ids=[f"{o}:{g}" for o, g in REQUESTS])
def test_idempotence(values, op, genres):
    once = apply(F(*values), op, genres)
    # Planning the same operation on the stored result gives no change.
    twice = apply(F(*once.values), op, genres)
    assert not twice.changed, (once, twice)
    assert twice.values == once.values
    assert F(*once.values).components == once.components
