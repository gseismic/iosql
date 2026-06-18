from dataclasses import dataclass
from typing import Any, List, Optional

from ...exception import BadArgument
from .case import Case


@dataclass
class _Token:
    type: str
    value: Any


_KEYWORDS = {"and", "or", "in", "between", "true", "false"}
_COMPARISON_OPS = {"=", "==", "!=", ">", ">=", "<", "<="}


def parse_text_where(text: str):
    """Parse a small SQL-like where expression into Case/CaseExpression."""
    if text is None:
        return None
    parser = _TextWhereParser(_tokenize(text))
    return parser.parse()


def _tokenize(text: str) -> List[_Token]:
    text = str(text)
    tokens = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if ch.isalpha() or ch == "_":
            start = i
            i += 1
            while i < len(text) and (text[i].isalnum() or text[i] == "_"):
                i += 1
            value = text[start:i]
            lower = value.lower()
            if lower in _KEYWORDS:
                tokens.append(_Token("KEYWORD", lower))
            else:
                tokens.append(_Token("IDENT", value))
            continue
        if ch in ["'", '"']:
            value, i = _read_string(text, i)
            tokens.append(_Token("STRING", value))
            continue
        if ch.isdigit() or (
            ch in ["+", "-"]
            and i + 1 < len(text)
            and text[i + 1].isdigit()
        ):
            value, i = _read_number(text, i)
            tokens.append(_Token("NUMBER", value))
            continue
        if text[i:i + 2] in _COMPARISON_OPS:
            tokens.append(_Token("OP", text[i:i + 2]))
            i += 2
            continue
        if ch in _COMPARISON_OPS:
            tokens.append(_Token("OP", ch))
            i += 1
            continue
        if ch == "(":
            tokens.append(_Token("LPAREN", ch))
            i += 1
            continue
        if ch == ")":
            tokens.append(_Token("RPAREN", ch))
            i += 1
            continue
        if ch == ",":
            tokens.append(_Token("COMMA", ch))
            i += 1
            continue
        raise BadArgument(f"Unsupported token `{ch}` in text where")
    tokens.append(_Token("EOF", None))
    return tokens


def _read_string(text: str, start: int):
    quote = text[start]
    i = start + 1
    chars = []
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            chars.append(text[i + 1])
            i += 2
            continue
        if ch == quote:
            if i + 1 < len(text) and text[i + 1] == quote:
                chars.append(quote)
                i += 2
                continue
            return "".join(chars), i + 1
        chars.append(ch)
        i += 1
    raise BadArgument("Unterminated string in text where")


def _read_number(text: str, start: int):
    i = start
    if text[i] in ["+", "-"]:
        i += 1
    has_dot = False
    while i < len(text):
        ch = text[i]
        if ch.isdigit():
            i += 1
            continue
        if ch == "." and not has_dot:
            has_dot = True
            i += 1
            continue
        break
    raw = text[start:i]
    try:
        if has_dot:
            return float(raw), i
        return int(raw), i
    except ValueError as e:
        raise BadArgument(f"Bad number `{raw}` in text where") from e


class _TextWhereParser:
    def __init__(self, tokens: List[_Token]):
        self._tokens = tokens
        self._pos = 0

    def parse(self):
        if self._peek().type == "EOF":
            return None
        expr = self._parse_or()
        self._expect("EOF")
        return expr

    def _parse_or(self):
        expr = self._parse_and()
        while self._match_keyword("or"):
            expr = expr + self._parse_and()
        return expr

    def _parse_and(self):
        expr = self._parse_factor()
        while self._match_keyword("and"):
            expr = expr * self._parse_factor()
        return expr

    def _parse_factor(self):
        if self._match("LPAREN"):
            expr = self._parse_or()
            self._expect("RPAREN")
            return expr
        return self._parse_predicate()

    def _parse_predicate(self):
        field = self._expect("IDENT").value
        if self._match_keyword("between"):
            start = self._parse_value()
            self._expect_keyword("and")
            end = self._parse_value()
            return Case(field, "between_and", [start, end])
        if self._match_keyword("in"):
            self._expect("LPAREN")
            values = [self._parse_value()]
            while self._match("COMMA"):
                values.append(self._parse_value())
            self._expect("RPAREN")
            return Case(field, "in", values)

        op = self._expect("OP").value
        if op not in _COMPARISON_OPS:
            raise BadArgument(f"Unsupported op `{op}` in text where")
        return Case(field, op, self._parse_value())

    def _parse_value(self):
        token = self._peek()
        if token.type in ["STRING", "NUMBER"]:
            self._advance()
            return token.value
        if token.type == "KEYWORD" and token.value in ["true", "false"]:
            self._advance()
            return token.value == "true"
        raise BadArgument("Expected literal value in text where")

    def _peek(self) -> _Token:
        return self._tokens[self._pos]

    def _advance(self) -> _Token:
        token = self._peek()
        self._pos += 1
        return token

    def _match(self, token_type: str) -> Optional[_Token]:
        if self._peek().type != token_type:
            return None
        return self._advance()

    def _expect(self, token_type: str) -> _Token:
        token = self._match(token_type)
        if token is None:
            raise BadArgument(f"Expected {token_type} in text where")
        return token

    def _match_keyword(self, value: str) -> bool:
        token = self._peek()
        if token.type == "KEYWORD" and token.value == value:
            self._advance()
            return True
        return False

    def _expect_keyword(self, value: str):
        if not self._match_keyword(value):
            raise BadArgument(f"Expected `{value}` in text where")
