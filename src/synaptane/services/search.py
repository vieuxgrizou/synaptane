"""Grep-style search in the accessible spaces (see docs/concepts.md, "Search").

ripgrep scans `content/` (Markdown and every text file) and the text extracted from PDFs,
Office documents and images. Permissions are applied first: only readable spaces are
scanned.

The text is never rewritten on disk: the pattern is what widens. Each term is folded
(lowercase, no accents or ligatures: "Nœuds" → "noeuds"), reduced to its stem
("sauvegarder" → "sauvegard"), then turned into a pattern that finds every spelling in the
raw text ("n(?:oe|œ)ud"). Highlighting is searched in the folded text and carried over,
character by character, to the original text.

Ranking: simplified BM25 (rarity of each term, occurrences, file size), bonuses for the exact
phrase, exact word, path and title; a slight edge for verified, up-to-date pages, a slight
setback for pages due for review (`services.freshness`, multiplicative factor: relevance
stays the first criterion); on equal scores, the most recently modified file comes first.
"""

import functools
import json
import logging
import math
import os
import re
import shutil
import subprocess
import threading
import unicodedata
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from itertools import islice
from pathlib import Path
from urllib.parse import quote
from uuid import UUID

from markdown_it import MarkdownIt
from mdit_py_plugins.anchors.index import slugify, unique_slug
from sqlalchemy.orm import Session

from synaptane.ai import embeddings, rerank
from synaptane.ai.base import AIError
from synaptane.ai.embeddings import Embedder, EmbeddingConfig, prefixes
from synaptane.core import markdown
from synaptane.core.errors import invalid, not_found, unavailable
from synaptane.services import Actor, freshness, semantic, spaces
from synaptane.services.freshness import Freshness
from synaptane.services.inbox import CONTENT_DIR
from synaptane.services.workspace import Workspace
from synaptane.storage.models import Space

log = logging.getLogger("synaptane.search")
MAX_HITS_PER_SPACE = 60  # files read for snippets, per space
SNIPPET_LINES_PER_FILE = 10
SNIPPETS_PER_HIT = 3
CONTEXT = 2
PASSAGE_LINES = 5  # lines of the excerpt shown, a single block per result
RG_TIMEOUT = 20
K1 = 1.2  # BM25: beyond a few occurrences, one more word adds almost nothing
B = 0.75  # BM25: weight of file size (a long page mentions everything, a little)


# Stop words: ignored as long as the search holds at least one meaningful word.
_STOPWORDS = """
le la les un une des de du d l au aux et ou en dans sur pour par avec sans ce cet cette ces
son sa ses leur leurs mon ma mes ton ta tes notre nos votre vos qui que quoi dont où est
sont été être a ont à il elle ils elles on nous vous je tu ne pas plus se s c n y comment
the a an of to in on for with and or is are was were be by at from as it this that these
those how what which who
"""
STOPWORDS = frozenset(_STOPWORDS.split())


@dataclass
class Snippet:
    line: int
    text: str
    before: list[str] = field(default_factory=list)
    after: list[str] = field(default_factory=list)


@dataclass
class Hit:
    org: str
    space: str
    space_name: str
    path: str  # relative to content/
    title: str
    score: float
    snippets: list[Snippet]
    extracted: bool = False  # found in the extracted text (PDF, Office, image)
    match: str = "text"  # text (words), meaning (embeddings) or both
    passage: list[str] = field(default_factory=list)  # readable excerpt; "…": a gap
    anchor: str = ""  # anchor of the best snippet's section, as in the Markdown rendering
    section: str = ""  # title of that section, as text
    modified: float = 0.0  # file modification date (tie-breaker on equal scores)
    freshness: Freshness | None = None  # Markdown page: verified, due for review…

    @property
    def verified(self) -> bool:
        return self.freshness is not None and self.freshness.is_verified

    @property
    def stale(self) -> bool:
        return self.freshness is not None and self.freshness.stale

    @property
    def address(self) -> str:
        return f"{self.org}/{self.space}"

    @property
    def fragment(self) -> str:
        """The "#anchor" to append to the page address; empty outside a section."""
        return f"#{quote(self.anchor)}" if self.anchor else ""


@dataclass(frozen=True)
class Query:
    text: str
    regex: bool
    terms: tuple[str, ...]

    @classmethod
    def parse(cls, text: str, regex: bool = False) -> "Query":
        text = " ".join(text.split())
        if not text:
            raise invalid("Enter text to search for.")
        if len(text) > 300:
            raise invalid("Search too long (300 characters maximum).")
        if regex:
            _check_regex(text)
            return cls(text, True, (text,))
        words = [fold(w.strip(".,;:!?«»\"'()")) for w in text.split()]
        words = [w for w in words if len(w) > 1]
        meaningful = [w for w in words if w not in STOPWORDS]
        terms = tuple(dict.fromkeys(meaningful or words)) or (text,)
        return cls(text, False, terms)

    def rg_patterns(self) -> list[str]:
        args: list[str] = []
        for term in self.terms:
            args += ["-e", term if self.regex else term_pattern(term)]
        return args

    def matched_terms(self, text: str) -> set[str]:
        """Terms present in `text`. Always empty for a regex: only ripgrep, in linear time,
        evaluates it (Python's `re` engine can blow up and freeze the process)."""
        if self.regex:
            return set()
        folded = fold(text)
        return {t for t in self.terms if _matcher(t).search(folded)}

    def exact_terms(self, text: str) -> set[str]:
        """Terms present as such, as a whole word: "sauvegarder" and not "sauvegarde"."""
        if self.regex:
            return set()
        folded = fold(text)
        return {t for t in self.terms if _exact(t).search(folded)}


