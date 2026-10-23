"""17-character vehicle identification numbers with the ISO 3779 / 49 CFR 565
check digit (position 9). Pure functions; no Frappe imports.

Layout used for synthetic vehicles:
    1-8   prefix (WMI + VDS) from Vehicle Model.vin_prefix  (synthetic, not an assigned WMI)
    9     check digit
    10    model year code
    11    plant code
    12-17 sequential production number
"""

from __future__ import annotations

TRANSLITERATION = {
	**{str(d): d for d in range(10)},
	"A": 1, "B": 2, "C": 3, "D": 4, "E": 5, "F": 6, "G": 7, "H": 8,
	"J": 1, "K": 2, "L": 3, "M": 4, "N": 5, "P": 7, "R": 9,
	"S": 2, "T": 3, "U": 4, "V": 5, "W": 6, "X": 7, "Y": 8, "Z": 9,
}  # fmt: skip
WEIGHTS = (8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2)
# 30-year cycle; I, O, Q, U, Z and 0 are never used as year codes.
YEAR_CODES = "ABCDEFGHJKLMNPRSTVWXY123456789"


class VinError(ValueError):
	pass


def model_year_code(year: int) -> str:
	return YEAR_CODES[(year - 2010) % 30]


def check_digit(vin_without_check: str) -> str:
	"""vin_without_check: the 17 characters with any placeholder at position 9."""
	if len(vin_without_check) != 17:
		raise VinError("VIN must have 17 characters")
	total = 0
	for position, char in enumerate(vin_without_check.upper()):
		if position == 8:
			continue
		if char not in TRANSLITERATION:
			raise VinError(f"invalid VIN character {char!r} at position {position + 1}")
		total += TRANSLITERATION[char] * WEIGHTS[position]
	remainder = total % 11
	return "X" if remainder == 10 else str(remainder)


def build(prefix: str, model_year: int, plant_code: str, sequence: int) -> str:
	prefix = prefix.upper()
	if len(prefix) != 8:
		raise VinError("VIN prefix must be 8 characters (WMI + VDS)")
	if len(plant_code) != 1 or plant_code.upper() not in TRANSLITERATION:
		raise VinError("plant code must be one valid VIN character")
	if not 0 <= sequence <= 999_999:
		raise VinError("production sequence must fit in 6 digits")
	body = f"{prefix}0{model_year_code(model_year)}{plant_code.upper()}{sequence:06d}"
	return body[:8] + check_digit(body) + body[9:]


def is_valid(vin: str) -> bool:
	try:
		return len(vin) == 17 and check_digit(vin) == vin[8].upper()
	except VinError:
		return False
