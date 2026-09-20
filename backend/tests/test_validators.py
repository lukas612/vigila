import pytest

from app.validators import InvalidIdentifier, validate_identifier


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("12345678Z", "12345678Z"),
        ("12345678-z", "12345678Z"),
        ("x1234567l", "X1234567L"),
        ("1234 bcd", "1234BCD"),
        ("M-1234-AB", "M1234AB"),
        ("b1234cd", "B1234CD"),
        ("ss-1234-a", "SS1234A"),
    ],
)
def test_validate_identifier_accepts_valid_formats(raw, expected):
    assert validate_identifier(raw) == expected


def test_validate_identifier_rejects_bad_dni_check_letter():
    with pytest.raises(InvalidIdentifier):
        validate_identifier("12345678A")


def test_validate_identifier_rejects_garbage():
    with pytest.raises(InvalidIdentifier):
        validate_identifier("not-an-id")