# --- Folding: accents, ligatures, stems -------------------------------------------------------

_LIGATURES = str.maketrans({"œ": "oe", "æ": "ae", "ß": "ss", "’": "'"})


def fold(text: str) -> str:
    """Lowercase, no accents or ligatures (é → e, ç → c, œ → oe, æ → ae, ß → ss), straight
    apostrophe."""
    if text.isascii():  # the common case (paths, typed words): nothing to decompose
        return text.lower()
    decomposed = unicodedata.normalize("NFKD", text.lower().translate(_LIGATURES))
    return "".join(c for c in decomposed if not unicodedata.combining(c))


@functools.lru_cache(maxsize=4096)
def _fold_char(char: str) -> str:
    return fold(char)


def _fold_offsets(text: str) -> tuple[str, list[int]]:
    """Folded text and, for each of its characters, its position in `text` ("œ" gives two
    characters, a combining accent none)."""
    pieces: list[str] = []
    origin: list[int] = []
    for index, char in enumerate(text):
        folded = _fold_char(char)
        pieces.append(folded)
        origin += [index] * len(folded)
    return "".join(pieces), origin


def _variants() -> dict[str, str]:
    """For each folded letter, the class of all its Latin spellings ("e" → "[eèéêëēĕėęě]"):
    those that `fold` brings back to it, no more, no less."""
    found: dict[str, set[str]] = {}
    for code in range(0x00C0, 0x0250):  # Latin-1, Latin Extended-A and -B
        char = chr(code)
        base = fold(char)
        if char.islower() and len(base) == 1 and base.isascii() and base.isalpha():
            found.setdefault(base, {base}).add(char)
    return {base: "[" + "".join(sorted(chars)) + "]" for base, chars in found.items()}


_CLASSES = _variants()
_DIGRAPHS = {"oe": "œ", "ae": "æ", "ss": "ß"}  # the raw text may carry either form
_META = set("\\.+*?()|[]{}^$#&-~")

# Endings removed to find the other forms of a word: "sauvegarder" finds "sauvegarde",
# "installation" finds "installer". Cautious rules, no dictionary: the stem keeps at least 5
# letters, otherwise the word stays whole ("porter" must not find "portail").
_ENDING_LIST = """
e es ee ees er ers ez ent ant ants ante antes ait aient ais ions iez ons
era eras erai erais erait eraient erons erez eront ement ements
ation ations ateur ateurs atrice atrices
ing ings ed ly ion ions ment ments s x
"""
_ENDINGS = sorted(set(_ENDING_LIST.split()), key=len, reverse=True)
MIN_STEM = 5


def stem(word: str) -> str:
    """Stem of a folded word: common French or English ending removed ("sauvegarder" →
    "sauvegard"); only the plural for a 5-letter word ("notes" → "note"); nothing for a
    shorter word, nor for what is not a word."""
    if not word.isalpha():
        return word
    for ending in _ENDINGS:
        if word.endswith(ending) and len(word) - len(ending) >= MIN_STEM:
            return word[: -len(ending)]
    if len(word) > 4 and word[-1] in "sx":
        return word[:-1]
    return word


def _whole_word(folded: str) -> bool:
    return folded.isalnum() and len(folded) <= 3


def term_pattern(term: str) -> str:
    """ripgrep pattern (Rust syntax) that finds `term` in the raw text: with or without
    accents and ligatures, and in its other forms (stem); a word of 3 letters or fewer only
    as a whole word."""
    folded = fold(term)
    pattern = _letters(stem(folded))
    return rf"\b{pattern}\b" if _whole_word(folded) else pattern


def _phrase_pattern(text: str) -> str:
    """Exact phrase, accents and ligatures folded, any whitespace between the words."""
    return _letters(fold(text))


@functools.lru_cache(maxsize=512)
def _matcher(term: str) -> "re.Pattern[str]":
    """The same term, searched in **folded** text (Python); the match runs to the end of the
    word, for highlighting ("sauvegard" covers "sauvegardées")."""
    folded = fold(term)
    root = stem(folded)
    pattern = re.escape(root)
    if _whole_word(folded):
        return re.compile(rf"\b{pattern}\b")
    return re.compile(pattern + (r"\w*" if root != folded else ""))


@functools.lru_cache(maxsize=512)
def _exact(term: str) -> "re.Pattern[str]":
    return re.compile(rf"\b{re.escape(fold(term))}\b")


def _letter(char: str) -> str:
    if char in _CLASSES:
        return _CLASSES[char]
    if char == "'":
        return "['’]"  # straight or typographic apostrophe
    if char.isspace():
        return r"\s+"
    if char in _META:
        return "\\" + char
    return char


