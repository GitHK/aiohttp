# type: ignore
# Tests for aiohttp/protocol.py

import asyncio
import re
from contextlib import nullcontext
from typing import Any, Dict, List
from unittest import mock
from urllib.parse import quote

import pytest
from multidict import CIMultiDict
from yarl import URL

import aiohttp
from aiohttp import http_exceptions, streams
from aiohttp.http_parser import (
    NO_EXTENSIONS,
    DeflateBuffer,
    HttpPayloadParser,
    HttpRequestParserPy,
    HttpResponseParserPy,
    HttpVersion,
)

try:
    try:
        import brotlicffi as brotli
    except ImportError:
        import brotli
except ImportError:
    brotli = None


REQUEST_PARSERS: Any = [HttpRequestParserPy]
RESPONSE_PARSERS: Any = [HttpResponseParserPy]

try:
    from aiohttp.http_parser import HttpRequestParserC, HttpResponseParserC

    REQUEST_PARSERS.append(HttpRequestParserC)
    RESPONSE_PARSERS.append(HttpResponseParserC)
except ImportError:  # pragma: no cover
    pass


@pytest.fixture
def protocol():
    return mock.Mock()


def _gen_ids(parsers: List[Any]) -> List[str]:
    return [
        "py-parser" if parser.__module__ == "aiohttp.http_parser" else "c-parser"
        for parser in parsers
    ]


@pytest.fixture(params=REQUEST_PARSERS, ids=_gen_ids(REQUEST_PARSERS))
def parser(loop: Any, protocol: Any, request: Any):
    # Parser implementations
    return request.param(
        protocol,
        loop,
        2**16,
        max_line_size=8190,
        max_field_size=8190,
    )


@pytest.fixture(params=REQUEST_PARSERS, ids=_gen_ids(REQUEST_PARSERS))
def request_cls(request: Any):
    # Request Parser class
    return request.param


@pytest.fixture(params=RESPONSE_PARSERS, ids=_gen_ids(RESPONSE_PARSERS))
def response(loop: Any, protocol: Any, request: Any):
    # Parser implementations
    return request.param(
        protocol,
        loop,
        2**16,
        max_line_size=8190,
        max_field_size=8190,
    )


@pytest.fixture(params=RESPONSE_PARSERS, ids=_gen_ids(RESPONSE_PARSERS))
def response_cls(request: Any):
    # Parser implementations
    return request.param


@pytest.fixture
def stream():
    return mock.Mock()


@pytest.mark.skipif(NO_EXTENSIONS, reason="Extensions available but not imported")
def test_c_parser_loaded():
    assert "HttpRequestParserC" in dir(aiohttp.http_parser)
    assert "HttpResponseParserC" in dir(aiohttp.http_parser)
    assert "RawRequestMessageC" in dir(aiohttp.http_parser)
    assert "RawResponseMessageC" in dir(aiohttp.http_parser)


def test_parse_headers(parser: Any) -> None:
    text = b"""GET /test HTTP/1.1\r
test: line\r
 continue\r
test2: data\r
\r
"""
    messages, upgrade, tail = parser.feed_data(text)
    assert len(messages) == 1
    msg = messages[0][0]

    assert list(msg.headers.items()) == [("test", "line continue"), ("test2", "data")]
    assert msg.raw_headers == ((b"test", b"line continue"), (b"test2", b"data"))
    assert not msg.should_close
    assert msg.compression is None
    assert not msg.upgrade


@pytest.mark.skipif(NO_EXTENSIONS, reason="Only tests C parser.")
def test_invalid_character(loop: Any, protocol: Any, request: Any) -> None:
    parser = HttpRequestParserC(
        protocol,
        loop,
        2**16,
        max_line_size=8190,
        max_field_size=8190,
    )
    text = b"POST / HTTP/1.1\r\nHost: localhost:8080\r\nSet-Cookie: abc\x01def\r\n\r\n"
    error_detail = re.escape(
        r""":

    b'Set-Cookie: abc\x01def'
                     ^"""
    )
    with pytest.raises(http_exceptions.BadHttpMessage, match=error_detail):
        parser.feed_data(text)


@pytest.mark.skipif(NO_EXTENSIONS, reason="Only tests C parser.")
def test_invalid_linebreak(loop: Any, protocol: Any, request: Any) -> None:
    parser = HttpRequestParserC(
        protocol,
        loop,
        2**16,
        max_line_size=8190,
        max_field_size=8190,
    )
    text = b"GET /world HTTP/1.1\r\nHost: 127.0.0.1\n\r\n"
    error_detail = re.escape(
        r""":

    b'Host: 127.0.0.1\n'
                     ^"""
    )
    with pytest.raises(http_exceptions.BadHttpMessage, match=error_detail):
        parser.feed_data(text)


def test_cve_2023_37276(parser: Any) -> None:
    text = b"""POST / HTTP/1.1\r\nHost: localhost:8080\r\nX-Abc: \rxTransfer-Encoding: chunked\r\n\r\n"""
    with pytest.raises(http_exceptions.BadHttpMessage):
        parser.feed_data(text)


@pytest.mark.parametrize(
    "rfc9110_5_6_2_token_delim",
    r'"(),/:;<=>?@[\]{}',
)
def test_bad_header_name(parser: Any, rfc9110_5_6_2_token_delim: str) -> None:
    text = f"POST / HTTP/1.1\r\nhead{rfc9110_5_6_2_token_delim}er: val\r\n\r\n".encode()
    expectation = pytest.raises(http_exceptions.BadHttpMessage)
    if rfc9110_5_6_2_token_delim == ":":
        # Inserting colon into header just splits name/value earlier.
        expectation = nullcontext()
    with expectation:
        parser.feed_data(text)


@pytest.mark.parametrize(
    "hdr",
    (
        "Content-Length: -5",  # https://www.rfc-editor.org/rfc/rfc9110.html#name-content-length
        "Content-Length: +256",
        "Content-Length: \N{superscript one}",
        "Content-Length: \N{mathematical double-struck digit one}",
        "Foo: abc\rdef",  # https://www.rfc-editor.org/rfc/rfc9110.html#section-5.5-5
        "Bar: abc\ndef",
        "Baz: abc\x00def",
        "Foo : bar",  # https://www.rfc-editor.org/rfc/rfc9112.html#section-5.1-2
        "Foo\t: bar",
        "\xffoo: bar",
    ),
)
def test_bad_headers(parser: Any, hdr: str) -> None:
    text = f"POST / HTTP/1.1\r\n{hdr}\r\n\r\n".encode()
    with pytest.raises(http_exceptions.BadHttpMessage):
        parser.feed_data(text)


def test_unpaired_surrogate_in_header_py(loop: Any, protocol: Any) -> None:
    parser = HttpRequestParserPy(
        protocol,
        loop,
        2**16,
        max_line_size=8190,
        max_field_size=8190,
    )
    text = b"POST / HTTP/1.1\r\n\xff\r\n\r\n"
    message = None
    try:
        parser.feed_data(text)
    except http_exceptions.InvalidHeader as e:
        message = e.message.encode("utf-8")
    assert message is not None


def test_content_length_transfer_encoding(parser: Any) -> None:
    text = (
        b"GET / HTTP/1.1\r\nHost: a\r\nContent-Length: 5\r\nTransfer-Encoding: a\r\n\r\n"
        + b"apple\r\n"
    )
    with pytest.raises(http_exceptions.BadHttpMessage):
        parser.feed_data(text)


def test_bad_chunked_py(loop: Any, protocol: Any) -> None:
    """Test that invalid chunked encoding doesn't allow content-length to be used."""
    parser = HttpRequestParserPy(
        protocol,
        loop,
        2**16,
        max_line_size=8190,
        max_field_size=8190,
    )
    text = (
        b"GET / HTTP/1.1\r\nHost: a\r\nTransfer-Encoding: chunked\r\n\r\n0_2e\r\n\r\n"
        + b"GET / HTTP/1.1\r\nHost: a\r\nContent-Length: 5\r\n\r\n0\r\n\r\n"
    )
    messages, upgrade, tail = parser.feed_data(text)
    assert isinstance(messages[0][1].exception(), http_exceptions.TransferEncodingError)


