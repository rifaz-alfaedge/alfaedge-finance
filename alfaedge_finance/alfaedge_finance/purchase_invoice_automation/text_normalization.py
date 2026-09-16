"""Normalization for matching supplier line-item text against Purchase Item
Mapping, so trivial LLM re-wording (extra whitespace, casing) between two
extractions of what is otherwise the same line item doesn't look unmapped.

This only affects the *lookup/storage key* in Purchase Item Mapping - the
item_name actually shown on a Purchase Invoice/Purchase Expense Center keeps
its original extracted casing and spacing.
"""

import re

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_for_matching(text: str) -> str:
	if not text:
		return text
	return _WHITESPACE_RE.sub(" ", text.strip()).lower()
