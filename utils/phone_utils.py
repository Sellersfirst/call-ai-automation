import logging
import re

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(funcName)s | %(message)s"
)
def remove_plus(phone):
    cleaned = phone.lstrip("+")
    logging.info(f"Removed plus: {phone} -> {cleaned}")
    return cleaned


def digits_only(phone) -> str:
    """Strip everything but digits from a raw phone value (e.g. sheet cell text)."""
    return re.sub(r"\D", "", str(phone or ""))


def phones_match(a, b) -> bool:
    """
    Compare two phone numbers ignoring formatting (spaces, dashes, parens, '+')
    and an optional leading country code, by comparing the last 10 digits.
    Used to match a raw sheet value against a call's phone number.
    """
    da, db = digits_only(a), digits_only(b)
    if len(da) < 10 or len(db) < 10:
        return False
    return da[-10:] == db[-10:]