@pytest.mark.skipif(
    "HttpRequestParserC" not in dir(aiohttp.http_parser),
    reason="C based HTTP parser not available",
)
def test_bad_chunked_c(loop: Any, protocol: Any) -> None:
    """C parser behaves differently. Maybe we should align them later."""
    parser = HttpRequestParserC(
        protocol,
        loop,
        2**16,
        max_line_size=8190,
        max_field_size=8190,
    )
    text = (
        b"GET / HTTP/1.1\r\nHost: a\r\nTransfer-Encoding: chunked\r\n\r\n0_2e\r\n\r\n"
        + b"GET / HTTP/1.1\r\nHost: a\r\nContent-Length: 5\r\n\r\n0\r\n\r\n"
    )
    with pytest.raises(http_exceptions.BadHttpMessage):
        parser.feed_data(text)


def test_whitespace_before_header(parser: Any) -> None:
    text = b"GET / HTTP/1.1\r\n\tContent-Length: 1\r\n\r\nX"
    with pytest.raises(http_exceptions.BadHttpMessage):
        parser.feed_data(text)


def test_parse_headers_longline(parser: Any) -> None:
    invalid_unicode_byte = b"\xd9"
    header_name = b"Test" + invalid_unicode_byte + b"Header" + b"A" * 8192
    text = b"GET /test HTTP/1.1\r\n" + header_name + b": test\r\n" + b"\r\n" + b"\r\n"
    with pytest.raises((http_exceptions.LineTooLong, http_exceptions.BadHttpMessage)):
        # FIXME: `LineTooLong` doesn't seem to actually be happening
        parser.feed_data(text)


@pytest.fixture
def xfail_c_parser_status(request) -> None:
    if isinstance(request.getfixturevalue("parser"), HttpRequestParserPy):
        return
    request.node.add_marker(
        pytest.mark.xfail(
            reason="Regression test for Py parser. May match C behaviour later.",
            raises=http_exceptions.BadStatusLine,
        )
    )


@pytest.mark.usefixtures("xfail_c_parser_status")
def test_parse_unusual_request_line(parser: Any) -> None:
    text = b"#smol //a HTTP/1.3\r\n\r\n"
    messages, upgrade, tail = parser.feed_data(text)
    assert len(messages) == 1
    msg, _ = messages[0]
    assert msg.compression is None
    assert not msg.upgrade
    assert msg.method == "#smol"
    assert msg.path == "//a"
    assert msg.version == (1, 3)


def test_parse(parser: Any) -> None:
    text = b"GET /test HTTP/1.1\r\n\r\n"
    messages, upgrade, tail = parser.feed_data(text)
    assert len(messages) == 1
    msg, _ = messages[0]
    assert msg.compression is None
    assert not msg.upgrade
    assert msg.method == "GET"
    assert msg.path == "/test"
    assert msg.version == (1, 1)


async def test_parse_body(parser: Any) -> None:
    text = b"GET /test HTTP/1.1\r\nContent-Length: 4\r\n\r\nbody"
    messages, upgrade, tail = parser.feed_data(text)
    assert len(messages) == 1
    _, payload = messages[0]
    body = await payload.read(4)
    assert body == b"body"


async def test_parse_body_with_CRLF(parser: Any) -> None:
    text = b"\r\nGET /test HTTP/1.1\r\nContent-Length: 4\r\n\r\nbody"
    messages, upgrade, tail = parser.feed_data(text)
    assert len(messages) == 1
    _, payload = messages[0]
    body = await payload.read(4)
    assert body == b"body"


def test_parse_delayed(parser: Any) -> None:
    text = b"GET /test HTTP/1.1\r\n"
    messages, upgrade, tail = parser.feed_data(text)
    assert len(messages) == 0
    assert not upgrade

    messages, upgrade, tail = parser.feed_data(b"\r\n")
    assert len(messages) == 1
    msg = messages[0][0]
    assert msg.method == "GET"


def test_headers_multi_feed(parser: Any) -> None:
    text1 = b"GET /test HTTP/1.1\r\n"
    text2 = b"test: line\r"
    text3 = b"\n continue\r\n\r\n"

    messages, upgrade, tail = parser.feed_data(text1)
    assert len(messages) == 0

    messages, upgrade, tail = parser.feed_data(text2)
    assert len(messages) == 0

    messages, upgrade, tail = parser.feed_data(text3)
    assert len(messages) == 1

    msg = messages[0][0]
    assert list(msg.headers.items()) == [("test", "line continue")]
    assert msg.raw_headers == ((b"test", b"line continue"),)
    assert not msg.should_close
    assert msg.compression is None
    assert not msg.upgrade


def test_headers_split_field(parser: Any) -> None:
    text1 = b"GET /test HTTP/1.1\r\n"
    text2 = b"t"
    text3 = b"es"
    text4 = b"t: value\r\n\r\n"

    messages, upgrade, tail = parser.feed_data(text1)
    messages, upgrade, tail = parser.feed_data(text2)
    messages, upgrade, tail = parser.feed_data(text3)
    assert len(messages) == 0
    messages, upgrade, tail = parser.feed_data(text4)
    assert len(messages) == 1

    msg = messages[0][0]
    assert list(msg.headers.items()) == [("test", "value")]
    assert msg.raw_headers == ((b"test", b"value"),)
    assert not msg.should_close
    assert msg.compression is None
    assert not msg.upgrade


def test_parse_headers_multi(parser: Any) -> None:
    text = (
        b"GET /test HTTP/1.1\r\n"
        b"Set-Cookie: c1=cookie1\r\n"
        b"Set-Cookie: c2=cookie2\r\n\r\n"
    )

    messages, upgrade, tail = parser.feed_data(text)
    assert len(messages) == 1
    msg = messages[0][0]

    assert list(msg.headers.items()) == [
        ("Set-Cookie", "c1=cookie1"),
        ("Set-Cookie", "c2=cookie2"),
    ]
    assert msg.raw_headers == (
        (b"Set-Cookie", b"c1=cookie1"),
        (b"Set-Cookie", b"c2=cookie2"),
    )
    assert not msg.should_close
    assert msg.compression is None


def test_conn_default_1_0(parser: Any) -> None:
    text = b"GET /test HTTP/1.0\r\n\r\n"
    messages, upgrade, tail = parser.feed_data(text)
    msg = messages[0][0]
    assert msg.should_close


def test_conn_default_1_1(parser: Any) -> None:
    text = b"GET /test HTTP/1.1\r\n\r\n"
    messages, upgrade, tail = parser.feed_data(text)
    msg = messages[0][0]
    assert not msg.should_close


def test_conn_close(parser: Any) -> None:
    text = b"GET /test HTTP/1.1\r\n" b"connection: close\r\n\r\n"
    messages, upgrade, tail = parser.feed_data(text)
    msg = messages[0][0]
    assert msg.should_close


def test_conn_close_1_0(parser: Any) -> None:
    text = b"GET /test HTTP/1.0\r\n" b"connection: close\r\n\r\n"
    messages, upgrade, tail = parser.feed_data(text)
    msg = messages[0][0]
    assert msg.should_close


def test_conn_keep_alive_1_0(parser: Any) -> None:
    text = b"GET /test HTTP/1.0\r\n" b"connection: keep-alive\r\n\r\n"
    messages, upgrade, tail = parser.feed_data(text)
    msg = messages[0][0]
    assert not msg.should_close


def test_conn_keep_alive_1_1(parser: Any) -> None:
    text = b"GET /test HTTP/1.1\r\n" b"connection: keep-alive\r\n\r\n"
    messages, upgrade, tail = parser.feed_data(text)
    msg = messages[0][0]
    assert not msg.should_close


def test_conn_other_1_0(parser: Any) -> None:
    text = b"GET /test HTTP/1.0\r\n" b"connection: test\r\n\r\n"
    messages, upgrade, tail = parser.feed_data(text)
    msg = messages[0][0]
    assert msg.should_close


def test_conn_other_1_1(parser: Any) -> None:
    text = b"GET /test HTTP/1.1\r\n" b"connection: test\r\n\r\n"
    messages, upgrade, tail = parser.feed_data(text)
    msg = messages[0][0]
    assert not msg.should_close


