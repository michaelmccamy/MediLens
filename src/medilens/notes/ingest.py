"""Note ingestion: read and normalize clinical note text for the pipeline.

Real EHR and dictation exports are messy: inconsistent line endings, unicode
punctuation, non-breaking spaces, trailing whitespace, and long runs of blank
lines. The verbatim-span grounding in the reasoning layer is sensitive to that
noise, so notes are normalized once at this boundary. Everything downstream
(PHI screen, content reference, grounding, storage) then sees the same
canonical text, which keeps character offsets consistent end to end.

Normalization only cleans formatting; it never removes or rewrites clinical
content. Unicode punctuation is folded to its plain-ASCII equivalent (curly
quotes to straight, en/em dashes to hyphen) because models tend to cite the
plain form, which reduces spurious grounding mismatches without changing
meaning.

Scope: plain text (.txt/.md) and RTF (.rtf, common for dictation exports) are
supported. PDF and other EHR export formats are a documented extension point;
they need extraction that preserves offsets well enough for grounding, which
pairs with the grounding hardening work.
"""

import unicodedata
from pathlib import Path

# Unicode characters folded to a plain-ASCII equivalent. Whitespace variants
# become a regular space; punctuation variants become their ASCII form. Escape
# sequences are used rather than literal characters so the invisible ones are
# readable in source and there is no literal em dash in this file (project
# formatting rule).
_UNICODE_REPLACEMENTS = {
    chr(0x00A0): " ",  # non-breaking space
    chr(0x2007): " ",  # figure space
    chr(0x202F): " ",  # narrow no-break space
    chr(0x2018): "'",  # left single quote
    chr(0x2019): "'",  # right single quote / apostrophe
    chr(0x201C): '"',  # left double quote
    chr(0x201D): '"',  # right double quote
    chr(0x2013): "-",  # en dash
    chr(0x2014): "-",  # em dash
    chr(0x2026): "...",  # ellipsis
}

_SUPPORTED_TEXT_SUFFIXES = {".txt", ".text", ".md", ""}


def normalize_note_text(raw_text: str) -> str:
    """Return canonical note text: consistent unicode, line endings, whitespace.

    Idempotent: normalizing already-normalized text returns it unchanged.
    """
    normalized = unicodedata.normalize("NFC", raw_text)

    for source_char, replacement in _UNICODE_REPLACEMENTS.items():
        normalized = normalized.replace(source_char, replacement)

    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")

    # Strip trailing whitespace per line and collapse runs of blank lines to a
    # single blank line, so section spacing is predictable.
    cleaned_lines: list[str] = []
    previous_blank = False
    for line in normalized.split("\n"):
        stripped_line = line.rstrip()
        is_blank = stripped_line == ""
        if is_blank and previous_blank:
            continue
        cleaned_lines.append(stripped_line)
        previous_blank = is_blank

    result = "\n".join(cleaned_lines).strip()
    if result:
        result = result + "\n"
    return result


def extract_note_text(filename: str, raw_bytes: bytes) -> str:
    """Extract raw (un-normalized) text from an uploaded note by extension.

    Fails loudly on an unsupported format rather than guessing, so a caller
    cannot silently feed a binary blob into the pipeline (CLAUDE.md section 7).
    """
    suffix = Path(filename).suffix.lower()
    if suffix in _SUPPORTED_TEXT_SUFFIXES:
        return raw_bytes.decode("utf-8", errors="replace")
    if suffix == ".rtf":
        from striprtf.striprtf import rtf_to_text

        return rtf_to_text(raw_bytes.decode("utf-8", errors="replace"))
    raise ValueError(
        f"unsupported note format {suffix!r}; supported formats are "
        ".txt, .text, .md, and .rtf"
    )


def load_and_normalize_upload(filename: str, raw_bytes: bytes) -> str:
    """Extract then normalize an uploaded note in one step."""
    return normalize_note_text(extract_note_text(filename, raw_bytes))


def load_note_text_from_path(path: Path) -> str:
    """Read a note file from disk and return normalized text."""
    return load_and_normalize_upload(path.name, path.read_bytes())
