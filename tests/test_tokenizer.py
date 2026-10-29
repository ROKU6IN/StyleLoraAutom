import unittest
from stylelora.tagging.catalog import TagCatalog
from stylelora.tagging.tokenizer import tokenize_prompt, parse_original_prompt


class TokenizerTests(unittest.TestCase):
    def setUp(self):
        self.catalog=TagCatalog('test',{'blue hair':'blue hair','azure hair':'blue hair','name (series)':'name (series)'},{},True)

    def test_exact_offsets_aliases_disambiguation_unknown_and_dedup(self):
        raw=' Azure_Hair , blue hair, name (series), 未知描述'
        parsed=parse_original_prompt(raw,catalog=self.catalog)
        self.assertEqual([x.tag for x in parsed.tags],['blue hair','name (series)'])
        self.assertEqual(parsed.natural_language,'未知描述')
        self.assertFalse(parsed.unverifiable)
        for token in parsed.segments: self.assertEqual(raw[token.start:token.end],token.raw)
        self.assertEqual(parsed.segments[0].lookup,'alias')

    def test_no_weight_escape_or_control_evaluation(self):
        for text in ('(blue hair:1.2)','[blue hair]','blue hair AND other','BREAK',r'blue\,hair','(one,blue hair)', '(one,other,blue hair', 'blue hair,','_', '\0'):
            with self.subTest(text=text):
                parsed=parse_original_prompt(text,catalog=self.catalog)
                self.assertTrue(parsed.unverifiable)
        tokens=tokenize_prompt('(one,blue hair)',catalog=self.catalog)
        self.assertTrue(all(t.lookup=='unverifiable' for t in tokens))
        self.assertEqual(tokenize_prompt('(blue hair:1.2)',catalog=self.catalog)[0].weight,1.2)
        nested=tokenize_prompt('(' * 1000 + ','.join(['blue hair']*1000),catalog=self.catalog)
        self.assertTrue(all(t.lookup=='unverifiable' for t in nested))

    def test_no_catalog_is_unverifiable_not_unknown(self):
        self.assertEqual(tokenize_prompt('blue hair')[0].lookup,'unverifiable')
        self.assertEqual(tokenize_prompt('blue  hair',catalog=self.catalog)[0].lookup,'unknown')