def test_request_chunked(parser: Any) -> None:
    text = b"GET /test HTTP/1.1\r\n" b"transfer-encoding: chunked\r\n\r\n"
    messages, upgrade, tail = parser.feed_data(text)
    msg, payload = messages[0]
    assert msg.chunked
    assert not upgrade
    assert isinstance(payload, streams.StreamReader)


def test_request_te_chunked_with_content_length(parser: Any) -> None:
    text = (
        b"GET /test HTTP/1.1\r\n"
        b"content-length: 1234\r\n"
        b"transfer-encoding: chunked\r\n\r\n"
    )
    with pytest.raises(
        http_exceptions.BadHttpMessage,
        match="Transfer-Encoding can't be present with Content-Length",
    ):
        parser.feed_data(text)


def test_request_te_chunked123(parser: Any) -> None:
    text = b"GET /test HTTP/1.1\r\n" b"transfer-encoding: chunked123\r\n\r\n"
    with pytest.raises(
        http_exceptions.BadHttpMessage,
        match="Request has invalid `Transfer-Encoding`",
    ):
        parser.feed_data(text)


def test_conn_upgrade(parser: Any) -> None:
    text = (
        b"GET /test HTTP/1.1\r\n"
        b"connection: upgrade\r\n"
        b"upgrade: websocket\r\n\r\n"
    )
    messages, upgrade, tail = parser.feed_data(text)
    msg = messages[0][0]
    assert not msg.should_close
    assert msg.upgrade
    assert upgrade


def test_bad_upgrade(parser: Any) -> None:
    """Test not upgraded if missing Upgrade header."""
    text = b"GET /test HTTP/1.1\r\nconnection: upgrade\r\n\r\n"
    messages, upgrade, tail = parser.feed_data(text)
    msg = messages[0][0]
    assert not msg.upgrade
    assert not upgrade


def test_compression_empty(parser: Any) -> None:
    text = b"GET /test HTTP/1.1\r\n" b"content-encoding: \r\n\r\n"
    messages, upgrade, tail = parser.feed_data(text)
    msg = messages[0][0]
    assert msg.compression is None


def test_compression_deflate(parser: Any) -> None:
    text = b"GET /test HTTP/1.1\r\n" b"content-encoding: deflate\r\n\r\n"
    messages, upgrade, tail = parser.feed_data(text)
    msg = messages[0][0]
    assert msg.compression == "deflate"


def test_compression_gzip(parser: Any) -> None:
    text = b"GET /test HTTP/1.1\r\n" b"content-encoding: gzip\r\n\r\n"
    messages, upgrade, tail = parser.feed_data(text)
    msg = messages[0][0]
    assert msg.compression == "gzip"


@pytest.mark.skipif(brotli is None, reason="brotli is not installed")
def test_compression_brotli(parser: Any) -> None:
    text = b"GET /test HTTP/1.1\r\n" b"content-encoding: br\r\n\r\n"
    messages, upgrade, tail = parser.feed_data(text)
    msg = messages[0][0]
    assert msg.compression == "br"


def test_compression_unknown(parser: Any) -> None:
    text = b"GET /test HTTP/1.1\r\n" b"content-encoding: compress\r\n\r\n"
    messages, upgrade, tail = parser.feed_data(text)
    msg = messages[0][0]
    assert msg.compression is None


def test_url_connect(parser: Any) -> None:
    text = b"CONNECT www.google.com HTTP/1.1\r\n" b"content-length: 0\r\n\r\n"
    messages, upgrade, tail = parser.feed_data(text)
    msg, payload = messages[0]
    assert upgrade
    assert msg.url == URL.build(authority="www.google.com")


def test_headers_connect(parser: Any) -> None:
    text = b"CONNECT www.google.com HTTP/1.1\r\n" b"content-length: 0\r\n\r\n"
    messages, upgrade, tail = parser.feed_data(text)
    msg, payload = messages[0]
    assert upgrade
    assert isinstance(payload, streams.StreamReader)


def test_url_absolute(parser: Any) -> None:
    text = (
        b"GET https://www.google.com/path/to.html HTTP/1.1\r\n"
        b"content-length: 0\r\n\r\n"
    )
    messages, upgrade, tail = parser.feed_data(text)
    msg, payload = messages[0]
    assert not upgrade
    assert msg.method == "GET"
    assert msg.url == URL("https://www.google.com/path/to.html")


def test_headers_old_websocket_key1(parser: Any) -> None:
    text = b"GET /test HTTP/1.1\r\n" b"SEC-WEBSOCKET-KEY1: line\r\n\r\n"

    with pytest.raises(http_exceptions.BadHttpMessage):
        parser.feed_data(text)


def test_headers_content_length_err_1(parser: Any) -> None:
    text = b"GET /test HTTP/1.1\r\n" b"content-length: line\r\n\r\n"

    with pytest.raises(http_exceptions.BadHttpMessage):
        parser.feed_data(text)


def test_headers_content_length_err_2(parser: Any) -> None:
    text = b"GET /test HTTP/1.1\r\n" b"content-length: -1\r\n\r\n"

    with pytest.raises(http_exceptions.BadHttpMessage):
        parser.feed_data(text)


_pad: Dict[bytes, str] = {
    b"": "empty",
    # not a typo. Python likes triple zero
    b"\000": "NUL",
    b" ": "SP",
    b"  ": "SPSP",
    # not a typo: both 0xa0 and 0x0a in case of 8-bit fun
    b"\n": "LF",
    b"\xa0": "NBSP",
    b"\t ": "TABSP",
}


@pytest.mark.parametrize("hdr", [b"", b"foo"], ids=["name-empty", "with-name"])
@pytest.mark.parametrize("pad2", _pad.keys(), ids=["post-" + n for n in _pad.values()])
@pytest.mark.parametrize("pad1", _pad.keys(), ids=["pre-" + n for n in _pad.values()])
def test_invalid_header_spacing(
    parser: Any, pad1: bytes, pad2: bytes, hdr: bytes
) -> None:
    text = b"GET /test HTTP/1.1\r\n" b"%s%s%s: value\r\n\r\n" % (pad1, hdr, pad2)
    expectation = pytest.raises(http_exceptions.BadHttpMessage)
    if pad1 == pad2 == b"" and hdr != b"":
        # one entry in param matrix is correct: non-empty name, not padded
        expectation = nullcontext()
    with expectation:
        parser.feed_data(text)


def test_empty_header_name(parser: Any) -> None:
    text = b"GET /test HTTP/1.1\r\n" b":test\r\n\r\n"
    with pytest.raises(http_exceptions.BadHttpMessage):
        parser.feed_data(text)


def test_invalid_header(parser: Any) -> None:
    text = b"GET /test HTTP/1.1\r\n" b"test line\r\n\r\n"
    with pytest.raises(http_exceptions.BadHttpMessage):
        parser.feed_data(text)


def test_invalid_name(parser: Any) -> None:
    text = b"GET /test HTTP/1.1\r\n" b"test[]: line\r\n\r\n"

    with pytest.raises(http_exceptions.BadHttpMessage):
        parser.feed_data(text)


@pytest.mark.parametrize("size", [40960, 8191])
def test_max_header_field_size(parser: Any, size: Any) -> None:
    name = b"t" * size
    text = b"GET /test HTTP/1.1\r\n" + name + b":data\r\n\r\n"

    match = f"400, message:\n  Got more than 8190 bytes \\({size}\\) when reading"
    with pytest.raises(http_exceptions.LineTooLong, match=match):
        parser.feed_data(text)


def test_max_header_field_size_under_limit(parser: Any) -> None:
    name = b"t" * 8190
    text = b"GET /test HTTP/1.1\r\n" + name + b":data\r\n\r\n"

    messages, upgrade, tail = parser.feed_data(text)
    msg = messages[0][0]
    assert msg.method == "GET"
    assert msg.path == "/test"
    assert msg.version == (1, 1)
    assert msg.headers == CIMultiDict({name.decode(): "data"})
    assert msg.raw_headers == ((name, b"data"),)
    assert not msg.should_close
    assert msg.compression is None
    assert not msg.upgrade
    assert not msg.chunked
    assert msg.url == URL("/test")


