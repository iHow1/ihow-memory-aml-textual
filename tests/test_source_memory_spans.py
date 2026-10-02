import json
import unittest
from experimental.source_memory.span_model import SpanAnnotator, source_spans


class SpanSelectionTests(unittest.TestCase):
    def test_verbatim_unicode_and_markdown(self):
        messages = [{'role':'assistant','content':'Advice:\n- **Keep** “qualifiers”.\n- 先问医生。','timestamp':None}]
        spans = source_spans(messages)
        model = SpanAnnotator(lambda request: json.dumps({'atoms':[{'span':'s1','kind':'recommendation','key':'advice'}]}))
        value = model.extract(messages, [])['atoms'][0]
        self.assertEqual(value['quote'], '- **Keep** “qualifiers”.')
        self.assertEqual(value['links'], [])
        for span in spans:
            self.assertEqual(messages[span['message']]['content'][span['start']:span['end']], span['text'])

    def test_repeated_lines_fall_back_to_whole_message(self):
        body = 'Yes\nYes\nNo'
        spans = source_spans([{'role':'user','content':body}])
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0]['text'], body)

    def test_reject_invented_ids_duplicates_and_model_quotes(self):
        messages = [{'role':'user','content':'24 blue marbles.'}]
        valid = {'span':'s0','kind':'fact','key':'marbles'}
        for atoms in ([{**valid,'span':'s999'}], [valid,valid], [{**valid,'quote':'27 marbles.'}]):
            with self.subTest(atoms=atoms), self.assertRaises(ValueError):
                SpanAnnotator(lambda request: json.dumps({'atoms':atoms})).extract(messages, [])


if __name__ == '__main__':
    unittest.main()