def _letters(folded: str) -> str:
    """Pattern that finds, in the raw text, any spelling whose folded form is `folded`."""
    parts = []
    index = 0
    while index < len(folded):
        pair = folded[index : index + 2]
        if pair in _DIGRAPHS:
            parts.append(f"(?:{_letter(pair[0])}{_letter(pair[1])}|{_DIGRAPHS[pair]})")
            index += 2
        else:
            parts.append(_letter(folded[index]))
            index += 1
    return "".join(parts)


def highlight_spans(text: str, terms: Sequence[str]) -> list[tuple[int, int]]:
    """Ranges of `text` to highlight, sorted and disjoint.

    Terms are searched in the folded text, then each match is carried over to the original
    text: "noeuds" highlights "nœuds". Two overlapping terms give a single range: never any
    nested highlighting."""
    if not text or not terms:
        return []
    folded, origin = _fold_offsets(text)
    spans = []
    for term in terms:
        if not fold(term).strip():
            continue
        for found in _matcher(term).finditer(folded):
            if found.end() > found.start():
                first, last = origin[found.start()], origin[found.end() - 1] + 1
                while last < len(text) and not _fold_char(text[last]):
                    last += 1  # combining accent following the last letter
                spans.append((first, last))
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _check_regex(pattern: str) -> None:
    """Validates the regex with ripgrep itself (Rust syntax, no backtracking)."""
    try:
        result = subprocess.run(
            [shutil.which("rg") or "rg", "--quiet", "--regexp", pattern, "--", os.devnull],
            capture_output=True,
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        raise invalid("Regular expression too complex.") from None
    if result.returncode == 2:
        raise invalid("Invalid regular expression.")


_SLOTS = threading.BoundedSemaphore(8)  # concurrent searches (ripgrep) in the process
_SLOT_WAIT = 10  # seconds: beyond that, "try again" rather than an endless queue


def _ripgrep(args: list[str], *, timeout: float = RG_TIMEOUT) -> bytes:
    """Runs ripgrep; empty output if the timeout is exceeded (search remains usable)."""
    command = [shutil.which("rg") or "rg", "--hidden", "--no-ignore", "--max-filesize=10M", *args]
    try:
        return subprocess.run(command, capture_output=True, timeout=timeout).stdout
    except subprocess.TimeoutExpired:
        log.warning("Search: timed out (%s s)", timeout)
        return b""


def _window(text: str, submatches: object) -> str:
    """Excerpt of a long line, centered on the first occurrence found."""
    if len(text) <= 400:
        return text
    start = 0
    if isinstance(submatches, list) and submatches and isinstance(submatches[0], dict):
        # Offset in bytes: converted to characters over the start of the line.
        offset = int(submatches[0].get("start", 0))
        start = len(text.encode("utf-8")[:offset].decode("utf-8", "ignore"))
    begin = max(0, start - 150)
    return (
        ("…" if begin else "")
        + text[begin : begin + 400]
        + ("…" if begin + 400 < len(text) else "")
    )


def _text(data: object) -> str:
    if isinstance(data, dict):
        return str(data.get("text", "")).rstrip("\n")
    return ""


@dataclass
class _FileMatches:
    path: str
    extracted: bool
    lines: dict[int, tuple[str, bool]] = field(default_factory=dict)  # no. → (text, matched?)


def _is_markdown(path: str) -> bool:
    return Path(path).suffix.lower() in (".md", ".markdown")


def _title(clone: Path, path: str) -> str:
    target = clone / CONTENT_DIR / path
    if _is_markdown(path):
        try:
            with target.open(encoding="utf-8", errors="replace") as stream:
                for _ in range(30):
                    line = stream.readline()
                    if line.startswith("# "):
                        return line[2:].strip()
        except OSError:
            pass
    return Path(path).name


def _snippets(query: Query, matches: _FileMatches) -> list[Snippet]:
    """Most telling snippets first: those that contain the most terms."""
    ranked = sorted(
        ((n, text) for n, (text, matched) in matches.lines.items() if matched),
        key=lambda item: (-len(query.matched_terms(item[1])), item[0]),
    )
    snippets = []
    for number, text in ranked:
        before = [
            matches.lines[n][0] for n in range(number - CONTEXT, number) if n in matches.lines
        ]
        after = [
            matches.lines[n][0]
            for n in range(number + 1, number + CONTEXT + 1)
            if n in matches.lines and not matches.lines[n][1]
        ]
        snippets.append(Snippet(number, text, before, after))
        if len(snippets) == SNIPPETS_PER_HIT:
            break
    return snippets


# --- Readable excerpt and section -------------------------------------------------------------

_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})")
_TABLE_RULE = re.compile(r"\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?")
_THEMATIC_BREAK = re.compile(r"([-*_])(?:\s*\1){2,}")


def _unlink(found: "re.Match[str]") -> str:
    return (found.group(3) or found.group(1)).strip()