@pytest.mark.parametrize("size", [40960, 8191])
def test_max_header_value_size(parser: Any, size: Any) -> None:
    name = b"t" * size
    text = b"GET /test HTTP/1.1\r\n" b"data:" + name + b"\r\n\r\n"

    match = f"400, message:\n  Got more than 8190 bytes \\({size}\\) when reading"
    with pytest.raises(http_exceptions.LineTooLong, match=match):
        parser.feed_data(text)


def test_max_header_value_size_under_limit(parser: Any) -> None:
    value = b"A" * 8190
    text = b"GET /test HTTP/1.1\r\n" b"data:" + value + b"\r\n\r\n"

    messages, upgrade, tail = parser.feed_data(text)
    msg = messages[0][0]
    assert msg.method == "GET"
    assert msg.path == "/test"
    assert msg.version == (1, 1)
    assert msg.headers == CIMultiDict({"data": value.decode()})
    assert msg.raw_headers == ((b"data", value),)
    assert not msg.should_close
    assert msg.compression is None
    assert not msg.upgrade
    assert not msg.chunked
    assert msg.url == URL("/test")


@pytest.mark.parametrize("size", [40965, 8191])
def test_max_header_value_size_continuation(parser: Any, size: Any) -> None:
    name = b"T" * (size - 5)
    text = b"GET /test HTTP/1.1\r\n" b"data: test\r\n " + name + b"\r\n\r\n"

    match = f"400, message:\n  Got more than 8190 bytes \\({size}\\) when reading"
    with pytest.raises(http_exceptions.LineTooLong, match=match):
        parser.feed_data(text)


def test_max_header_value_size_continuation_under_limit(parser: Any) -> None:
    value = b"A" * 8185
    text = b"GET /test HTTP/1.1\r\n" b"data: test\r\n " + value + b"\r\n\r\n"

    messages, upgrade, tail = parser.feed_data(text)
    msg = messages[0][0]
    assert msg.method == "GET"
    assert msg.path == "/test"
    assert msg.version == (1, 1)
    assert msg.headers == CIMultiDict({"data": "test " + value.decode()})
    assert msg.raw_headers == ((b"data", b"test " + value),)
    assert not msg.should_close
    assert msg.compression is None
    assert not msg.upgrade
    assert not msg.chunked
    assert msg.url == URL("/test")


def test_http_request_parser(parser: Any) -> None:
    text = b"GET /path HTTP/1.1\r\n\r\n"
    messages, upgrade, tail = parser.feed_data(text)
    msg = messages[0][0]

    assert msg.method == "GET"
    assert msg.path == "/path"
    assert msg.version == (1, 1)
    assert msg.headers == CIMultiDict()
    assert msg.raw_headers == ()
    assert not msg.should_close
    assert msg.compression is None
    assert not msg.upgrade
    assert not msg.chunked
    assert msg.url == URL("/path")


def test_http_request_bad_status_line(parser: Any) -> None:
    text = b"getpath \r\n\r\n"
    with pytest.raises(http_exceptions.BadStatusLine) as exc_info:
        parser.feed_data(text)
    # Check for accidentally escaped message.
    assert r"\n" not in exc_info.value.message


_num: Dict[bytes, str] = {
    # dangerous: accepted by Python int()
    # unicodedata.category("\U0001D7D9") == 'Nd'
    "\N{mathematical double-struck digit one}".encode(): "utf8digit",
    # only added for interop tests, refused by Python int()
    # unicodedata.category("\U000000B9") == 'No'
    "\N{superscript one}".encode(): "utf8number",
    "\N{superscript one}".encode("latin-1"): "latin1number",
}


@pytest.mark.parametrize("nonascii_digit", _num.keys(), ids=_num.values())
def test_http_request_bad_status_line_number(
    parser: Any, nonascii_digit: bytes
) -> None:
    text = b"GET /digit HTTP/1." + nonascii_digit + b"\r\n\r\n"
    with pytest.raises(http_exceptions.BadStatusLine):
        parser.feed_data(text)


def test_http_request_bad_status_line_separator(parser: Any) -> None:
    # single code point, old, multibyte NFKC, multibyte NFKD
    utf8sep = "\N{arabic ligature sallallahou alayhe wasallam}".encode()
    text = b"GET /ligature HTTP/1" + utf8sep + b"1\r\n\r\n"
    with pytest.raises(http_exceptions.BadStatusLine):
        parser.feed_data(text)


def test_http_request_bad_status_line_whitespace(parser: Any) -> None:
    text = b"GET\n/path\fHTTP/1.1\r\n\r\n"
    with pytest.raises(http_exceptions.BadStatusLine):
        parser.feed_data(text)


def test_http_request_upgrade(parser: Any) -> None:
    text = (
        b"GET /test HTTP/1.1\r\n"
        b"connection: upgrade\r\n"
        b"upgrade: websocket\r\n\r\n"
        b"some raw data"
    )
    messages, upgrade, tail = parser.feed_data(text)
    msg = messages[0][0]
    assert not msg.should_close
    assert msg.upgrade
    assert upgrade
    assert tail == b"some raw data"


@pytest.fixture
def xfail_c_parser_url(request) -> None:
    if isinstance(request.getfixturevalue("parser"), HttpRequestParserPy):
        return
    request.node.add_marker(
        pytest.mark.xfail(
            reason="Regression test for Py parser. May match C behaviour later.",
            raises=http_exceptions.InvalidURLError,
        )
    )


@pytest.mark.usefixtures("xfail_c_parser_url")
def test_http_request_parser_utf8_request_line(parser: Any) -> None:
    messages, upgrade, tail = parser.feed_data(
        # note the truncated unicode sequence
        b"GET /P\xc3\xbcnktchen\xa0\xef\xb7 HTTP/1.1\r\n" +
        # for easier grep: ASCII 0xA0 more commonly known as non-breaking space
        # note the leading and trailing spaces
        "sTeP:  \N{latin small letter sharp s}nek\t\N{no-break space}  "
        "\r\n\r\n".encode()
    )
    msg = messages[0][0]

    assert msg.method == "GET"
    assert msg.path == "/Pünktchen\udca0\udcef\udcb7"
    assert msg.version == (1, 1)
    assert msg.headers == CIMultiDict([("STEP", "ßnek\t\xa0")])
    assert msg.raw_headers == ((b"sTeP", "ßnek\t\xa0".encode()),)
    assert not msg.should_close
    assert msg.compression is None
    assert not msg.upgrade
    assert not msg.chunked
    # python HTTP parser depends on Cython and CPython URL to match
    # .. but yarl.URL("/abs") is not equal to URL.build(path="/abs"), see #6409
    assert msg.url == URL.build(path="/Pünktchen\udca0\udcef\udcb7", encoded=True)


def test_http_request_parser_utf8(parser: Any) -> None:
    text = "GET /path HTTP/1.1\r\nx-test:тест\r\n\r\n".encode()
    messages, upgrade, tail = parser.feed_data(text)
    msg = messages[0][0]

    assert msg.method == "GET"
    assert msg.path == "/path"
    assert msg.version == (1, 1)
    assert msg.headers == CIMultiDict([("X-TEST", "тест")])
    assert msg.raw_headers == ((b"x-test", "тест".encode()),)
    assert not msg.should_close
    assert msg.compression is None
    assert not msg.upgrade
    assert not msg.chunked
    assert msg.url == URL("/path")


def test_http_request_parser_non_utf8(parser: Any) -> None:
    text = "GET /path HTTP/1.1\r\nx-test:тест\r\n\r\n".encode("cp1251")
    msg = parser.feed_data(text)[0][0][0]

    assert msg.method == "GET"
    assert msg.path == "/path"
    assert msg.version == (1, 1)
    assert msg.headers == CIMultiDict(
        [("X-TEST", "тест".encode("cp1251").decode("utf8", "surrogateescape"))]
    )
    assert msg.raw_headers == ((b"x-test", "тест".encode("cp1251")),)
    assert not msg.should_close
    assert msg.compression is None
    assert not msg.upgrade
    assert not msg.chunked
    assert msg.url == URL("/path")


