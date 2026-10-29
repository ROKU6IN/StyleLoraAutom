"""Lossless comma-v1 recognition. Recognizing prompt syntax does not evaluate it."""
from dataclasses import dataclass
import math
import re

from ..exchange.errors import ExchangeError
from ..project_db.types import Tag
from ..project_db.errors import ValidationError
from ..project_db.validation import normalize_tag
from .plain import validate_plain_tag

VERSION = 'comma-v1'


@dataclass(frozen=True)
class Token:
    raw: str
    normalized: str
    syntax: str
    escaped: bool
    weight: float | None
    lookup: str
    canonical: str | None
    start: int
    end: int
    reason: str | None = None


@dataclass(frozen=True)
class ParsedPrompt:
    tags: tuple[Tag, ...]
    natural_language: str | None
    segments: tuple[Token, ...]
    tokenizer_version: str
    unverifiable: tuple[str, ...]


def tokenize_prompt(text, *, catalog=None):
    # Split only literal commas, while marking every segment touched by a
    # bracket/escape span. A trailing segment must not be misclassified as plain.
    pieces = text.split(',') if text else []
    unsafe, stack, segment = set(), [], 0
    for index, char in enumerate(text):
        if char == ',':
            slashes, previous = 0, index-1
            while previous >= 0 and text[previous] == '\\':
                slashes += 1
                previous -= 1
            if stack or slashes % 2:
                unsafe.update((segment, segment+1))
            segment += 1
        elif char in '([{':
            stack.append((char, segment))
        elif char in ')]}':
            if stack and '([{'.index(stack[-1][0]) == ')]}'.index(char):
                stack.pop()  # Commas already marked every segment within this span.
            else:
                unsafe.add(segment)
    for _, start in stack:
        unsafe.add(start)  # Avoid quadratic rescans of nested, unterminated spans.
    result, offset = [], 0
    for index, raw in enumerate(pieces):
        plain = raw.strip()
        try:
            key = normalize_tag(plain) if plain else ''
        except ValidationError:
            key = ''
        canonical = catalog.lookup(plain) if catalog and key else None
        syntax, reason, weight = 'plain', None, None
        escaped = '\\' in raw
        number = re.search(r':([+-]?(?:\d+(?:\.\d*)?|\.\d+))\)?$', plain)
        if number:
            candidate = float(number[1])
            weight = candidate if math.isfinite(candidate) else None
        if index in unsafe:
            syntax, reason = 'unknown', 'tokenizer:cross_segment_syntax'
        elif escaped:
            syntax, reason = 'escaped', 'tokenizer:escape_unsupported'
        elif not key:
            syntax, reason = 'unknown', 'tag:empty_segment'
        elif re.search(r'\b(?:BREAK|AND)\b', plain) or '|' in plain:
            syntax, reason = 'separator', 'tokenizer:control_unsupported'
        else:
            try:
                validate_plain_tag(plain)
                if any(c in plain for c in '()') and canonical is None:
                    syntax, reason = 'unknown', 'tokenizer:unverified_parentheses'
            except ExchangeError:
                syntax = 'weight' if number or any(c in plain for c in '()[]{}:') else 'unknown'
                reason = 'tokenizer:syntax_unsupported'
        lookup = ('unverifiable' if reason or not catalog or not catalog.entries else
                  'unknown' if canonical is None else 'alias' if canonical != key else 'known')
        result.append(Token(raw, key, syntax, escaped, weight, lookup, canonical if not reason else None,
                            offset, offset+len(raw), reason))
        offset += len(raw)+1
    return tuple(result)


def parse_original_prompt(text, *, catalog):
    if catalog is None or not catalog.entries:
        raise ExchangeError('catalog:required')
    tokens = tokenize_prompt(text, catalog=catalog)
    tags, nl, seen, unverifiable = [], [], set(), []
    for token in tokens:
        if token.lookup in ('known','alias'):
            if token.canonical not in seen:
                tags.append(Tag(token.canonical))
                seen.add(token.canonical)
        else:
            nl.append(token.raw.strip())
            if token.lookup == 'unverifiable':
                unverifiable.append(token.raw)
    return ParsedPrompt(tuple(tags), ', '.join(nl) or None, tokens, VERSION, tuple(unverifiable))
