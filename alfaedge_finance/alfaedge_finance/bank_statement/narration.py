"""Pull a transaction reference and a counterparty out of a bank narration.

Axis narrations are "/"-separated, with the layout depending on the channel prefix:

    POS/FACEBK* VRWBZF9G6/+35315530550/010426/23:00/609123083089     card: merchant, ..., RRN
    ECOM PUR/AMAZON PAY IN/1246624801/080426/05:50/609705127358     card: merchant, ..., RRN
    NEFT/IN42609256645836/AL/TAZA SPARTA/ICICI BANK LIMITED/...     inward NEFT: UTR, name
    NEFT/HDFCH00927812316/ABRECO MOTORS PRIVATE LIMITE/HDFC BANK/.. inward NEFT: UTR, name
    NEFT/260001718903/14/AW0003809429/                              Axis bulk upload: batch ref
    INB/NEFT/AXODH12041487058/C LOUNGE BUSINESS CEN/INDUSIND BANK/  internet banking NEFT
    INB/950195056/GST TAX PAYMENT/                                  internet banking: ref, purpose
    IMPS/P2A/610610160765/ANSONCHI/FEDERALB/DYNAMICS/...            IMPS: RRN, name
    RTGS/HDFCR52025113089405053/BODHYRAJ P/HDFC BANK///...          RTGS: UTR, name
    BRN/REF NO.0081FIR2601568 USD 15160.80/RLZ                      branch (foreign inward)
    VISA MERCH Refund /23/APR/26/GOOGLE CLOUD                       card refund: merchant last

The counterparty is only a hint for suggestions; nothing is matched on it fuzzily.
"""

import re
from dataclasses import dataclass

_BRN_REF_RE = re.compile(r"REF\s*NO\.?\s*([A-Z0-9]+)", re.IGNORECASE)
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")

BULK_UPLOAD = "AXIS BULK UPLOAD"


@dataclass
class Narration:
	channel: str
	reference: str | None
	counterparty: str | None


def _segment(parts, index):
	if index < len(parts):
		value = parts[index].strip()
		return value or None
	return None


def parse_narration(text: str) -> Narration:
	text = (text or "").strip()
	parts = text.split("/")
	head = parts[0].strip().upper()

	if head in ("POS", "ECOM PUR"):
		reference = next((p.strip() for p in reversed(parts) if p.strip()), None)
		return Narration("Card", reference if reference != parts[0].strip() else None, _segment(parts, 1))

	if "REFUND" in head:
		return Narration("Card Refund", None, next((p.strip() for p in reversed(parts) if p.strip()), None))

	if head == "INB":
		sub = (_segment(parts, 1) or "").upper()
		if sub in ("NEFT", "RTGS", "IMPS"):
			return Narration(sub, _segment(parts, 2), _segment(parts, 3))
		if sub == "IFT":
			# INB/IFT/<beneficiary>/TPARTY TRANSFER - an intra-bank transfer, no reference.
			return Narration("IFT", None, _segment(parts, 2))
		return Narration("Net Banking", _segment(parts, 1), _segment(parts, 2))

	if head in ("NEFT", "RTGS"):
		reference = _segment(parts, 1)
		third, fourth = _segment(parts, 2), _segment(parts, 3) or ""
		if third and third.isdigit() and fourth.upper().startswith("AW"):
			return Narration("Bulk Upload", reference, BULK_UPLOAD)
		# "NEFT/IN4260.../AL/TAZA SPARTA/..." - a short segment is part of a split name.
		if third and len(third) <= 3 and _segment(parts, 3):
			return Narration(head, reference, f"{third} {_segment(parts, 3)}")
		return Narration(head, reference, third)

	if head in ("IMPS", "UPI"):
		if (_segment(parts, 1) or "").upper() in ("P2A", "P2P", "P2M", "MMT"):
			return Narration(head, _segment(parts, 2), _segment(parts, 3))
		return Narration(head, _segment(parts, 1), _segment(parts, 2))

	if head.startswith("BRN"):
		match = _BRN_REF_RE.search(text)
		return Narration("Branch", match.group(1) if match else None, None)

	if head == "CLG":
		# CLG/<cheque no>/<ddmmyy>/<drawee bank>
		return Narration("Cheque", _segment(parts, 1), _segment(parts, 3))

	# Bank charges and other free-text lines ("Monthly Service Chrgs"): the text itself
	# is the only stable identifier, so it doubles as the counterparty for rules.
	return Narration("Other", None, text or None)


def counterparty_key(counterparty: str | None) -> str | None:
	"""Stable lookup key for a counterparty: lowercase alphanumerics, single-spaced.
	Card merchants carry a per-transaction code after '*' ("FACEBK* VRWBZF9G6"),
	which is dropped so every charge from the same merchant shares one key."""
	if not counterparty:
		return None
	text = counterparty.split("*")[0]
	key = _NON_ALNUM_RE.sub(" ", text.lower()).strip()
	key = re.sub(r"\s+", " ", key)
	return key or None


def compact(text: str | None) -> str:
	"""Alphanumerics only, lowercase - for comparing bank-truncated names such as
	'ANSONCHI' against 'Anson Chits India Private Limited'."""
	return _NON_ALNUM_RE.sub("", (text or "").lower())


def clean_reference(reference: str | None) -> str:
	return re.sub(r"[^A-Z0-9]", "", (reference or "").upper())


def references_match(statement_ref: str | None, voucher_ref: str | None) -> bool:
	"""Equal, or one contains the other with at least 8 characters on the shorter side -
	covers a bank prefix ('IN42609256645836' vs '42609256645836') and a truncated
	reference typed into a voucher ('0081FIR260178' vs '0081FIR2601782')."""
	a, b = clean_reference(statement_ref), clean_reference(voucher_ref)
	if not a or not b:
		return False
	if a == b:
		return True
	shorter, longer = sorted((a, b), key=len)
	return len(shorter) >= 8 and shorter in longer


def looks_like_reference(value: str | None) -> bool:
	cleaned = clean_reference(value)
	return len(cleaned) >= 6 and any(ch.isdigit() for ch in cleaned)