def test_http_request_parser_two_slashes(parser: Any) -> None:
    text = b"GET //path HTTP/1.1\r\n\r\n"
    msg = parser.feed_data(text)[0][0][0]

    assert msg.method == "GET"
    assert msg.path == "//path"
    assert msg.url.path == "//path"
    assert msg.version == (1, 1)
    assert not msg.should_close
    assert msg.compression is None
    assert not msg.upgrade
    assert not msg.chunked


@pytest.mark.parametrize(
    "rfc9110_5_6_2_token_delim",
    [bytes([i]) for i in rb'"(),/:;<=>?@[\]{}'],
)
def test_http_request_parser_bad_method(
    parser: Any, rfc9110_5_6_2_token_delim: bytes
) -> None:
    with pytest.raises(http_exceptions.BadStatusLine):
        parser.feed_data(rfc9110_5_6_2_token_delim + b'ET" /get HTTP/1.1\r\n\r\n')


def test_http_request_parser_bad_version(parser: Any) -> None:
    with pytest.raises(http_exceptions.BadHttpMessage):
        parser.feed_data(b"GET //get HT/11\r\n\r\n")


def test_http_request_parser_bad_version_number(parser: Any) -> None:
    with pytest.raises(http_exceptions.BadHttpMessage):
        parser.feed_data(b"GET /test HTTP/1.32\r\n\r\n")


def test_http_request_parser_bad_ascii_uri(parser: Any) -> None:
    with pytest.raises(http_exceptions.InvalidURLError):
        parser.feed_data(b"GET ! HTTP/1.1\r\n\r\n")


def test_http_request_parser_bad_nonascii_uri(parser: Any) -> None:
    with pytest.raises(http_exceptions.InvalidURLError):
        parser.feed_data(b"GET \xff HTTP/1.1\r\n\r\n")


@pytest.mark.parametrize("size", [40965, 8191])
def test_http_request_max_status_line(parser: Any, size: Any) -> None:
    path = b"t" * (size - 5)
    match = f"400, message:\n  Got more than 8190 bytes \\({size}\\) when reading"
    with pytest.raises(http_exceptions.LineTooLong, match=match):
        parser.feed_data(b"GET /path" + path + b" HTTP/1.1\r\n\r\n")


def test_http_request_max_status_line_under_limit(parser: Any) -> None:
    path = b"t" * (8190 - 5)
    messages, upgraded, tail = parser.feed_data(
        b"GET /path" + path + b" HTTP/1.1\r\n\r\n"
    )
    msg = messages[0][0]

    assert msg.method == "GET"
    assert msg.path == "/path" + path.decode()
    assert msg.version == (1, 1)
    assert msg.headers == CIMultiDict()
    assert msg.raw_headers == ()
    assert not msg.should_close
    assert msg.compression is None
    assert not msg.upgrade
    assert not msg.chunked
    assert msg.url == URL("/path" + path.decode())


def test_http_response_parser_utf8(response: Any) -> None:
    text = "HTTP/1.1 200 Ok\r\nx-test:тест\r\n\r\n".encode()

    messages, upgraded, tail = response.feed_data(text)
    assert len(messages) == 1
    msg = messages[0][0]

    assert msg.version == (1, 1)
    assert msg.code == 200
    assert msg.reason == "Ok"
    assert msg.headers == CIMultiDict([("X-TEST", "тест")])
    assert msg.raw_headers == ((b"x-test", "тест".encode()),)
    assert not upgraded
    assert not tail


def test_http_response_parser_utf8_without_reason(response: Any) -> None:
    text = "HTTP/1.1 200 \r\nx-test:тест\r\n\r\n".encode()

    messages, upgraded, tail = response.feed_data(text)
    assert len(messages) == 1
    msg = messages[0][0]

    assert msg.version == (1, 1)
    assert msg.code == 200
    assert msg.reason == ""
    assert msg.headers == CIMultiDict([("X-TEST", "тест")])
    assert msg.raw_headers == ((b"x-test", "тест".encode()),)
    assert not upgraded
    assert not tail


@pytest.mark.parametrize("size", [40962, 8191])
def test_http_response_parser_bad_status_line_too_long(
    response: Any, size: Any
) -> None:
    reason = b"t" * (size - 2)
    match = f"400, message:\n  Got more than 8190 bytes \\({size}\\) when reading"
    with pytest.raises(http_exceptions.LineTooLong, match=match):
        response.feed_data(b"HTTP/1.1 200 Ok" + reason + b"\r\n\r\n")


def test_http_response_parser_status_line_under_limit(response: Any) -> None:
    reason = b"O" * 8190
    messages, upgraded, tail = response.feed_data(
        b"HTTP/1.1 200 " + reason + b"\r\n\r\n"
    )
    msg = messages[0][0]
    assert msg.version == (1, 1)
    assert msg.code == 200
    assert msg.reason == reason.decode()


def test_http_response_parser_bad_version(response: Any) -> None:
    with pytest.raises(http_exceptions.BadHttpMessage):
        response.feed_data(b"HT/11 200 Ok\r\n\r\n")


def test_http_response_parser_bad_version_number(response: Any) -> None:
    with pytest.raises(http_exceptions.BadHttpMessage):
        response.feed_data(b"HTTP/12.3 200 Ok\r\n\r\n")


def test_http_response_parser_no_reason(response: Any) -> None:
    msg = response.feed_data(b"HTTP/1.1 200\r\n\r\n")[0][0][0]

    assert msg.version == (1, 1)
    assert msg.code == 200
    assert msg.reason == ""


def test_http_response_parser_lenient_headers(response: Any) -> None:
    messages, upgrade, tail = response.feed_data(
        b"HTTP/1.1 200 test\r\nFoo: abc\x01def\r\n\r\n"
    )
    msg = messages[0][0]

    assert msg.headers["Foo"] == "abc\x01def"


@pytest.mark.dev_mode
def test_http_response_parser_strict_headers(response: Any) -> None:
    if isinstance(response, HttpResponseParserPy):
        pytest.xfail("Py parser is lenient. May update py-parser later.")
    with pytest.raises(http_exceptions.BadHttpMessage):
        response.feed_data(b"HTTP/1.1 200 test\r\nFoo: abc\x01def\r\n\r\n")


def test_http_response_parser_bad_crlf(response: Any) -> None:
    """Still a lot of dodgy servers sending bad requests like this."""
    messages, upgrade, tail = response.feed_data(
        b"HTTP/1.0 200 OK\nFoo: abc\nBar: def\n\nBODY\n"
    )
    msg = messages[0][0]

    assert msg.headers["Foo"] == "abc"
    assert msg.headers["Bar"] == "def"


async def test_http_response_parser_bad_chunked_lax(response: Any) -> None:
    text = (
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5 \r\nabcde\r\n0\r\n\r\n"
    )
    messages, upgrade, tail = response.feed_data(text)

    assert await messages[0][1].read(5) == b"abcde"


@pytest.mark.dev_mode
async def test_http_response_parser_bad_chunked_strict_py(
    loop: Any, protocol: Any
) -> None:
    response = HttpResponseParserPy(
        protocol,
        loop,
        2**16,
        max_line_size=8190,
        max_field_size=8190,
    )
    text = (
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5 \r\nabcde\r\n0\r\n\r\n"
    )
    messages, upgrade, tail = response.feed_data(text)
    assert isinstance(messages[0][1].exception(), http_exceptions.TransferEncodingError)


@pytest.mark.dev_mode
@pytest.mark.skipif(
    "HttpRequestParserC" not in dir(aiohttp.http_parser),
    reason="C based HTTP parser not available",
)
async def test_http_response_parser_bad_chunked_strict_c(
    loop: Any, protocol: Any
) -> None:
    response = HttpResponseParserC(
        protocol,
        loop,
        2**16,
        max_line_size=8190,
        max_field_size=8190,
    )
    text = (
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5 \r\nabcde\r\n0\r\n\r\n"
    )
    with pytest.raises(http_exceptions.BadHttpMessage):
        response.feed_data(text)


def test_http_response_parser_bad(response: Any) -> None:
    with pytest.raises(http_exceptions.BadHttpMessage):
        response.feed_data(b"HTT/1\r\n\r\n")


def test_http_response_parser_code_under_100(response: Any) -> None:
    with pytest.raises(http_exceptions.BadStatusLine):
        response.feed_data(b"HTTP/1.1 99 test\r\n\r\n")


