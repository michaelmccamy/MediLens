"""Tests for the shared content-hashing primitive."""

from medilens.hashing import hash_content


def test_hash_is_stable() -> None:
    parts = ["ICD-10-CM", "M54.16", "Radiculopathy, lumbar region"]

    assert hash_content(parts) == hash_content(parts)


def test_hash_is_sha256_hex_width() -> None:
    digest = hash_content(["a", "b"])

    assert len(digest) == 64
    assert all(character in "0123456789abcdef" for character in digest)


def test_hash_is_order_sensitive() -> None:
    assert hash_content(["a", "b"]) != hash_content(["b", "a"])


def test_hash_field_boundaries_do_not_collide() -> None:
    # Without an unambiguous separator, ["ab", "c"] and ["a", "bc"] could hash
    # to the same string. The unit separator prevents that collision.
    assert hash_content(["ab", "c"]) != hash_content(["a", "bc"])
