"""
Phonetic alphabet utilities for callsign encoding (TX) and decoding (RX).
Handles both NATO phonetics and common speech-recognition variants.
"""
import re

NATO = {
    'A': 'Alpha', 'B': 'Bravo', 'C': 'Charlie', 'D': 'Delta',
    'E': 'Echo',  'F': 'Foxtrot', 'G': 'Golf',  'H': 'Hotel',
    'I': 'India', 'J': 'Juliet', 'K': 'Kilo',   'L': 'Lima',
    'M': 'Mike',  'N': 'November', 'O': 'Oscar', 'P': 'Papa',
    'Q': 'Quebec', 'R': 'Romeo', 'S': 'Sierra', 'T': 'Tango',
    'U': 'Uniform', 'V': 'Victor', 'W': 'Whiskey', 'X': 'X-ray',
    'Y': 'Yankee', 'Z': 'Zulu',
    '0': 'Zero', '1': 'One', '2': 'Two', '3': 'Three', '4': 'Four',
    '5': 'Five',  '6': 'Six', '7': 'Seven', '8': 'Eight', '9': 'Niner',
    '/': 'stroke',
}

# Reverse map: every recognisable word → letter/digit
# Includes common Whisper mis-transcriptions
_REVERSE_RAW = {
    # Letters
    'alpha': 'A', 'alfa': 'A',
    'bravo': 'B',
    'charlie': 'C', 'charli': 'C',
    'delta': 'D',
    'echo': 'E',
    'foxtrot': 'F', 'fox': 'F',
    'golf': 'G',
    'hotel': 'H',
    'india': 'I',
    'juliet': 'J', 'juliett': 'J',
    'kilo': 'K',
    'lima': 'L',
    'mike': 'M',
    'november': 'N',
    'oscar': 'O',
    'papa': 'P',
    'quebec': 'Q', 'kilo echo': 'Q',
    'romeo': 'R',
    'sierra': 'S',
    'tango': 'T',
    'uniform': 'U',
    'victor': 'V',
    'whiskey': 'W', 'whisky': 'W',
    'x-ray': 'X', 'xray': 'X', 'x ray': 'X',
    'yankee': 'Y',
    'zulu': 'Z',
    # Digits
    'zero': '0', 'nought': '0',
    'one': '1', 'wun': '1',
    'two': '2', 'too': '2',
    'three': '3', 'tree': '3',
    'four': '4', 'fower': '4',
    'five': '5', 'fife': '5',
    'six': '6',
    'seven': '7',
    'eight': '8', 'ait': '8',
    'nine': '9', 'niner': '9',
    'stroke': '/', 'slash': '/', 'portable': '/',
}

# Build a sorted list so longer phrases match first (e.g. "x ray" before single words)
_REVERSE = dict(sorted(_REVERSE_RAW.items(), key=lambda x: -len(x[0])))

# RST number words
_RST_WORDS = {
    'zero': 0, 'nought': 0,
    'one': 1, 'wun': 1,
    'two': 2, 'too': 2,
    'three': 3, 'tree': 3,
    'four': 4, 'fower': 4,
    'five': 5, 'fife': 5,
    'six': 6,
    'seven': 7,
    'eight': 8, 'ait': 8,
    'nine': 9, 'niner': 9,
}

# Known callsign prefixes (for validation heuristics)
_CALLSIGN_RE = re.compile(
    r'\b([A-Z]{1,2}[0-9][A-Z]{1,4}|[A-Z][0-9][A-Z]{1,4}|[0-9][A-Z]{1,2}[0-9][A-Z]{1,4})\b'
)


def callsign_to_speech(callsign: str) -> str:
    """Convert 'M8WLO' → 'Mike Eight Whiskey Lima Oscar'."""
    parts = []
    for ch in callsign.upper():
        parts.append(NATO.get(ch, ch))
    return ' '.join(parts)


def extract_all_callsigns(text: str) -> list[str]:
    """
    Return every callsign found in a transcript — both direct regex matches
    and any callsigns recoverable from phonetic words.
    Duplicates are removed; order is preserved.
    """
    seen: dict[str, None] = {}

    # Direct matches
    for m in _CALLSIGN_RE.finditer(text.upper()):
        seen[m.group(1)] = None

    # Phonetic decode — may reveal additional callsigns not written out
    decoded = _decode_phonetics(text)
    for m in _CALLSIGN_RE.finditer(decoded):
        seen[m.group(1)] = None

    return list(seen)


def speech_to_callsign(text: str) -> str | None:
    """
    Try to extract a callsign from Whisper-transcribed text.
    Handles: direct letters ('M 8 W L O'), phonetics ('Mike Eight Whiskey Lima Oscar'),
    and already-formatted callsigns ('M8WLO').
    Returns the callsign string or None.
    """
    text = text.strip().lower()

    # 1. Direct callsign already in text (most reliable)
    direct = _CALLSIGN_RE.search(text.upper())
    if direct:
        return direct.group(1)

    # 2. Phonetic decoding
    decoded = _decode_phonetics(text)
    if decoded:
        m = _CALLSIGN_RE.search(decoded)
        if m:
            return m.group(1)

    return None


def _decode_phonetics(text: str) -> str:
    """Replace phonetic words with their letter/digit equivalents."""
    result = text
    for word, letter in _REVERSE.items():
        result = re.sub(r'\b' + re.escape(word) + r'\b', letter, result, flags=re.IGNORECASE)
    # Collapse spaces between resulting letters/digits
    result = re.sub(r'([A-Z0-9])\s+([A-Z0-9])', r'\1\2', result.upper())
    return result


def speech_to_rst(text: str) -> str | None:
    """
    Extract RST from transcribed speech.
    Handles: '59', 'five nine', 'five niner', '5 9', '599'.
    Returns e.g. '59' or None.
    """
    text = text.strip().lower()

    # Replace number words
    for word, digit in _RST_WORDS.items():
        text = re.sub(r'\b' + re.escape(word) + r'\b', str(digit), text)

    # Find sequences of 2-3 digits
    nums = re.findall(r'\d', text)
    if len(nums) >= 2:
        # Take first 2 digits (RS) or 3 (RST for CW)
        return ''.join(nums[:3]) if len(nums) >= 3 else ''.join(nums[:2])

    return None


def smeter_to_rst(s_reading: int) -> str:
    """Convert S-meter integer (0-9 or higher) to RST report string."""
    # S-meter values from hamlib are typically 0-60 dBm above S1 or similar
    # We'll treat the value as roughly: <12=S1-S3, 12-24=S4-S6, 24-36=S7-S8, 36+=S9
    if s_reading >= 36:
        readability, strength = 5, 9
    elif s_reading >= 24:
        readability, strength = 5, 7
    elif s_reading >= 12:
        readability, strength = 4, 5
    else:
        readability, strength = 3, 3
    return f"{readability}{strength}"