def test_http_response_parser_code_above_999(response: Any) -> None:
    with pytest.raises(http_exceptions.BadStatusLine):
        response.feed_data(b"HTTP/1.1 9999 test\r\n\r\n")


def test_http_response_parser_code_not_int(response: Any) -> None:
    with pytest.raises(http_exceptions.BadStatusLine):
        response.feed_data(b"HTTP/1.1 ttt test\r\n\r\n")


@pytest.mark.parametrize("nonascii_digit", _num.keys(), ids=_num.values())
def test_http_response_parser_code_not_ascii(
    response: Any, nonascii_digit: bytes
) -> None:
    with pytest.raises(http_exceptions.BadStatusLine):
        response.feed_data(b"HTTP/1.1 20" + nonascii_digit + b" test\r\n\r\n")


def test_http_request_chunked_payload(parser: Any) -> None:
    text = b"GET /test HTTP/1.1\r\n" b"transfer-encoding: chunked\r\n\r\n"
    msg, payload = parser.feed_data(text)[0][0]

    assert msg.chunked
    assert not payload.is_eof()
    assert isinstance(payload, streams.StreamReader)

    parser.feed_data(b"4\r\ndata\r\n4\r\nline\r\n0\r\n\r\n")

    assert b"dataline" == b"".join(d for d in payload._buffer)
    assert [4, 8] == payload._http_chunk_splits
    assert payload.is_eof()


def test_http_request_chunked_payload_and_next_message(parser: Any) -> None:
    text = b"GET /test HTTP/1.1\r\n" b"transfer-encoding: chunked\r\n\r\n"
    msg, payload = parser.feed_data(text)[0][0]

    messages, upgraded, tail = parser.feed_data(
        b"4\r\ndata\r\n4\r\nline\r\n0\r\n\r\n"
        b"POST /test2 HTTP/1.1\r\n"
        b"transfer-encoding: chunked\r\n\r\n"
    )

    assert b"dataline" == b"".join(d for d in payload._buffer)
    assert [4, 8] == payload._http_chunk_splits
    assert payload.is_eof()

    assert len(messages) == 1
    msg2, payload2 = messages[0]

    assert msg2.method == "POST"
    assert msg2.chunked
    assert not payload2.is_eof()


def test_http_request_chunked_payload_chunks(parser: Any) -> None:
    text = b"GET /test HTTP/1.1\r\n" b"transfer-encoding: chunked\r\n\r\n"
    msg, payload = parser.feed_data(text)[0][0]

    parser.feed_data(b"4\r\ndata\r")
    parser.feed_data(b"\n4")
    parser.feed_data(b"\r")
    parser.feed_data(b"\n")
    parser.feed_data(b"li")
    parser.feed_data(b"ne\r\n0\r\n")
    parser.feed_data(b"test: test\r\n")

    assert b"dataline" == b"".join(d for d in payload._buffer)
    assert [4, 8] == payload._http_chunk_splits
    assert not payload.is_eof()

    parser.feed_data(b"\r\n")
    assert b"dataline" == b"".join(d for d in payload._buffer)
    assert [4, 8] == payload._http_chunk_splits
    assert payload.is_eof()


def test_parse_chunked_payload_chunk_extension(parser: Any) -> None:
    text = b"GET /test HTTP/1.1\r\n" b"transfer-encoding: chunked\r\n\r\n"
    msg, payload = parser.feed_data(text)[0][0]

    parser.feed_data(b"4;test\r\ndata\r\n4\r\nline\r\n0\r\ntest: test\r\n\r\n")

    assert b"dataline" == b"".join(d for d in payload._buffer)
    assert [4, 8] == payload._http_chunk_splits
    assert payload.is_eof()


def _test_parse_no_length_or_te_on_post(loop, protocol, request_cls):
    parser = request_cls(protocol, loop, readall=True)
    text = b"POST /test HTTP/1.1\r\n\r\n"
    msg, payload = parser.feed_data(text)[0][0]

    assert payload.is_eof()


def test_parse_payload_response_without_body(
    loop: Any, protocol: Any, response_cls: Any
) -> None:
    parser = response_cls(protocol, loop, 2**16, response_with_body=False)
    text = b"HTTP/1.1 200 Ok\r\n" b"content-length: 10\r\n\r\n"
    msg, payload = parser.feed_data(text)[0][0]

    assert payload.is_eof()


def test_parse_length_payload(response: Any) -> None:
    text = b"HTTP/1.1 200 Ok\r\n" b"content-length: 4\r\n\r\n"
    msg, payload = response.feed_data(text)[0][0]
    assert not payload.is_eof()

    response.feed_data(b"da")
    response.feed_data(b"t")
    response.feed_data(b"aHT")

    assert payload.is_eof()
    assert b"data" == b"".join(d for d in payload._buffer)


def test_parse_no_length_payload(parser: Any) -> None:
    text = b"PUT / HTTP/1.1\r\n\r\n"
    msg, payload = parser.feed_data(text)[0][0]
    assert payload.is_eof()


def test_parse_content_length_payload_multiple(response: Any) -> None:
    text = b"HTTP/1.1 200 OK\r\ncontent-length: 5\r\n\r\nfirst"
    msg, payload = response.feed_data(text)[0][0]
    assert msg.version == HttpVersion(major=1, minor=1)
    assert msg.code == 200
    assert msg.reason == "OK"
    assert msg.headers == CIMultiDict(
        [
            ("Content-Length", "5"),
        ]
    )
    assert msg.raw_headers == ((b"content-length", b"5"),)
    assert not msg.should_close
    assert msg.compression is None
    assert not msg.upgrade
    assert not msg.chunked
    assert payload.is_eof()
    assert b"first" == b"".join(d for d in payload._buffer)

    text = b"HTTP/1.1 200 OK\r\ncontent-length: 6\r\n\r\nsecond"
    msg, payload = response.feed_data(text)[0][0]
    assert msg.version == HttpVersion(major=1, minor=1)
    assert msg.code == 200
    assert msg.reason == "OK"
    assert msg.headers == CIMultiDict(
        [
            ("Content-Length", "6"),
        ]
    )
    assert msg.raw_headers == ((b"content-length", b"6"),)
    assert not msg.should_close
    assert msg.compression is None
    assert not msg.upgrade
    assert not msg.chunked
    assert payload.is_eof()
    assert b"second" == b"".join(d for d in payload._buffer)


def test_parse_content_length_than_chunked_payload(response: Any) -> None:
    text = b"HTTP/1.1 200 OK\r\ncontent-length: 5\r\n\r\nfirst"
    msg, payload = response.feed_data(text)[0][0]
    assert msg.version == HttpVersion(major=1, minor=1)
    assert msg.code == 200
    assert msg.reason == "OK"
    assert msg.headers == CIMultiDict(
        [
            ("Content-Length", "5"),
        ]
    )
    assert msg.raw_headers == ((b"content-length", b"5"),)
    assert not msg.should_close
    assert msg.compression is None
    assert not msg.upgrade
    assert not msg.chunked
    assert payload.is_eof()
    assert b"first" == b"".join(d for d in payload._buffer)

    text = (
        b"HTTP/1.1 200 OK\r\n"
        b"transfer-encoding: chunked\r\n\r\n"
        b"6\r\nsecond\r\n0\r\n\r\n"
    )
    msg, payload = response.feed_data(text)[0][0]
    assert msg.version == HttpVersion(major=1, minor=1)
    assert msg.code == 200
    assert msg.reason == "OK"
    assert msg.headers == CIMultiDict(
        [
            ("Transfer-Encoding", "chunked"),
        ]
    )
    assert msg.raw_headers == ((b"transfer-encoding", b"chunked"),)
    assert not msg.should_close
    assert msg.compression is None
    assert not msg.upgrade
    assert msg.chunked
    assert payload.is_eof()
    assert b"second" == b"".join(d for d in payload._buffer)