def readable(line: str, is_markdown: bool = True) -> str:
    """Readable line in an excerpt: without Markdown syntax (hashes, bullets, quote, emphasis,
    code, links), a table row as "pve1 · 10.0.0.11 · compute". Empty for what does not read:
    blank line, code fence (and its language, "bash"), table or thematic break rule."""
    text = line.strip()
    if not is_markdown:
        return " ".join(text.split())
    if not text or _FENCE.match(text) or _TABLE_RULE.fullmatch(text):
        return ""
    if _THEMATIC_BREAK.fullmatch(text):
        return ""
    if text.startswith("|"):
        cells = [cell.strip() for cell in text.strip("|").split("|")]
        text = " · ".join(cell for cell in cells if cell)
    text = re.sub(r"^#{1,6}\s+(.*?)(?:\s+#+)?$", r"\1", text)
    text = re.sub(r"^(?:>\s?)+", "", text)
    text = re.sub(r"^(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s+)?", "", text)
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = markdown.WIKILINK.sub(_unlink, text)
    text = re.sub(r"\*\*|__|`|~~", "", text)
    text = re.sub(r"(?<!\w)\*(\S[^*]*?)\*(?!\w)", r"\1", text)  # *italic*
    return " ".join(text.split())


def _front_matter_end(file: Path) -> int:
    """Last line of a page's front matter (0 without front matter): its fields (owner,
    verification date…) are not an excerpt to show."""
    try:
        with file.open(encoding="utf-8", errors="replace") as stream:
            if stream.readline().strip() != "---":
                return 0
            for number, line in enumerate(stream, start=2):
                if line.strip() in ("---", "..."):
                    return number
                if number > 200:
                    break
    except OSError:
        pass
    return 0


def _passage(entry: "_Candidate", snippets: list[Snippet], skip: int = 0) -> list[str]:
    """Excerpt shown: a single readable block around the best snippet; the other matching
    lines are added while there is room, "…" marking a gap. Lines up to `skip` (the front
    matter) are not read."""
    is_markdown = _is_markdown(entry.path) and not entry.extracted
    text = {
        n: "" if n <= skip else readable(line, is_markdown) for n, (line, _) in entry.lines.items()
    }
    wanted: list[int] = []
    if snippets:
        best = snippets[0].line
        wanted = [best, best + 1, best - 1]
        wanted += [s.line for s in snippets[1:]] + [best + 2, best - 2]
    chosen: list[int] = []
    for number in wanted:
        if len(chosen) < PASSAGE_LINES and text.get(number) and number not in chosen:
            chosen.append(number)
    passage: list[str] = []
    previous = None
    for number in sorted(chosen):
        # Gap: a readable (or unknown) line between two chosen lines.
        if previous is not None and any(text.get(n, "…") for n in range(previous + 1, number)):
            passage.append("…")
        passage.append(text[number])
        previous = number
    return passage


_ATX = re.compile(r" {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")
_UNDERLINE = re.compile(r" {0,3}(=+|-+)[ \t]*$")
_NOT_PARAGRAPH = re.compile(r" {0,3}(?:[-*+>|]|\d+[.)]\s|#)")
_INLINE_SYNTAX = re.compile(r"[\[\]`*_~\\&<!]")


@dataclass(frozen=True)
class Heading:
    line: int
    level: int
    anchor: str  # the rendering's: <h2 id="…">
    text: str  # heading text (no syntax), as the rendering reads it
    raw: str  # heading as written


@functools.lru_cache(maxsize=2048)
def _heading_text(raw: str) -> str:
    """Text of a heading as the `anchors` extension reads it (text and code, links included,
    no images or emphasis). The Markdown parser is only used if there is syntax."""
    if not _INLINE_SYNTAX.search(raw):
        return raw
    tokens = _inline_parser().parseInline(raw)
    children = (tokens[0].children or []) if tokens else []
    return "".join(c.content for c in children if c.type in ("text", "code_inline"))


@functools.cache
def _inline_parser() -> MarkdownIt:
    return markdown.parser()


def headings(lines: Iterable[str], until: int | None = None) -> list[Heading]:
    """h1 to h4 headings of a Markdown text (the only ones with an anchor in the rendering),
    up to and including line `until`, with the rendering's anchors: same naming function
    (`mdit_py_plugins.anchors`), same -1, -2… suffix for a name already taken. Front matter
    and code blocks are skipped ("# install" in a bash block is not a heading)."""
    found: list[Heading] = []
    taken: set[str] = set()
    fence = ""
    front = False
    paragraph: list[str] = []
    for number, raw_line in enumerate(lines, start=1):
        if until is not None and number > until:
            break
        line = raw_line.rstrip("\r\n")
        if number == 1 and line.strip() == "---":
            front = True
            continue
        if front:
            front = line.strip() not in ("---", "...")
            continue
        if fence:
            if re.fullmatch(rf" {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}[ \t]*", line):
                fence = ""
            continue
        if opening := _FENCE.match(line):
            fence, paragraph = opening.group(1), []
            continue
        if atx := _ATX.match(line):
            level, raw = len(atx.group(1)), atx.group(2) or ""
        elif paragraph and (under := _UNDERLINE.match(line)):
            level, raw = (1 if under.group(1)[0] == "=" else 2), "".join(paragraph)
        else:
            if not line.strip() or _NOT_PARAGRAPH.match(line):
                paragraph = []
            else:
                paragraph.append(line.strip())
            continue
        paragraph = []
        if level <= 4:
            text = _heading_text(raw.strip())
            anchor = unique_slug(slugify(text), taken)
            found.append(Heading(number, level, anchor, text.strip(), raw.strip()))
    return found


