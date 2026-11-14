import json
from contextlib import closing
from pathlib import Path
import unittest
from memory_helpers import Memory,MemoryDirectory
from memory_engine.memory import initialize_schema
from memory_engine.storage import EncryptedStore


class IntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp=MemoryDirectory();self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name);self.m=Memory(self.home,test_key=b'B'*32)

    def test_partial_legacy_support_is_not_complete_support(self):
        a=self.m.ingest('original required evidence');cid=self.m.propose('reviewed interpretation',[a['id']]);self.m.confirm(cid)
        with self.m.transaction(write=True) as db:
            db.execute('UPDATE claims SET evidence=? WHERE id=?',(json.dumps([a['id'],'missing-history']),cid))
            item=db.execute('SELECT * FROM interpretations WHERE id=?',(cid,)).fetchone()
            self.assertEqual(item['support_state'],'partial');self.assertEqual(item['review_state'],'confirmed')
            self.assertEqual(db.execute('SELECT text FROM claims WHERE id=?',(cid,)).fetchone()[0],'reviewed interpretation')
        self.assertEqual(self.m.claims(),[])
        self.assertEqual(self.m.fetch(a['id'])['content'],'original required evidence')

    def test_repeated_support_id_is_one_edge(self):
        a=self.m.ingest('original');cid=self.m.propose('interpretation',[a['id'],a['id']])
        with self.m.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM interpretation_evidence WHERE interpretation_id=?',(cid,)).fetchone()[0],1)

    def test_nonfinite_or_boolean_confidence_is_rejected(self):
        a=self.m.ingest('original')
        for confidence in [float('nan'),float('inf'),True]:
            with self.assertRaises(ValueError):self.m.interpretation('value',[{'revision_id':a['id']}],confidence=confidence)

    def test_recipe_tags_match_both_lexical_projections(self):
        self.m.ingest('CASE-7319')
        with self.m.transaction() as db:
            self.assertEqual(db.execute('SELECT lexical_version FROM retrieval_chunks').fetchone()[0],'fts5-unicode61-v1+porter-unicode61-v2')

    def test_schema8_projection_repair_is_idempotent_and_preserves_review(self):
        self.m.close()
        # Build a real schema-8 catalog in a separate synthetic store. Its schema
        # shape is identical; emulate the old incorrect derived support state.
        home=self.home/'schema8'
        with closing(EncryptedStore(home,test_key=b'B'*32,initialize=lambda db:initialize_schema(db,target_version=8))) as store:
            with store.transaction(write=True) as db:
                db.execute("INSERT INTO claims VALUES ('old','project:memory_engine','reviewed value','[]','confirmed','2026-09-01T00:00:00Z','')")
                db.execute("UPDATE interpretations SET support_state='supported' WHERE id='old'")
        for _ in range(2):
            with Memory(home,test_key=b'B'*32) as m:
                with m.transaction() as db:
                    row=db.execute("SELECT * FROM interpretations WHERE id='old'").fetchone()
                    self.assertEqual(row['support_state'],'missing');self.assertEqual(row['review_state'],'confirmed')
                    self.assertEqual(row['value'],'reviewed value')
                self.assertEqual(m.status()['schema'],10)