@pytest.mark.parametrize("code", (204, 304, 101, 102))
def test_parse_chunked_payload_empty_body_than_another_chunked(
    response: Any, code: int
) -> None:
    head = f"HTTP/1.1 {code} OK\r\n".encode()
    text = head + b"transfer-encoding: chunked\r\n\r\n"
    msg, payload = response.feed_data(text)[0][0]
    assert msg.version == HttpVersion(major=1, minor=1)
    assert msg.code == code
    assert msg.reason == "OK"
    assert msg.headers == CIMultiDict(
        [
            ("Transfer-Encoding", "chunked"),
        ]
    )
    assert msg.raw_headers == ((b"transfer-encoding", b"chunked"),)
    assert not msg.should_close
    assert msg.compression is None
    assert not msg.upgrade
    assert msg.chunked
    assert payload.is_eof()

    text = (
        b"HTTP/1.1 200 OK\r\n"
        b"transfer-encoding: chunked\r\n\r\n"
        b"6\r\nsecond\r\n0\r\n\r\n"
    )
    msg, payload = response.feed_data(text)[0][0]
    assert msg.version == HttpVersion(major=1, minor=1)
    assert msg.code == 200
    assert msg.reason == "OK"
    assert msg.headers == CIMultiDict(
        [
            ("Transfer-Encoding", "chunked"),
        ]
    )
    assert msg.raw_headers == ((b"transfer-encoding", b"chunked"),)
    assert not msg.should_close
    assert msg.compression is None
    assert not msg.upgrade
    assert msg.chunked
    assert payload.is_eof()
    assert b"second" == b"".join(d for d in payload._buffer)


def test_partial_url(parser: Any) -> None:
    messages, upgrade, tail = parser.feed_data(b"GET /te")
    assert len(messages) == 0
    messages, upgrade, tail = parser.feed_data(b"st HTTP/1.1\r\n\r\n")
    assert len(messages) == 1

    msg, payload = messages[0]

    assert msg.method == "GET"
    assert msg.path == "/test"
    assert msg.version == (1, 1)
    assert payload.is_eof()


@pytest.mark.parametrize(
    ("uri", "path", "query", "fragment"),
    [
        ("/path%23frag", "/path#frag", {}, ""),
        ("/path%2523frag", "/path%23frag", {}, ""),
        ("/path?key=value%23frag", "/path", {"key": "value#frag"}, ""),
        ("/path?key=value%2523frag", "/path", {"key": "value%23frag"}, ""),
        ("/path#frag%20", "/path", {}, "frag "),
        ("/path#frag%2520", "/path", {}, "frag%20"),
    ],
)
def test_parse_uri_percent_encoded(
    parser: Any, uri: Any, path: Any, query: Any, fragment: Any
) -> None:
    text = (f"GET {uri} HTTP/1.1\r\n\r\n").encode()
    messages, upgrade, tail = parser.feed_data(text)
    msg = messages[0][0]

    assert msg.path == uri
    assert msg.url == URL(uri)
    assert msg.url.path == path
    assert msg.url.query == query
    assert msg.url.fragment == fragment


def test_parse_uri_utf8(parser: Any) -> None:
    if not isinstance(parser, HttpRequestParserPy):
        pytest.xfail("Not valid HTTP. Maybe update py-parser to reject later.")
    text = ("GET /путь?ключ=знач#фраг HTTP/1.1\r\n\r\n").encode()
    messages, upgrade, tail = parser.feed_data(text)
    msg = messages[0][0]

    assert msg.path == "/путь?ключ=знач#фраг"
    assert msg.url.path == "/путь"
    assert msg.url.query == {"ключ": "знач"}
    assert msg.url.fragment == "фраг"


def test_parse_uri_utf8_percent_encoded(parser: Any) -> None:
    text = (
        "GET %s HTTP/1.1\r\n\r\n" % quote("/путь?ключ=знач#фраг", safe="/?=#")
    ).encode()
    messages, upgrade, tail = parser.feed_data(text)
    msg = messages[0][0]

    assert msg.path == quote("/путь?ключ=знач#фраг", safe="/?=#")
    assert msg.url == URL("/путь?ключ=знач#фраг")
    assert msg.url.path == "/путь"
    assert msg.url.query == {"ключ": "знач"}
    assert msg.url.fragment == "фраг"


@pytest.mark.skipif(
    "HttpRequestParserC" not in dir(aiohttp.http_parser),
    reason="C based HTTP parser not available",
)
def test_parse_bad_method_for_c_parser_raises(loop: Any, protocol: Any) -> None:
    payload = b"GET1 /test HTTP/1.1\r\n\r\n"
    parser = HttpRequestParserC(
        protocol,
        loop,
        2**16,
        max_line_size=8190,
        max_field_size=8190,
    )

    with pytest.raises(aiohttp.http_exceptions.BadStatusLine):
        messages, upgrade, tail = parser.feed_data(payload)


