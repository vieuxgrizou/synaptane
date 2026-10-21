"""kind_of: a UTF-8 text file stays « text » even when the 8 KiB sniffing window cuts a character."""

from synaptane.services.content import kind_of


def test_character_cut_by_the_window_is_still_text() -> None:
    data = ("a" * 8191 + "é" + " fin").encode()  # « é » is 2 bytes: byte 8192 falls in the middle
    assert data[:8192].decode("utf-8", "replace").endswith("�")
    assert kind_of("jeux/monte_meuble.jsonl", data) == "text"


def test_invalid_byte_is_still_binary() -> None:
    assert kind_of("x.jsonl", b"abc\xff\xfedef") == "binary"
    assert kind_of("x.jsonl", ("a" * 8191).encode() + b"\xff" + b"rest") == "binary"


def test_short_file_ending_with_a_cut_character_is_binary() -> None:
    assert kind_of("x.txt", "é".encode()[:1]) == "binary"  # the whole file is read: a truncated end is invalid


def test_nul_byte_is_binary() -> None:
    assert kind_of("x.txt", b"abc\0def") == "binary"
