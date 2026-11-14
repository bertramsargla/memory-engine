import unittest
from memory_engine.fusion import scores


class FusionTests(unittest.TestCase):
    def test_rank_fusion_is_invariant_to_monotonic_score_scale(self):
        a={'lexical':[{'chunk_id':1,'bm25':-100},{'chunk_id':2,'bm25':-1}],
           'dense':[{'chunk_id':2,'dense_score':.9},{'chunk_id':3,'dense_score':.8}]}
        expected=scores(a,'rrf');a['lexical'][0]['bm25']=-.001
        self.assertEqual(scores(a,'rrf'),expected)
        self.assertGreater(expected[2],expected[1])

    def test_normalization_is_independent_per_retriever(self):
        a={'lexical':[{'chunk_id':1,'bm25':-100},{'chunk_id':2,'bm25':-1}],
           'dense':[{'chunk_id':2,'dense_score':.9},{'chunk_id':3,'dense_score':.8}]}
        self.assertEqual(scores(a,'normalized'),{1:1,2:1,3:0})

    def test_duplicate_candidates_do_not_manufacture_agreement(self):
        a={'entity':[{'chunk_id':1},{'chunk_id':1}]}
        self.assertEqual(scores(a,'rrf'),{1:1/61})

    def test_exact_priority_is_not_a_comparable_numeric_score(self):
        self.assertEqual(scores({'exact':[{'chunk_id':9}]},'rrf'),{})

    def test_nonfinite_normalized_score_is_rejected(self):
        with self.assertRaises(ValueError):scores({'dense':[{'chunk_id':1,'dense_score':float('nan')}]},'normalized')
