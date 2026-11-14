from pathlib import Path
import unittest
from unittest.mock import patch
from memory_helpers import Memory,MemoryDirectory


class Ranker:
    space_id='synthetic-reranker'
    def score(self,q,text):return 100 if 'noise' in text else -1


class RerankingTests(unittest.TestCase):
    def setUp(self):
        self.temp=MemoryDirectory();self.addCleanup(self.temp.cleanup)
        self.m=Memory(Path(self.temp.name),test_key=b'B'*32)

    def test_reranker_cannot_demote_exact_identifier(self):
        target=self.m.ingest('CASE-7319 contract');self.m.ingest('contract noise')
        r=self.m.search('CASE-7319 contract',reranker=Ranker(),trace=True)
        self.assertEqual(r['results'][0]['id'],target['id']);self.assertEqual(r['reranking']['scored_count'],2)

    def test_revocation_during_reranking_wins(self):
        self.m.ingest('contract');grant=self.m.grant('test',['project:memory_engine']);m=self.m
        class Revoker(Ranker):
            def score(self,*args):m.revoke(grant['id']);return 1
        with self.assertRaises(PermissionError):self.m.search('contract',reranker=Revoker(),token=grant['token'])

    def test_deletion_during_reranking_does_not_resurrect(self):
        target=self.m.ingest('contract');m=self.m
        class Deleter(Ranker):
            def score(self,*args):m.delete_source(target['id']);return 1
        self.assertEqual(self.m.search('contract',reranker=Deleter())['results'],[])

    def test_nonfinite_output_falls_back_without_hiding_failure(self):
        self.m.ingest('contract')
        class Bad(Ranker):
            def score(self,*args):return float('nan')
        r=self.m.search('contract',reranker=Bad())
        self.assertEqual(r['reranking']['state'],'inference_failed');self.assertEqual(len(r['results']),1)

    def test_depth_is_bounded(self):
        for i in range(30):self.m.ingest('contract number '+str(i))
        r=self.m.search('contract',reranker=Ranker(),rerank_depth=20)
        self.assertEqual(r['reranking']['scored_count'],20)
        with self.assertRaises(ValueError):self.m.search('contract',reranker=Ranker(),rerank_depth=200)

    def test_dense_head_reaches_bounded_reranker_despite_lexical_agreement_noise(self):
        target=self.m.ingest('Ainult eesti keeles vajalik tõend')
        for i in range(25):self.m.ingest('contract noise number '+str(i))
        with self.m.transaction() as db:
            from memory_engine.retrieval import BASE
            dense=[dict(row) for row in db.execute(BASE+' ORDER BY c.id')]
        for i,row in enumerate(dense):row['dense_score']=1-i/100
        class Embedder:
            def embed(self,*args):return [1]
        class CorrectReader(Ranker):
            def score(self,q,text):return 100 if 'Ainult' in text else -1
        with patch('memory_engine.memory.vector_index.candidates',return_value=(dense,{'state':'ready'})):
            options=dict(strategy='hybrid_rrf',embedder=Embedder(),reranker=CorrectReader(),trace=True)
            lost=self.m.search('contract',protect_candidates=False,**options)
            kept=self.m.search('contract',**options)
        self.assertNotIn(target['id'],[r['id'] for r in lost['results']])
        self.assertEqual(kept['results'][0]['id'],target['id'])
        self.assertEqual(kept['reranking']['scored_count'],20)
        self.assertTrue(kept['trace']['candidate_pool']['decisions'])