def _read_headings(file: Path, until: int | None = None) -> list[Heading]:
    try:
        with file.open(encoding="utf-8", errors="replace") as stream:
            return headings(islice(stream, until) if until else stream)
    except OSError:
        return []


def _section(found: list[Heading], line: int) -> Heading | None:
    """Heading of the section `line` is in; none for the top of the page (before the first
    subheading, or under the page title)."""
    before = [h for h in found if h.line <= line]
    if not before or (before[-1] is found[0] and found[0].level == 1):
        return None
    return before[-1]


# --- First pass: counts and ranking -----------------------------------------------------------


class _Roots:
    """Roots of a space, and conversion of a ripgrep path into (wiki path, extracted?)."""

    def __init__(self, ws: Workspace, space: Space) -> None:
        self.clone = ws.clone_path(space.id)
        self.content = str(self.clone / CONTENT_DIR) + "/"
        self.extract = str(ws.extract_path(space.id)) + "/"
        self.paths = [r.rstrip("/") for r in (self.content, self.extract) if Path(r).is_dir()]
        self.states: dict[str, freshness.Freshness] = {}  # verified or due-for-review pages

    def locate(self, raw: str) -> tuple[str, bool] | None:
        if raw.startswith(self.content):
            return raw[len(self.content) :], False
        if raw.startswith(self.extract):
            relative = raw[len(self.extract) :]
            prefix = CONTENT_DIR + "/"
            if relative.startswith(prefix) and relative.endswith(".txt"):
                return relative[len(prefix) : -4], True
        return None

    def file(self, path: str, extracted: bool) -> str:
        """File read by ripgrep: the page itself, or its extracted text."""
        if extracted:
            return f"{self.extract}{CONTENT_DIR}/{path}.txt"
        return self.content + path


def _counts(roots: _Roots, patterns: list[str]) -> dict[str, tuple[int, bool]]:
    """First pass: number of occurrences per file (no snippets, very fast)."""
    out = _ripgrep(["--count-matches", "--null", "--ignore-case", *patterns, "--", *roots.paths])
    found: dict[str, tuple[int, bool]] = {}
    for line in out.split(b"\n"):
        raw, _, number = line.partition(b"\0")
        located = roots.locate(raw.decode("utf-8", "surrogateescape")) if number else None
        if located is not None:
            path, extracted = located
            previous = found.get(path, (0, extracted))[0]
            found[path] = (previous + int(number), extracted)
    return found


@dataclass
class _Candidate:
    path: str
    extracted: bool = False
    counts: dict[str, int] = field(default_factory=dict)  # term → occurrences
    phrase: bool = False
    lines: dict[int, tuple[str, bool]] = field(default_factory=dict)  # no. → (text, matched?)
    size: int = 0  # bytes of the file read by ripgrep
    modified: float = 0.0
    score: float = 0.0  # first pass (without the title)
    factor: float = 1.0  # verification: slight bonus (verified) or penalty (due for review)

    @property
    def count(self) -> int:
        return sum(self.counts.values())


@dataclass
class _Stats:
    """What BM25 knows about the space: number of files, files per term, average size."""

    documents: int
    frequencies: dict[str, int]
    average: float = 1.0
    _weights: dict[str, float] | None = None  # rarity of the query terms, computed once

    def idf(self, term: str) -> float:
        """Rarity of a term: a word present everywhere weighs almost nothing."""
        frequency = max(1, self.frequencies.get(term, 0))
        documents = max(self.documents, frequency)
        return math.log(1 + (documents - frequency + 0.5) / (frequency + 0.5))

    def weights(self, terms: Sequence[str]) -> dict[str, float]:
        if self._weights is None:
            self._weights = {term: self.idf(term) for term in terms}
        return self._weights


def _candidates(roots: _Roots, query: Query) -> tuple[dict[str, _Candidate], _Stats]:
    """All the files where at least one term appears (or whose name matches), and the
    frequencies used for ranking: they come for free, since counting is per term."""
    found: dict[str, _Candidate] = {}
    frequencies: dict[str, int] = {}
    for term in query.terms:
        pattern = term if query.regex else term_pattern(term)
        counted = _counts(roots, ["-e", pattern])
        frequencies[term] = len(counted)
        for path, (count, extracted) in counted.items():
            entry = found.setdefault(path, _Candidate(path, extracted))
            entry.counts[term] = entry.counts.get(term, 0) + count
    if not query.regex and len(query.terms) > 1:
        for path in _counts(roots, ["-e", _phrase_pattern(query.text)]):
            if path in found:
                found[path].phrase = True
    documents = 0
    if Path(roots.content).is_dir():
        listing = _ripgrep(["--files", "--null", "--", roots.content.rstrip("/")])
        for raw in listing.split(b"\0"):
            located = roots.locate(raw.decode("utf-8", "surrogateescape"))
            if located is None:
                continue
            documents += 1
            if not query.regex and located[0] not in found and query.matched_terms(located[0]):
                found[located[0]] = _Candidate(located[0])
    return found, _Stats(documents, frequencies)


