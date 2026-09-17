"""Format validation for DNI / NIE / matrícula, independent from the BOE
exact-match check (see pdf_parser.normalize for the comparison normalizer).
"""
from __future__ import annotations

import re

_DNI_RE = re.compile(r"^\d{8}[A-Z]$")
_NIE_RE = re.compile(r"^[XYZ]\d{7}[A-Z]$")
_PLATE_RE = re.compile(r"^\d{4}[BCDFGHJKLMNPRSTVWXYZ]{3}$")

_DNI_LETTERS = "TRWAGMYFPDXBNJZSQVHLCKE"


class InvalidIdentifier(ValueError):
    pass


def _clean(value: str) -> str:
    return re.sub(r"[\s\-]", "", value or "").upper()


def validate_identifier(value: str) -> str:
    """Validate that `value` looks like a DNI, NIE or vehicle plate, and
    return it normalized. Raises InvalidIdentifier otherwise."""
    cleaned = _clean(value)

    if _DNI_RE.match(cleaned):
        digits, letter = cleaned[:-1], cleaned[-1]
        expected = _DNI_LETTERS[int(digits) % 23]
        if letter != expected:
            raise InvalidIdentifier("La letra del DNI no es válida")
        return cleaned

    if _NIE_RE.match(cleaned):
        return cleaned

    if _PLATE_RE.match(cleaned):
        return cleaned

    raise InvalidIdentifier("Formato no reconocido. Usa un DNI, NIE o matrícula válidos")