class TestParsePayload:
    async def test_parse_eof_payload(self, stream: Any) -> None:
        out = aiohttp.FlowControlDataQueue(stream, 2**16, loop=asyncio.get_event_loop())
        p = HttpPayloadParser(out, readall=True)
        p.feed_data(b"data")
        p.feed_eof()

        assert out.is_eof()
        assert [(bytearray(b"data"), 4)] == list(out._buffer)

    async def test_parse_no_body(self, stream: Any) -> None:
        out = aiohttp.FlowControlDataQueue(stream, 2**16, loop=asyncio.get_event_loop())
        p = HttpPayloadParser(out, method="PUT")

        assert out.is_eof()
        assert p.done

    async def test_parse_length_payload_eof(self, stream: Any) -> None:
        out = aiohttp.FlowControlDataQueue(stream, 2**16, loop=asyncio.get_event_loop())

        p = HttpPayloadParser(out, length=4)
        p.feed_data(b"da")

        with pytest.raises(http_exceptions.ContentLengthError):
            p.feed_eof()

    async def test_parse_chunked_payload_size_error(self, stream: Any) -> None:
        out = aiohttp.FlowControlDataQueue(stream, 2**16, loop=asyncio.get_event_loop())
        p = HttpPayloadParser(out, chunked=True)
        with pytest.raises(http_exceptions.TransferEncodingError):
            p.feed_data(b"blah\r\n")
        assert isinstance(out.exception(), http_exceptions.TransferEncodingError)

    async def test_parse_chunked_payload_split_end(self, protocol: Any) -> None:
        out = aiohttp.StreamReader(protocol, 2**16, loop=None)
        p = HttpPayloadParser(out, chunked=True)
        p.feed_data(b"4\r\nasdf\r\n0\r\n")
        p.feed_data(b"\r\n")

        assert out.is_eof()
        assert b"asdf" == b"".join(out._buffer)

    async def test_parse_chunked_payload_split_end2(self, protocol: Any) -> None:
        out = aiohttp.StreamReader(protocol, 2**16, loop=None)
        p = HttpPayloadParser(out, chunked=True)
        p.feed_data(b"4\r\nasdf\r\n0\r\n\r")
        p.feed_data(b"\n")

        assert out.is_eof()
        assert b"asdf" == b"".join(out._buffer)

    async def test_parse_chunked_payload_split_end_trailers(
        self, protocol: Any
    ) -> None:
        out = aiohttp.StreamReader(protocol, 2**16, loop=None)
        p = HttpPayloadParser(out, chunked=True)
        p.feed_data(b"4\r\nasdf\r\n0\r\n")
        p.feed_data(b"Content-MD5: 912ec803b2ce49e4a541068d495ab570\r\n")
        p.feed_data(b"\r\n")

        assert out.is_eof()
        assert b"asdf" == b"".join(out._buffer)

    async def test_parse_chunked_payload_split_end_trailers2(
        self, protocol: Any
    ) -> None:
        out = aiohttp.StreamReader(protocol, 2**16, loop=None)
        p = HttpPayloadParser(out, chunked=True)
        p.feed_data(b"4\r\nasdf\r\n0\r\n")
        p.feed_data(b"Content-MD5: 912ec803b2ce49e4a541068d495ab570\r\n\r")
        p.feed_data(b"\n")

        assert out.is_eof()
        assert b"asdf" == b"".join(out._buffer)

    async def test_parse_chunked_payload_split_end_trailers3(
        self, protocol: Any
    ) -> None:
        out = aiohttp.StreamReader(protocol, 2**16, loop=None)
        p = HttpPayloadParser(out, chunked=True)
        p.feed_data(b"4\r\nasdf\r\n0\r\nContent-MD5: ")
        p.feed_data(b"912ec803b2ce49e4a541068d495ab570\r\n\r\n")

        assert out.is_eof()
        assert b"asdf" == b"".join(out._buffer)

    async def test_parse_chunked_payload_split_end_trailers4(
        self, protocol: Any
    ) -> None:
        out = aiohttp.StreamReader(protocol, 2**16, loop=None)
        p = HttpPayloadParser(out, chunked=True)
        p.feed_data(b"4\r\nasdf\r\n0\r\n" b"C")
        p.feed_data(b"ontent-MD5: 912ec803b2ce49e4a541068d495ab570\r\n\r\n")

        assert out.is_eof()
        assert b"asdf" == b"".join(out._buffer)

    async def test_http_payload_parser_length(self, stream: Any) -> None:
        out = aiohttp.FlowControlDataQueue(stream, 2**16, loop=asyncio.get_event_loop())
        p = HttpPayloadParser(out, length=2)
        eof, tail = p.feed_data(b"1245")
        assert eof

        assert b"12" == b"".join(d for d, _ in out._buffer)
        assert b"45" == tail

    async def test_http_payload_parser_deflate(self, stream: Any) -> None:
        # c=compressobj(wbits=15); b''.join([c.compress(b'data'), c.flush()])
        COMPRESSED = b"x\x9cKI,I\x04\x00\x04\x00\x01\x9b"

        length = len(COMPRESSED)
        out = aiohttp.FlowControlDataQueue(stream, 2**16, loop=asyncio.get_event_loop())
        p = HttpPayloadParser(out, length=length, compression="deflate")
        p.feed_data(COMPRESSED)
        assert b"data" == b"".join(d for d, _ in out._buffer)
        assert out.is_eof()

    async def test_http_payload_parser_deflate_no_hdrs(self, stream: Any) -> None:
        """Tests incorrectly formed data (no zlib headers)."""
        # c=compressobj(wbits=-15); b''.join([c.compress(b'data'), c.flush()])
        COMPRESSED = b"KI,I\x04\x00"

        length = len(COMPRESSED)
        out = aiohttp.FlowControlDataQueue(stream, 2**16, loop=asyncio.get_event_loop())
        p = HttpPayloadParser(out, length=length, compression="deflate")
        p.feed_data(COMPRESSED)
        assert b"data" == b"".join(d for d, _ in out._buffer)
        assert out.is_eof()

    async def test_http_payload_parser_deflate_light(self, stream: Any) -> None:
        # c=compressobj(wbits=9); b''.join([c.compress(b'data'), c.flush()])
        COMPRESSED = b"\x18\x95KI,I\x04\x00\x04\x00\x01\x9b"

        length = len(COMPRESSED)
        out = aiohttp.FlowControlDataQueue(stream, 2**16, loop=asyncio.get_event_loop())
        p = HttpPayloadParser(out, length=length, compression="deflate")
        p.feed_data(COMPRESSED)
        assert b"data" == b"".join(d for d, _ in out._buffer)
        assert out.is_eof()

    async def test_http_payload_parser_deflate_split(self, stream: Any) -> None:
        out = aiohttp.FlowControlDataQueue(stream, 2**16, loop=asyncio.get_event_loop())
        p = HttpPayloadParser(out, compression="deflate", readall=True)
        # Feeding one correct byte should be enough to choose exact
        # deflate decompressor
        p.feed_data(b"x", 1)
        p.feed_data(b"\x9cKI,I\x04\x00\x04\x00\x01\x9b", 11)
        p.feed_eof()
        assert b"data" == b"".join(d for d, _ in out._buffer)

    async def test_http_payload_parser_deflate_split_err(self, stream: Any) -> None:
        out = aiohttp.FlowControlDataQueue(stream, 2**16, loop=asyncio.get_event_loop())
        p = HttpPayloadParser(out, compression="deflate", readall=True)
        # Feeding one wrong byte should be enough to choose exact
        # deflate decompressor
        p.feed_data(b"K", 1)
        p.feed_data(b"I,I\x04\x00", 5)
        p.feed_eof()
        assert b"data" == b"".join(d for d, _ in out._buffer)

    async def test_http_payload_parser_length_zero(self, stream: Any) -> None:
        out = aiohttp.FlowControlDataQueue(stream, 2**16, loop=asyncio.get_event_loop())
        p = HttpPayloadParser(out, length=0)
        assert p.done
        assert out.is_eof()

    @pytest.mark.skipif(brotli is None, reason="brotli is not installed")
    async def test_http_payload_brotli(self, stream: Any) -> None:
        compressed = brotli.compress(b"brotli data")
        out = aiohttp.FlowControlDataQueue(stream, 2**16, loop=asyncio.get_event_loop())
        p = HttpPayloadParser(out, length=len(compressed), compression="br")
        p.feed_data(compressed)
        assert b"brotli data" == b"".join(d for d, _ in out._buffer)
        assert out.is_eof()


class TestDeflateBuffer:
    async def test_feed_data(self, stream: Any) -> None:
        buf = aiohttp.FlowControlDataQueue(stream, 2**16, loop=asyncio.get_event_loop())
        dbuf = DeflateBuffer(buf, "deflate")

        dbuf.decompressor = mock.Mock()
        dbuf.decompressor.decompress_sync.return_value = b"line"

        # First byte should be b'x' in order code not to change the decoder.
        dbuf.feed_data(b"xxxx", 4)
        assert [b"line"] == list(d for d, _ in buf._buffer)

    async def test_feed_data_err(self, stream: Any) -> None:
        buf = aiohttp.FlowControlDataQueue(stream, 2**16, loop=asyncio.get_event_loop())
        dbuf = DeflateBuffer(buf, "deflate")

        exc = ValueError()
        dbuf.decompressor = mock.Mock()
        dbuf.decompressor.decompress_sync.side_effect = exc

        with pytest.raises(http_exceptions.ContentEncodingError):
            # Should be more than 4 bytes to trigger deflate FSM error.
            # Should start with b'x', otherwise code switch mocked decoder.
            dbuf.feed_data(b"xsomedata", 9)

    async def test_feed_eof(self, stream: Any) -> None:
        buf = aiohttp.FlowControlDataQueue(stream, 2**16, loop=asyncio.get_event_loop())
        dbuf = DeflateBuffer(buf, "deflate")

        dbuf.decompressor = mock.Mock()
        dbuf.decompressor.flush.return_value = b"line"

        dbuf.feed_eof()
        assert [b"line"] == list(d for d, _ in buf._buffer)
        assert buf._eof

    async def test_feed_eof_err_deflate(self, stream: Any) -> None:
        buf = aiohttp.FlowControlDataQueue(stream, 2**16, loop=asyncio.get_event_loop())
        dbuf = DeflateBuffer(buf, "deflate")

        dbuf.decompressor = mock.Mock()
        dbuf.decompressor.flush.return_value = b"line"
        dbuf.decompressor.eof = False

        with pytest.raises(http_exceptions.ContentEncodingError):
            dbuf.feed_eof()

    async def test_feed_eof_no_err_gzip(self, stream: Any) -> None:
        buf = aiohttp.FlowControlDataQueue(stream, 2**16, loop=asyncio.get_event_loop())
        dbuf = DeflateBuffer(buf, "gzip")

        dbuf.decompressor = mock.Mock()
        dbuf.decompressor.flush.return_value = b"line"
        dbuf.decompressor.eof = False

        dbuf.feed_eof()
        assert [b"line"] == list(d for d, _ in buf._buffer)

    async def test_feed_eof_no_err_brotli(self, stream: Any) -> None:
        buf = aiohttp.FlowControlDataQueue(stream, 2**16, loop=asyncio.get_event_loop())
        dbuf = DeflateBuffer(buf, "br")

        dbuf.decompressor = mock.Mock()
        dbuf.decompressor.flush.return_value = b"line"
        dbuf.decompressor.eof = False

        dbuf.feed_eof()
        assert [b"line"] == list(d for d, _ in buf._buffer)

    async def test_empty_body(self, stream: Any) -> None:
        buf = aiohttp.FlowControlDataQueue(stream, 2**16, loop=asyncio.get_event_loop())
        dbuf = DeflateBuffer(buf, "deflate")
        dbuf.feed_eof()

        assert buf.at_eof()
