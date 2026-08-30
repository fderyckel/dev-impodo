"""Semantic comparison for Odoo HTML field read-back values."""

from __future__ import annotations

from html.parser import HTMLParser


_VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)


class _CanonicalHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tokens: list[tuple[object, ...]] = []
        self.stack: list[str] = []
        self.valid = True

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        normalized_tag = tag.lower()
        normalized_attrs = tuple(
            sorted((name.lower(), value) for name, value in attrs)
        )
        if normalized_tag in _VOID_TAGS:
            self.tokens.append(("void", normalized_tag, normalized_attrs))
            return
        self.tokens.append(("start", normalized_tag, normalized_attrs))
        self.stack.append(normalized_tag)

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        normalized_tag = tag.lower()
        normalized_attrs = tuple(
            sorted((name.lower(), value) for name, value in attrs)
        )
        if normalized_tag in _VOID_TAGS:
            self.tokens.append(("void", normalized_tag, normalized_attrs))
            return
        self.tokens.extend(
            (
                ("start", normalized_tag, normalized_attrs),
                ("end", normalized_tag),
            )
        )

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.lower()
        if normalized_tag in _VOID_TAGS:
            return
        if not self.stack or self.stack[-1] != normalized_tag:
            self.valid = False
            return
        self.stack.pop()
        self.tokens.append(("end", normalized_tag))

    def handle_data(self, data: str) -> None:
        normalized = data.replace("\r\n", "\n").replace("\r", "\n")
        if normalized:
            self.tokens.append(("text", normalized))

    def handle_comment(self, data: str) -> None:
        del data

    def unknown_decl(self, data: str) -> None:
        del data
        self.valid = False


def odoo_html_values_equal(expected: object, actual: object) -> bool:
    """Compare HTML fragments without mistaking serialization for fallout.

    The comparison retains meaningful tags and attributes. It only removes
    differences that do not change the fragment: character-reference spelling,
    tag/attribute case and ordering, void-tag spelling, line endings, comments,
    surrounding root whitespace, and Odoo's optional outer ``<p>`` wrapper.
    """

    if expected == actual:
        return True
    if not isinstance(expected, str) or not isinstance(actual, str):
        return False
    expected_tokens = _canonical_html_fragment(expected)
    actual_tokens = _canonical_html_fragment(actual)
    return (
        expected_tokens is not None
        and actual_tokens is not None
        and expected_tokens == actual_tokens
    )


def _canonical_html_fragment(
    value: str,
) -> tuple[tuple[object, ...], ...] | None:
    parser = _CanonicalHtmlParser()
    try:
        parser.feed(value)
        parser.close()
    except (ValueError, TypeError):
        return None
    if not parser.valid or parser.stack:
        return None
    tokens = _merge_text_tokens(tuple(parser.tokens))
    tokens = _trim_root_whitespace(tokens)
    tokens = _unwrap_outer_paragraph(tokens)
    return _merge_text_tokens(tokens)


def _merge_text_tokens(
    tokens: tuple[tuple[object, ...], ...],
) -> tuple[tuple[object, ...], ...]:
    merged: list[tuple[object, ...]] = []
    for token in tokens:
        if token[0] == "text" and merged and merged[-1][0] == "text":
            merged[-1] = ("text", str(merged[-1][1]) + str(token[1]))
        else:
            merged.append(token)
    return tuple(merged)


def _trim_root_whitespace(
    tokens: tuple[tuple[object, ...], ...],
) -> tuple[tuple[object, ...], ...]:
    start = 0
    end = len(tokens)
    while start < end and _whitespace_token(tokens[start]):
        start += 1
    while end > start and _whitespace_token(tokens[end - 1]):
        end -= 1
    return tokens[start:end]


def _whitespace_token(token: tuple[object, ...]) -> bool:
    return token[0] == "text" and not str(token[1]).strip()


def _unwrap_outer_paragraph(
    tokens: tuple[tuple[object, ...], ...],
) -> tuple[tuple[object, ...], ...]:
    if (
        len(tokens) < 2
        or tokens[0] != ("start", "p", ())
        or tokens[-1] != ("end", "p")
    ):
        return tokens
    depth = 0
    for index, token in enumerate(tokens):
        if token[0] == "start":
            depth += 1
        elif token[0] == "end":
            depth -= 1
            if depth == 0 and index != len(tokens) - 1:
                return tokens
    return tokens[1:-1]


__all__ = ["odoo_html_values_equal"]