def _measure(roots: _Roots, entries: Iterable[_Candidate], stats: _Stats) -> None:
    """Size and modification date of each candidate file (a `stat`, without reading it);
    average size of the files where a term appears, for BM25."""
    sizes = []
    for entry in entries:
        try:
            info = os.stat(roots.file(entry.path, entry.extracted))
        except OSError:
            continue
        entry.size, entry.modified = info.st_size, info.st_mtime
        if entry.counts:
            sizes.append(info.st_size)
    stats.average = max(1.0, sum(sizes) / len(sizes)) if sizes else 1.0


def _relevance(query: Query, entry: _Candidate, stats: _Stats) -> float:
    """First-pass score: BM25 scaled to 100 (each term once, in an average-size file), plus
    the exact phrase and the terms in the path."""
    terms = len(query.terms)
    in_path = query.matched_terms(entry.path)
    weights = stats.weights(query.terms)
    length = K1 * (1 - B + B * entry.size / stats.average)
    found = 0.0
    for term, weight in weights.items():
        if occurrences := entry.counts.get(term, 0):
            found += weight * occurrences * (K1 + 1) / (occurrences + length)
        elif term in in_path:
            found += weight  # in the name only: counts as one occurrence
    score = 100 * found / sum(weights.values())
    score += 50 if entry.phrase else 0  # exact phrase
    score += 30 * len(in_path) / terms
    return round(score, 2)


def _final_score(query: Query, entry: _Candidate, title: str) -> float:
    """Score shown: the first-pass one, plus the terms in the title and the words found as
    such (the stem widens, the exact word comes first)."""
    terms = len(query.terms)
    seen = "\n".join([entry.path, title, *(text for text, _ in entry.lines.values())])
    exact = query.exact_terms(seen)
    extra = 20 * len(query.matched_terms(title)) / terms + 10 * len(exact) / terms
    return round(entry.score + extra * entry.factor, 2)


def _excerpts(roots: _Roots, query: Query, best: list[_Candidate]) -> None:
    """Second pass: snippets, only for the best files."""
    files = [roots.file(entry.path, entry.extracted) for entry in best if entry.counts]
    if not files:
        return
    by_path = {entry.path: entry for entry in best}
    out = _ripgrep(
        [
            "--json",
            "--ignore-case",
            f"--context={CONTEXT}",
            f"--max-count={SNIPPET_LINES_PER_FILE}",
            "--max-columns=20000",  # whole line: the excerpt is centered on the word found
            "--max-columns-preview",
            *query.rg_patterns(),
            "--",
            *files,
        ]
    )
    for line in out.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") not in ("match", "context"):
            continue
        data = event["data"]
        located = roots.locate(_text(data.get("path")))
        target = by_path.get(located[0]) if located else None
        if target is not None:
            number = int(str(data.get("line_number", 0)))
            text, matched = _text(data.get("lines")), event["type"] == "match"
            target.lines[number] = (
                _window(text, data.get("submatches")) if matched else text[:400],
                matched,
            )


def _order(entry: _Candidate) -> tuple[float, float, str]:
    return (-entry.score, -entry.modified, entry.path)


def search_space(ws: Workspace, space: Space, query: Query) -> list[Hit]:
    """Results of a space, from most to least relevant.

    Every file is ranked (counted by ripgrep); only the best ones are read for snippets. The
    cost hardly depends on the size of the space any more.
    """
    ranked = _rank_space(ws, space, query)
    if ranked is None:
        return []
    roots, candidates = ranked
    return _hits(space, roots, query, candidates[:MAX_HITS_PER_SPACE])


def _rank_space(
    ws: Workspace, space: Space, query: Query
) -> tuple["_Roots", list[_Candidate]] | None:
    """First pass: all the files of a space ranked, without snippets."""
    roots = _Roots(ws, space)
    if not roots.paths:
        return None
    if not _SLOTS.acquire(timeout=_SLOT_WAIT):
        raise unavailable("Too many searches in progress: please try again in a moment.")
    try:
        found, stats = _candidates(roots, query)
    finally:
        _SLOTS.release()
    _measure(roots, found.values(), stats)
    states = roots.states = freshness.space_states(ws, space)
    for entry in found.values():
        state = states.get(entry.path) if not entry.extracted else None
        entry.factor = state.search_factor() if state is not None else 1.0
        entry.score = round(_relevance(query, entry, stats) * entry.factor, 2)
    return roots, sorted(found.values(), key=_order)


def _hits(space: Space, roots: "_Roots", query: Query, best: list[_Candidate]) -> list[Hit]:
    """Second pass: snippets, titles and sections, for the chosen files only."""
    if not best:
        return []
    if not _SLOTS.acquire(timeout=_SLOT_WAIT):
        raise unavailable("Too many searches in progress: please try again in a moment.")
    try:
        _excerpts(roots, query, best)
    finally:
        _SLOTS.release()
    hits = []
    org = space.organization
    for entry in best:
        title = _title(roots.clone, entry.path)
        snippets = _snippets(query, _FileMatches(entry.path, entry.extracted, entry.lines))
        section = None
        front = 0  # front matter: never in the excerpt (one first line read at most)
        if _is_markdown(entry.path) and not entry.extracted and entry.lines:
            front = _front_matter_end(Path(roots.file(entry.path, False)))
        if snippets and _is_markdown(entry.path) and not entry.extracted:
            found = _read_headings(Path(roots.file(entry.path, False)), snippets[0].line)
            section = _section(found, snippets[0].line)
        hits.append(
            Hit(
                org=org.slug,
                space=space.slug,
                space_name=space.name,
                path=entry.path,
                title=title,
                score=_final_score(query, entry, title),
                snippets=snippets,
                extracted=entry.extracted,
                passage=_passage(entry, snippets, front),
                anchor=section.anchor if section else "",
                section=section.text if section else "",
                modified=entry.modified,
                freshness=_state(roots.states, entry.path, entry.extracted),
            )
        )
    hits.sort(key=lambda h: (-h.score, -h.modified, h.path))
    return hits


