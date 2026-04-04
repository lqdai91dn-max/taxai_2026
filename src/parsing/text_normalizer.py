"""
Vietnamese OCR Text Normalizer

Fixes OCR artifacts from PDF text extraction (pdfplumber + font encoding issues):

1. MERGED WORDS — spaces dropped between syllables
   VD: "cánhân" → "cá nhân", "tronglãnhthổ" → "trong lãnh thổ"
   Root cause: pdfplumber infers word spacing from glyph positions; certain
   embedded fonts have zero advance-width for space → inter-word gaps invisible.

2. ACCENT DROPS (in principle, handled via patch files for now)
   VD: "thuê suất" → "thuế suất" (ê/ệ font mapping)

Vietnamese syllable structure (used for boundary detection):
  [Initial] + Nucleus + [Final]
  - Initial  : consonant cluster (nh th ph ch tr gi kh ng ngh qu) or single cons
  - Nucleus  : vowel (a ă â e ê i o ô ơ u ư y) with tone mark
  - Final    : n m p t c ng nh ch  (or empty for open syllable)
"""

import re
import unicodedata
from typing import Optional


# ---------------------------------------------------------------------------
# Vietnamese phonology constants
# ---------------------------------------------------------------------------

# Ordered longest-first so the regex tries digraphs before single chars.
# "ngh" must precede "ng" and "nh"; "gh" before "g"; "gi" before "g".
_VN_INITIALS = (
    'ngh', 'gh', 'gi', 'ng',
    'nh', 'th', 'ph', 'ch', 'tr', 'kh', 'qu',
    'b', 'c', 'd', 'đ', 'g', 'h', 'k', 'l', 'm',
    'n', 'p', 'r', 's', 't', 'v', 'x', 'y',
)

# Pattern: any char that CAN legally end a Vietnamese syllable.
# Includes tone-marked vowels (via Unicode category) AND final consonants.
_FINAL_CONSONANTS = set('nmpct')

# Vowel chars (base, no tone) used as nucleus or open-syllable ending.
_BASE_VOWELS = set('aăâeêioôơuưy')

# Regex: a "word token" — run of non-space chars
_TOKEN_RE = re.compile(r'\S+')

# Regex: initial consonant cluster (case-insensitive, longest match first)
_INITIAL_RE = re.compile(
    '(?:' + '|'.join(re.escape(i) for i in _VN_INITIALS) + ')',
    re.IGNORECASE | re.UNICODE,
)

# Minimum token length to attempt split (single syllables ≤ 4 NFC chars)
_MIN_MERGE_LEN = 5


# ---------------------------------------------------------------------------
# Core logic: detect syllable boundary in a merged token
# ---------------------------------------------------------------------------

def _is_vowel_char(ch: str) -> bool:
    """True if ch is a Vietnamese vowel (with or without tone mark)."""
    # Decompose to get base character
    base = unicodedata.normalize('NFD', ch)[0].lower()
    return base in _BASE_VOWELS


# 2-char final consonant groups in Vietnamese: 'ng', 'nh', 'ch'
_FINAL_DIGRAPHS = {'ng', 'nh', 'ch'}


def _prev_ends_syllable(token: str, i: int) -> bool:
    """Return True if token[:i] could be the end of a Vietnamese syllable.

    Handles:
      - Open syllable ending with a vowel (e.g., 'cá', 'thu')
      - Single-char final consonant: n m p t c
      - Two-char final digraph:  ng  nh  ch   (e.g., 'trong', 'lãnh')
    """
    if i < 1:
        return False
    prev1 = token[i - 1]
    if _is_vowel_char(prev1):
        return True
    if prev1.lower() in _FINAL_CONSONANTS:
        return True
    if i >= 2 and token[i - 2:i].lower() in _FINAL_DIGRAPHS:
        # Guard: final digraph (ng/nh/ch) must have a vowel nucleus before it.
        # Without a preceding vowel it's an initial cluster (e.g. "ngh" in "nghiệp"),
        # not a syllable-final digraph — so we must NOT split here.
        if any(_is_vowel_char(c) for c in token[:i - 2]):
            return True
        return False
    return False


def _find_split_pos(token: str) -> Optional[int]:
    """
    Find the position (exclusive end of first syllable) where a space should
    be inserted in a merged two-syllable token.

    Strategy:
      Scan left-to-right for positions where:
        a) token[:i] ends a valid Vietnamese syllable
        b) token[i:] starts a valid Vietnamese initial cluster followed by a vowel

    Returns the split position, or None if no split found.
    """
    n = len(token)
    if n < _MIN_MERGE_LEN:
        return None

    # Try each position i (split = token[:i] + ' ' + token[i:])
    # Start from 2 (minimum first-syllable length) to n-2
    for i in range(2, n - 1):
        # Condition A: token[:i] must end a valid Vietnamese syllable
        if not _prev_ends_syllable(token, i):
            continue

        # Condition B: current position starts a valid Vietnamese initial
        suffix = token[i:]
        m = _INITIAL_RE.match(suffix)
        if m is None:
            continue

        initial = m.group(0).lower()

        # Guard: 'y' after 'u' or 'o' is a DIPHTHONG GLIDE, not a new initial.
        # VD: 'chuyển' = ch-uy-ê-n (one syllable) — prev 'u' + initial 'y' = diphthong.
        if initial == 'y' and token[i - 1].lower() in ('u', 'o'):
            continue

        # After the initial there must be at least one more char (the vowel nucleus)
        rest_after_initial = suffix[len(initial):]
        if not rest_after_initial:
            continue
        if not _is_vowel_char(rest_after_initial[0]):
            continue

        return i

    return None


def _fix_merged_token(token: str) -> str:
    """
    Recursively split a merged token until no more splits are found.
    VD: "tronglãnhthổ" → "trong lãnh thổ" (two passes)
    """
    result = []
    remaining = token
    while True:
        pos = _find_split_pos(remaining)
        if pos is None:
            result.append(remaining)
            break
        result.append(remaining[:pos])
        remaining = remaining[pos:]
    return ' '.join(result)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fix_merged_words(text: str) -> str:
    """
    Scan all tokens in `text` and re-insert spaces where Vietnamese syllables
    were merged due to PDF font encoding issues.

    Only tokens longer than _MIN_MERGE_LEN (5 NFC chars) are examined.
    Correct text passes through unchanged.
    """
    def replace_token(m: re.Match) -> str:
        tok = m.group(0)
        if len(tok) <= _MIN_MERGE_LEN:
            return tok
        # Skip tokens containing '/' (form codes, doc refs like "02/PTHU-DK", "373/2025/NĐ-CP")
        # or digits — these are codes/numbers, not merged Vietnamese syllables.
        if '/' in tok or any(c.isdigit() for c in tok):
            return tok
        fixed = _fix_merged_token(tok)
        return fixed

    return _TOKEN_RE.sub(replace_token, text)


def normalize_text(text: str) -> str:
    """
    Full normalization pipeline for extracted PDF text.
    Currently: Unicode NFC  →  fix merged words.
    """
    text = unicodedata.normalize('NFC', text)
    text = fix_merged_words(text)
    return text
