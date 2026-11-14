from pathlib import Path
import unittest
from memory_engine.chunking import Recipe,project
from memory_helpers import Memory,MemoryDirectory


class ChunkingTests(unittest.TestCase):
    def test_formats_are_deterministic_exact_bounded_and_cover_all_bytes(self):
        cases=['Tere õhtust! Kas kõik sobib?\n\nJah, hästi. '*100,
               'Mira: do not send.\nUser: understood.\n'*100,
               'From: Liis\nTo: Karl\nSubject: update\n\nFriday replaces Monday.\n'*90,
               'line-item\n- one\n- two\n'*200,'C:\\work> build\nERROR CASE-7319\n'*100,
               'word '*1800,'Õ'*2500,'short UI','mixed: hind is not final. '*150]
        for text in cases:
            with self.subTest(text=text[:25]):
                raw=text.encode();parts=list(project(text));self.assertEqual(parts,list(project(text)))
                covered=set()
                for start,end,part in parts:
                    self.assertLessEqual(end-start,1200);self.assertEqual(raw[start:end].decode(),part)
                    covered.update(range(start,end))
                self.assertEqual(len(covered),len(raw))
                self.assertTrue(all(a[1]>b[0] for a,b in zip(parts,parts[1:])))

    def test_natural_paragraphs_and_known_message_boundaries(self):
        text='First sentence. '*35+'\n\n'+'Second paragraph. '*50
        parts=list(project(text));self.assertTrue(parts[0][2].endswith('\n\n'))
        boundary=len(('First sentence. '*35).encode())
        self.assertEqual(list(project(text,message_boundaries=[boundary]))[0][1],boundary)

    def test_invalid_utf8_adapter_boundary_and_recipe_fail(self):
        with self.assertRaises(ValueError):list(project('Õun',message_boundaries=[1]))
        with self.assertRaises(ValueError):Recipe(1200,800)

    def test_real_index_boundary_phrase_and_old_citation_after_rebuild(self):
        with MemoryDirectory() as d:
            m=Memory(Path(d),test_key=b'C'*32,chunk_recipe=Recipe().identity)
            a=m.ingest('x '*765+'Aster Nimbus')
            self.assertTrue(m.search('Aster Nimbus')['results'])
            before=m.fetch(a['id'],start_byte=1530,end_byte=1542)
            m.chunk_recipe=Recipe(720,120,120).identity;m.rebuild_chunks(a['id'])
            self.assertEqual(m.fetch(a['id'],start_byte=1530,end_byte=1542)['content'],before['content'])
