import tempfile
import threading
import concurrent.futures
from pathlib import Path
import unittest
import numpy as np
from memory_engine.memory import Memory
from memory_engine.vector_cache import VectorCache
from memory_engine import vector_index


class Model:
    space_id='cache-test-space';dimensions=3;contract={'recipe':'synthetic-cache-v1'}
    def embed(self,text,mode='passage'):
        v=np.array([sum(text.encode())%13+1,len(text)%7+1,3],dtype=np.float32)
        return v/np.linalg.norm(v)


class VectorCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='bb-cache-test-')
        self.memory=Memory(Path(self.tmp.name),test_key=b'C'*32)
        self.cache=VectorCache();self.model=Model();self.scope='project:memory_engine'
        self.events=[self.memory.ingest('item '+str(i)) for i in range(8)]
        self.memory.index_embeddings(self.model)
    def tearDown(self):
        self.cache.clear();self.memory.close();self.tmp.cleanup()
    def query(self):
        with self.memory.transaction() as db:
            return self.cache.candidates(db,self.model.embed('query'),self.model,self.scope)

    def test_matches_uncached_cosine_oracle_and_reuses_materialization(self):
        with self.memory.transaction() as db:
            expected,_=vector_index.candidates(db,self.model.embed('query'),self.model,self.scope)
        rows,state=self.query();self.assertEqual(state['cache'],'rebuilt')
        self.assertEqual([r['chunk_id'] for r in rows],[r['chunk_id'] for r in expected])
        np.testing.assert_allclose([r['dense_score'] for r in rows],[r['dense_score'] for r in expected],atol=1e-6)
        self.assertEqual(self.query()[1]['cache'],'hit')

    def test_local_delete_invalidates_and_zeroes_old_matrix(self):
        self.query();old=self.cache.vectors
        self.memory.delete_source(self.events[0]['id'])
        rows,state=self.query()
        self.assertEqual(state['cache'],'rebuilt');self.assertTrue(np.all(old==0))
        self.assertNotIn(self.events[0]['id'],[r['id'] for r in rows])

    def test_cross_connection_privacy_change_invalidates(self):
        self.query()
        with Memory(Path(self.tmp.name),test_key=b'C'*32) as other:
            with other.transaction(write=True) as db:
                db.execute("UPDATE occurrences SET sensitivity='private' WHERE revision_id=?",(self.events[1]['id'],))
        rows,state=self.query();self.assertEqual(state['cache'],'rebuilt')
        self.assertNotIn(self.events[1]['id'],[r['id'] for r in rows])

    def test_projection_rebuild_and_new_index_invalidate(self):
        self.query();self.memory.rebuild_chunks(self.events[2]['id'])
        self.assertEqual(self.query()[1]['missing'],1)
        self.memory.index_embeddings(self.model)
        self.assertEqual(self.query()[1]['missing'],0)

    def test_memory_budget_degrades_to_exact_uncached_scan(self):
        class Wide(Model):
            dimensions=128;space_id='wide';contract={'recipe':'wide-synthetic'}
            def embed(self,*args):
                v=np.zeros(128,dtype=np.float32);v[0]=1;return v
        model=Wide();self.memory.index_embeddings(model)
        cache=VectorCache(max_bytes=1024)
        with self.memory.transaction() as db:
            rows,state=cache.candidates(db,model.embed('q'),model,self.scope)
        self.assertEqual(len(rows),8);self.assertEqual(state['cache'],'budget_bypass')
        self.assertIsNone(cache.vectors)

    def test_scope_switch_cannot_return_previous_scope(self):
        self.query();other=self.memory.ingest('other scope',scope='project:other')
        self.memory.index_embeddings(self.model,scope='project:other')
        with self.memory.transaction() as db:
            rows,_=self.cache.candidates(db,self.model.embed('q'),self.model,'project:other')
        self.assertEqual([r['id'] for r in rows],[other['id']])

    def test_api_rechecks_revocation_and_clears_cache_on_close(self):
        grant=self.memory.grant('cache reader',[self.scope])
        self.memory.search('query',strategy='dense',embedder=self.model,token=grant['token'],use_vector_cache=True)
        matrix=self.memory._vector_cache.vectors
        self.memory.revoke(grant['id'])
        with self.assertRaises(PermissionError):
            self.memory.search('query',strategy='dense',embedder=self.model,token=grant['token'],use_vector_cache=True)
        self.memory.close();self.assertTrue(np.all(matrix==0))

    def test_temporal_filter_is_applied_on_cache_hit(self):
        from memory_engine.query import parse_query
        self.query()
        plan=parse_query('query on 2001-01-01',now='2026-09-10T00:00:00Z',tz='UTC')
        with self.memory.transaction() as db:
            rows,state=self.cache.candidates(db,self.model.embed('q'),self.model,self.scope,plan=plan)
        self.assertEqual(rows,[]);self.assertEqual(state['cache'],'hit')

    def test_close_waits_for_active_cache_reader_then_clears(self):
        self.memory.search('query',strategy='dense',embedder=self.model,use_vector_cache=True)
        cache=self.memory._vector_cache;matrix=cache.vectors
        entered=threading.Event();release=threading.Event()
        def reading():
            with self.memory.transaction() as db:
                entered.set();release.wait(3)
                return cache.candidates(db,self.model.embed('q'),self.model,self.scope)[1]['scanned']
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            reader=pool.submit(reading);self.assertTrue(entered.wait(1))
            closed=pool.submit(self.memory.close)
            self.assertFalse(closed.done());self.assertFalse(np.all(matrix==0))
            release.set();self.assertEqual(reader.result(),8);closed.result()
        self.assertTrue(np.all(matrix==0))
