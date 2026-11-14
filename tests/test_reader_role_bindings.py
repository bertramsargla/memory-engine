import unittest
from memory_engine.reader_role_bindings import RoleResolver,RoleBinding,resolve_name
from memory_engine.reader_contracts import Identity
from memory_engine.reader_identity import actor_key


class Backend:
    contract={'recipe':'test-all-analyses'}
    def analyses(self,s):
        return {
            'Uue':[dict(lemma='uus',pos='A',form='sg g')],
            'Uus':[dict(lemma='uus',pos='A',form='sg n')],
            'summa':[dict(lemma='summ',pos='S',form='sg g'),dict(lemma='summa',pos='S',form='sg g'),dict(lemma='summa',pos='S',form='sg n')],
            'Kertu':[dict(lemma='Kertu',pos='H',form='sg n'),dict(lemma='Kertu',pos='H',form='sg g')],
            'Kaarli':[dict(lemma='Kaarel',pos='H',form='sg g'),dict(lemma='Kaarli',pos='H',form='sg n')],
            'Esimese':[dict(lemma='esimene',pos='O',form='sg g')],
        }.get(s,[])


class RoleBindingTests(unittest.TestCase):
    def setUp(self):self.r=RoleResolver(Backend())

    def test_roles_do_not_require_single_object_lemma(self):
        r=self.r.resolve('Uue summa kinnitas Kertu.')
        self.assertEqual(r.state,'RESOLVED')
        self.assertEqual((r.frame.actor,r.frame.object),('Kertu','Uue summa'))
        obj=next(b for b in r.bindings if b.role=='object')
        self.assertEqual(obj.normalized_surface_candidates,('summ','summa'))

    def test_agent_last_does_not_use_actor_lemma_as_identity(self):
        r=self.r.resolve('Uus summa kinnitati Kaarli poolt.')
        self.assertEqual(r.state,'RESOLVED')
        actor=next(b for b in r.bindings if b.role=='actor')
        self.assertEqual(actor.identity_state,'UNKNOWN')
        self.assertEqual(actor.normalized_surface_candidates,('Kaarel','Kaarli'))
        self.assertEqual(r.frame.actor,'Kaarli')

    def test_unicode_spans_include_original_offset(self):
        text='  Uue summa kinnitas Kertu. '
        r=self.r.resolve(text,source_start=17)
        self.assertEqual(r.state,'RESOLVED')
        for b in r.bindings:self.assertEqual(text.encode()[b.start_byte-17:b.end_byte-17].decode(),b.surface)

    def test_ordinal_case_is_not_discarded(self):
        self.assertEqual(self.r.resolve('Esimese summa kinnitas Kertu.').state,'RESOLVED')

    def test_disagreement_does_not_guess(self):
        self.assertEqual(self.r.resolve('Uus summa kinnitas Kertu.').state,'UNKNOWN')
        self.assertEqual(self.r.resolve('Kertu kinnitas Kaarli.').state,'UNKNOWN')

    def test_non_assertions_do_not_become_positive_roles(self):
        for text in ['Kertu jaoks kinnitati uus summa.','Kertu ei kinnitanud uut summat.',
                     'Võimalik, et Kertu kinnitab uue summa.','Kertu ütles: "Uus summa kinnitati."',
                     'Kui Kertu kinnitab uue summa, jätkame.']:
            self.assertIsNone(self.r.resolve(text).frame)

    def test_identity_context_narrows_but_does_not_create_identity(self):
        b=RoleBinding('Kaarli',0,6,'actor',(),('Kaarel','Kaarli'))
        one=Identity('a','Kaarel','mail','team','work',1.)
        two=Identity('b','Kaarli','mail','team','work',1.)
        self.assertEqual(resolve_name(b,[(one,())])['state'],'UNKNOWN')
        self.assertEqual(resolve_name(b,[(one,()),(two,())])['state'],'AMBIGUOUS')
        self.assertEqual(resolve_name(b,[(one,()),(two,())],bound_native_key=actor_key(one))['state'],'RESOLVED')
        self.assertEqual(resolve_name(b,[(one,())],bound_native_key='wrong')['state'],'UNKNOWN')

    def test_unknown_morphology_stays_unknown(self):
        self.assertEqual(self.r.resolve('Uue summa kinnitas Xyzname.').state,'UNKNOWN')

    def test_dependency_only_supplies_roles_not_lemmas(self):
        import hashlib
        text='Uue summa kinnitas Xyzname.'
        class Dependency:
            contract={'recipe':'synthetic-dependency'}
            def roles(self,t):return dict(input_sha256=hashlib.sha256(t.encode()).hexdigest(),actor=(19,26),object=(4,9),actor_lemma='WrongPerson')
        r=RoleResolver(Backend(),dependency=Dependency()).resolve(text)
        self.assertEqual(r.state,'RESOLVED');self.assertEqual(r.frame.actor,'Xyzname')
        self.assertEqual(next(b for b in r.bindings if b.role=='actor').identity_state,'UNKNOWN')

    def test_dependency_disagreement_is_ambiguous(self):
        import hashlib
        class Dependency:
            contract={'recipe':'synthetic-dependency'}
            def roles(self,t):return dict(input_sha256=hashlib.sha256(t.encode()).hexdigest(),actor=(4,9),object=(19,24))
        r=RoleResolver(Backend(),dependency=Dependency()).resolve('Uue summa kinnitas Kertu.')
        self.assertEqual(r.state,'AMBIGUOUS');self.assertIsNone(r.frame)

    def test_dependency_wrong_input_rejected(self):
        class Dependency:
            contract={'recipe':'synthetic-dependency'}
            def roles(self,t):return dict(input_sha256='wrong',actor=(19,24),object=(4,9))
        self.assertEqual(RoleResolver(Backend(),dependency=Dependency()).resolve('Uue summa kinnitas Kertu.').state,'UNKNOWN')
