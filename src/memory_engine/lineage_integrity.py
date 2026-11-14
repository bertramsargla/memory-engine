"""Schema 9 repairs partial legacy support classification and index recipe tags."""
from . import evidence

LEXICAL_VERSION='fts5-unicode61-v1+porter-unicode61-v2'


def migrate(db):
    for table in ['claims','memory_items']:
        for action in ['insert','update','delete']:
            db.execute(f'DROP TRIGGER IF EXISTS {table}_normalize_{action}')
    evidence.interpretation_triggers(db)
    # Rebuild only relational support projections, without changing reviewed text
    # or ownership. A surviving subset of required support is explicitly partial.
    db.execute('UPDATE claims SET status=status')
    db.execute('UPDATE memory_items SET status=status')
    db.execute('''UPDATE retrieval_chunks SET lexical_version=? WHERE id IN (SELECT rowid FROM lexical_v2)
        AND id IN (SELECT rowid FROM search_index)''',(LEXICAL_VERSION,))
    db.execute('PRAGMA user_version=9')