def _state(
    states: dict[str, freshness.Freshness], path: str, extracted: bool
) -> freshness.Freshness | None:
    """Verification state of a Markdown page found (None for another file)."""
    if extracted or not freshness.is_page(path):
        return None
    return states.get(path, freshness.NEVER_VERIFIED)


RRF_K = 60
MEANING_ONLY_MAX = 5  # pages found only by meaning, at most
MEANING_ONLY_GAP = 0.08  # and close to the best semantic result
MEANING_ONLY_CONFIDENCE = 0.08  # minimal gap between the best neighbour and the median


def _semantic_lists(
    session: Session,
    spaces_: list[Space],
    query: Query,
    ws: Workspace,
    embedder: Embedder,
) -> dict[UUID, list[semantic.SemanticHit]]:
    """Pages close in meaning, for the spaces where embeddings are on.

    The question is embedded once per configuration (organization)."""
    by_config: dict[EmbeddingConfig, list[Space]] = {}
    for space in spaces_:
        config = semantic.config_for(session, ws.box, space)
        if config is not None:
            by_config.setdefault(config, []).append(space)
    results: dict[UUID, list[semantic.SemanticHit]] = {}
    for config, members in by_config.items():
        try:
            [vector] = embedder(config, [prefixes(config.model)[0] + query.text])
        except AIError:
            continue  # word search remains available
        for space in members:
            results[space.id] = semantic.nearest(session, space.id, config.model, vector)
    return results


def _named_section(clone: Path, path: str, trail: str) -> Heading | None:
    """Section of a chunk found by meaning, from its heading trail ("Title › Section"): the
    last heading, found again in the page."""
    names = trail.split(" › ")
    if len(names) < 2 or not _is_markdown(path):
        return None
    found = _read_headings(clone / CONTENT_DIR / path)
    return next((h for h in reversed(found) if h.raw == names[-1].strip()), None)


