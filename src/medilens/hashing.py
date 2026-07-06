"""Content hashing shared across code-set and policy ingestion.

CLAUDE.md section 6 requires a hash over the semantic fields of every code-set
and policy record so that upstream changes are detected and re-ingested.
Centralizing the hashing here keeps every ingester using the same canonical
form, so two ingesters cannot disagree on how a record is fingerprinted.
"""

import hashlib

# Unit Separator (ASCII 0x1F): a control character that does not appear in the
# human-readable field values, so two different field splits can never join
# into the same canonical string and collide.
_FIELD_SEPARATOR = "\x1f"


def hash_content(parts: list[str]) -> str:
    """Return a stable sha256 hex digest of the given ordered fields.

    The caller passes the semantic fields in a fixed order; changing any field,
    or their order, changes the digest. The digest is 64 hex characters, which
    matches the content_hash column width in the data model.
    """
    canonical = _FIELD_SEPARATOR.join(parts)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return digest
