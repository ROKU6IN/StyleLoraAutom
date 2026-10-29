import unittest

from stylelora.exchange.errors import ExchangeError
from stylelora.exchange.sidecar import decode_sidecar, parse_sidecar, serialize_caption
from stylelora.exchange.types import ImportOptions
from stylelora.tagging.catalog import TagCatalog
from stylelora.tagging.plain import parse_tag_argument


class SidecarTests(unittest.TestCase):
    def parse(self, content, **kwargs):
        return parse_sidecar(content, options=ImportOptions(**kwargs))

    def test_original_spelling_order_dedup_and_crlf(self):
        result = self.parse("Blue_Hair, Smile, blue hair\r\n\r\n第一段\r\n第二段\r\n".encode())
        self.assertEqual([tag.tag for tag in result.originals[0].content], ["Blue_Hair", "Smile"])
        self.assertEqual(result.originals[1].content, "第一段\n第二段")

    def test_missing_and_empty_are_distinct(self):
        self.assertEqual(self.parse(None).originals, ())
        self.assertEqual(self.parse(b"").originals[0].content, ())
        self.assertEqual(self.parse(b"\n\nA paragraph.").originals[1].content, "A paragraph.")

    def test_utf8_bom_takes_precedence_over_gbk(self):
        value, selected = decode_sidecar(b"\xef\xbb\xbf" + "标签".encode(), "gbk")
        self.assertEqual((value, selected), ("标签", "utf-8-sig"))
        with self.assertRaisesRegex(ExchangeError, "encoding:invalid"):
            decode_sidecar(b"\xef\xbb\xbf\xff", "gbk")

    def test_explicit_gbk_and_strict_default(self):
        encoded = "蓝发, 微笑".encode("gbk")
        self.assertEqual(len(self.parse(encoded, encoding="gbk").originals[0].content), 2)
        with self.assertRaisesRegex(ExchangeError, "encoding:invalid"):
            self.parse(encoded)
        result = self.parse(encoded, encoding="auto")
        self.assertEqual(result.encoding, 'gbk')
        self.assertTrue(result.encoding_fallback)
        self.assertEqual(result.warnings, ('encoding:gbk_fallback',))

    def test_auto_order_bom_ascii_and_line_endings(self):
        text = '1girl, long_hair\n\n一名女孩站在树旁。'
        for codec, content, actual, fallback in (
            ('utf-8', text, 'utf-8', False),
            ('utf-8-sig', text, 'utf-8-sig', False),
            ('gbk', text, 'gbk', True),
            ('utf-8', text.replace('\n', '\r\n'), 'utf-8', False),
            ('gbk', text.replace('\n', '\r'), 'gbk', True),
        ):
            with self.subTest(codec=codec, content=content):
                result = self.parse(content.encode(codec), encoding='auto')
                self.assertEqual((result.encoding, result.encoding_fallback), (actual, fallback))
                self.assertEqual(result.originals[1].content, text.split('\n\n')[1])
                for original in result.originals:
                    self.assertEqual(original.params['encoding_requested'], 'auto')
                    self.assertEqual(original.params['encoding'], actual)
                    self.assertEqual(original.params['warnings'], list(result.warnings))
        for content in (b'', b'smile', b'smile\n\nASCII.'):
            result = self.parse(content, encoding='auto')
            self.assertEqual((result.encoding, result.encoding_fallback, result.warnings), ('utf-8', False, ()))

    def test_auto_rejects_invalid_bytes_bom_and_nul_without_replace(self):
        for content in (b'\xff', b'\xef\xbb\xbf\xff', b'\xef\xbb\xbf'+ '蓝发'.encode('gbk')):
            with self.subTest(content=content), self.assertRaisesRegex(ExchangeError, 'encoding:invalid'):
                self.parse(content, encoding='auto')
        for content in (b'smile\0', 'smile\n\n中文\0'.encode('gbk')):
            with self.assertRaisesRegex(ExchangeError, 'sidecar:nul_character'):
                self.parse(content, encoding='auto')
        with self.assertRaisesRegex(ExchangeError, 'encoding:unsupported'):
            self.parse(b'smile', encoding='latin1')

    def test_explicit_gbk_does_not_attempt_utf8_or_report_fallback(self):
        result = self.parse(b'smile', encoding='gbk')
        self.assertEqual((result.encoding, result.encoding_fallback, result.warnings), ('gbk', False, ()))
        value, encoding = decode_sidecar(b'\xc2\xa1', 'gbk')
        self.assertEqual(encoding, 'gbk')
        self.assertNotEqual(value, '\u00a1')

    def test_auto_catalog_preserves_fallback_and_unknown_evidence(self):
        catalog = TagCatalog('fixture', {'smile': 'smile'}, {'smile': 'general'})
        result = parse_sidecar('smile, 画面说明\n\n中文段落。'.encode('gbk'),
                               options=ImportOptions(encoding='auto', parser='catalog-comma-v1'), catalog=catalog)
        self.assertEqual(result.warnings, ('encoding:gbk_fallback', 'sidecar:unknown_to_nl'))
        self.assertEqual(result.originals[1].content, '画面说明\n中文段落。')
        self.assertEqual(result.originals[0].params['warnings'], list(result.warnings))
        self.assertEqual(self.parse(None, encoding='auto').encoding, None)

    def test_rejects_ambiguous_layout_and_prompt_syntax(self):
        for content in (b"smile\nA paragraph", b"smile,", b"{smile}", b"(smile:1.2)", b"BREAK", b"a\x00b", b"a\tb"):
            with self.subTest(content=content), self.assertRaises(ExchangeError):
                self.parse(content)
        self.assertEqual(parse_tag_argument("Character_(Series), smile"), ("character (series)", "smile"))

    def test_catalog_alias_unknown_requires_explicit_nl(self):
        catalog = TagCatalog("fixture", {"blue hair": "blue hair", "azure hair": "blue hair"}, {"blue hair": "general"})
        options = ImportOptions(parser="catalog-comma-v1")
        result = parse_sidecar(b"azure_hair, a painted scene\n\nDescription.", options=options, catalog=catalog)
        self.assertEqual(result.originals[0].content[0].tag, "blue hair")
        self.assertEqual(result.originals[1].content, "a painted scene\nDescription.")
        with self.assertRaisesRegex(ExchangeError, "ambiguous_unknown"):
            parse_sidecar(b"azure_hair, unknown", options=options, catalog=catalog)
        with self.assertRaisesRegex(ExchangeError, "catalog:required"):
            parse_sidecar(b"smile", options=options)

    def test_size_limit_and_roundtrip(self):
        with self.assertRaisesRegex(ExchangeError, "too_large"):
            self.parse(b"smile", max_sidecar_bytes=4)
        encoded = serialize_caption(("custom trigger", "blue hair"), "描述\r\n多行")
        self.assertFalse(encoded.startswith(b"\xef\xbb\xbf"))
        self.assertNotIn(b"\r", encoded)
        result = self.parse(encoded)
        self.assertEqual(result.originals[1].content, "描述\n多行")

    def test_serialize_rejects_empty_or_duplicate_tags(self):
        for tags in ((), ("smile", "Smile")):
            with self.assertRaises(ExchangeError):
                serialize_caption(tags)