def _meaning(
    space: Space, lexical: list[Hit], meaning: list[semantic.SemanticHit], ws: Workspace
) -> list[tuple[float, Hit]]:
    """Contribution of meaning in a space: (RRF contribution, page), pages found by words
    included (their result is then marked "both"). Empty if the question resembles nothing
    in the space."""
    by_path = {hit.path: hit for hit in lexical}
    best = meaning[0].similarity if meaning else 0.0
    others = sorted(m.similarity for m in meaning[1:])
    median = others[len(others) // 2] if others else 0.0
    # The best neighbour does not stand out: the question probably has nothing to do with the
    # space; meaning then adds nothing (no tie-breaking, no extra page). With a similarity
    # floor, the best neighbour must reach it instead: in a space of similar pages (call
    # transcripts…), a relevant question stands out no more than an unrelated one.
    floor = ws.meaning_min_similarity
    if floor is not None:
        if best < floor:
            return []
    elif others and best - median < MEANING_ONLY_CONFIDENCE:
        return []
    clone = ws.clone_path(space.id)
    found_pages: list[tuple[float, Hit]] = []
    for rank, found in enumerate(meaning, start=1):
        if rank > MEANING_ONLY_MAX or found.similarity < best - MEANING_ONLY_GAP:
            continue  # distant neighbour: noise rather than a result
        hit = by_path.get(found.path)
        if hit is not None:
            hit.match = "both"
        else:
            excerpt = " ".join(found.text.split())[:300]
            section = _named_section(clone, found.path, found.heading)
            hit = by_path[found.path] = Hit(
                org=space.organization.slug,
                space=space.slug,
                space_name=space.name,
                path=found.path,
                title=_title(clone, found.path),
                score=0.0,
                snippets=[Snippet(0, excerpt, [found.heading], [])],
                match="meaning",
                passage=[readable(excerpt, _is_markdown(found.path))],
                anchor=section.anchor if section else "",
                section=section.text if section else "",
                freshness=_state(freshness.space_states(ws, space), found.path, False),
            )
        found_pages.append((1 / (RRF_K + rank), hit))
    return found_pages


RERANK_POOL = 40  # best fused results rescored by the reranker
RERANK_CHARS = 2000  # text read by the reranker, per result


def _rerank_text(hit: Hit) -> str:
    """What the reranker reads of a result: title, section and excerpts."""
    lines = [hit.title, hit.section]
    for snippet in hit.snippets:
        lines += [*snippet.before, snippet.text, *snippet.after]
    lines += hit.passage
    return "\n".join(line for line in lines if line)[:RERANK_CHARS]


def _rerank_order(hits: list[Hit], scores: list[float]) -> list[Hit]:
    """Results by decreasing relevance; equal scores keep the fused order."""
    return [hits[i] for i in sorted(range(len(hits)), key=lambda i: -scores[i])]


def _rerank_config(
    session: Session, ws: Workspace, spaces_: Sequence[Space]
) -> EmbeddingConfig | None:
    """Embedding configuration of the first organization whose provider can rerank."""
    seen: set[UUID] = set()
    for space in spaces_:
        if space.org_id in seen:
            continue
        seen.add(space.org_id)
        config = semantic.org_config(session, ws.box, space.org_id)
        if config is not None and config.provider in rerank.RERANK_PROVIDERS:
            return config
    return None


def _reranked(
    session: Session,
    ws: Workspace,
    spaces_: Sequence[Space],
    query: Query,
    hits: list[Hit],
    limit: int,
    mode: str,
    reranker: rerank.Reranker | None = None,
) -> list[Hit]:
    """The best `limit` results, rescored by the reranker when one is set (`rerank_model`).
    Word-only searches (`mode=text`, regular expression) keep their order; so does a provider
    outage."""
    top = max(1, min(limit, 100))
    model = ws.rerank_model
    if not model or query.regex or mode == "text" or len(hits) < 2:
        return hits[:top]
    config = _rerank_config(session, ws, spaces_)
    if config is None:
        return hits[:top]
    pool = hits[:RERANK_POOL]
    try:
        scores = (reranker or rerank.rerank)(
            config, model, query.text, [_rerank_text(h) for h in pool]
        )
    except AIError as error:
        log.warning("Reranking skipped: %s", error)
        return hits[:top]
    return (_rerank_order(pool, scores) + hits[len(pool) :])[:top]


def search(
    session: Session,
    actor: Actor,
    ws: Workspace,
    text: str,
    *,
    addresses: Sequence[str] = (),
    regex: bool = False,
    limit: int = 20,
    mode: str = "auto",
    embedder: Embedder | None = None,
) -> list[Hit]:
    """Searches the readable spaces, optionally restricted to `org/space`.

    `mode`: `auto` (words, and meaning where embeddings are on) or `text` (words only). A
    regular expression is always searched by words."""
    query = Query.parse(text, regex)
    visible = spaces.list_all_visible_spaces(session, actor)
    if addresses:
        wanted = set(addresses)
        known = {s.address for s in visible}
        if unknown := sorted(wanted - known):
            raise not_found("Space not found: {spaces}.", spaces=", ".join(unknown))
        visible = [s for s in visible if s.address in wanted]
    for space in visible:
        _ = space.organization  # loaded here: the search threads do not use the session
    session.commit()  # connection returned to the pool during ripgrep (sometimes seconds)
    # Two **global** passes: all spaces ranked (counts), then snippets for the best results
    # only, whatever the number of spaces.
    keep = max(min(limit, 100) * 2, 20)
    with ThreadPoolExecutor(max_workers=min(8, len(visible) or 1)) as pool:
        space_ranks = list(pool.map(lambda s: _rank_space(ws, s, query), visible))
        scored = [
            (space.id, entry)
            for space, found in zip(visible, space_ranks, strict=True)
            if found is not None
            for entry in found[1][:MAX_HITS_PER_SPACE]
        ]
        scored.sort(
            key=lambda item: (-item[1].score, -item[1].modified, str(item[0]), item[1].path)
        )
        chosen: dict[object, list[_Candidate]] = {}
        for space_id, entry in scored[:keep]:
            chosen.setdefault(space_id, []).append(entry)
        jobs = [
            (space, found[0], chosen.get(space.id, []))
            for space, found in zip(visible, space_ranks, strict=True)
            if found is not None
        ]
        results = pool.map(lambda job: _hits(job[0], job[1], query, job[2]), jobs)
        lexical: dict[Space, list[Hit]] = {space: [] for space in visible}
        lexical.update({job[0]: hits for job, hits in zip(jobs, results, strict=True)})
    meaning = (
        {}
        if regex or mode == "text"
        else _semantic_lists(session, visible, query, ws, embedder or embeddings.embed)
    )
    if not meaning:
        hits = [hit for space_hits in lexical.values() for hit in space_hits]
        hits.sort(key=lambda h: (-h.score, -h.modified, h.address, h.path))
        return _reranked(session, ws, visible, query, hits, limit, mode)
    # RRF fusion: **global** lexical rank (word scores compare across spaces), then the
    # contribution of meaning added where it exists. A page is never penalized because its
    # space has embeddings.
    every = sorted(
        (hit for space_hits in lexical.values() for hit in space_hits),
        key=lambda h: (-h.score, -h.modified, h.address, h.path),
    )
    fused: dict[tuple[str, str], tuple[float, Hit]] = {
        (hit.address, hit.path): (1 / (RRF_K + rank), hit)
        for rank, hit in enumerate(every, start=1)
    }
    for space, space_hits in lexical.items():
        if space.id not in meaning:
            continue
        for extra, hit in _meaning(space, space_hits, meaning[space.id], ws):
            key = (hit.address, hit.path)
            fused[key] = (fused.get(key, (0.0, hit))[0] + extra, hit)
    ranked = sorted(
        fused.values(), key=lambda item: (-item[0], -item[1].score, item[1].address, item[1].path)
    )
    return _reranked(session, ws, visible, query, [hit for _, hit in ranked], limit, mode)
