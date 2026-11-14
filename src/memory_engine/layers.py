"""Evidence-linked context layers. Automatic interpretation stays unconfirmed.

Chronicles/wiki use extractive previews, not model-written assertions. All tables
share the encrypted vault and scope checks. No model or network calls occur here.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
import json
import hashlib
import re
import uuid
from urllib.parse import urlsplit, urlunsplit


SCHEMA = """
CREATE TABLE snapshot_context(event TEXT PRIMARY KEY, context TEXT NOT NULL);
CREATE TABLE observations(id INTEGER PRIMARY KEY, event TEXT NOT NULL, scope TEXT NOT NULL,
 at TEXT NOT NULL, stream TEXT NOT NULL, session TEXT NOT NULL, app TEXT NOT NULL,
 source TEXT NOT NULL, elapsed REAL NOT NULL DEFAULT 0, context TEXT NOT NULL,
 UNIQUE(scope,stream,at));
CREATE INDEX observations_scope_time ON observations(scope,at);
CREATE TABLE chronicle_entries(scope TEXT,level TEXT,period TEXT,event TEXT,
 PRIMARY KEY(scope,level,period,event));
CREATE TABLE chronicle_links(scope TEXT,parent_level TEXT,parent_period TEXT,
 child_level TEXT,child_period TEXT,PRIMARY KEY(scope,parent_level,parent_period,child_level,child_period));
CREATE TABLE wiki_pages(id TEXT PRIMARY KEY,scope TEXT NOT NULL,kind TEXT NOT NULL,
 name TEXT NOT NULL,aliases TEXT NOT NULL DEFAULT '[]',summary TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL DEFAULT 'extractive',UNIQUE(scope,kind,name));
CREATE TABLE wiki_evidence(page TEXT,event TEXT,PRIMARY KEY(page,event));
CREATE TABLE memory_items(id TEXT PRIMARY KEY,scope TEXT NOT NULL,layer TEXT NOT NULL,
 category TEXT NOT NULL,text TEXT NOT NULL,event TEXT NOT NULL,start_byte INTEGER NOT NULL,
 end_byte INTEGER NOT NULL,status TEXT NOT NULL DEFAULT 'pending',due_at TEXT,
 created TEXT NOT NULL,supersedes TEXT NOT NULL DEFAULT '');
CREATE TABLE layer_processed(event TEXT PRIMARY KEY);
CREATE VIRTUAL TABLE layer_index USING fts5(text,scope UNINDEXED,ref UNINDEXED,kind UNINDEXED,
 tokenize='unicode61 remove_diacritics 2');
PRAGMA user_version=2;
"""
LAYERS = ('open_loops', 'identity', 'writing', 'reminders')
KINDS = ('person', 'project', 'company', 'tool', 'topic')
LEVELS = ('hour', 'day', 'week', 'month', 'year')


def timestamp(value=None):
    dt = datetime.fromisoformat(value) if value else datetime.now(timezone.utc)
    if dt.tzinfo is None:
        raise ValueError('A timestamp needs a timezone.')
    return dt.astimezone(timezone.utc).isoformat()


def clean_context(context=None):
    context = context or {}
    if not isinstance(context, dict):
        raise ValueError('Snapshot context must be an object.')
    allowed = {'url', 'domain', 'focused_name', 'focused_role', 'focused_path',
               'document_id', 'device_id', 'surface', 'completeness', 'stream_id',
               'capture_trigger', 'capture_method', 'capture_finished_at', 'window_id',
               'process_id', 'process_started_at', 'observed_offset', 'url_source', 'observation_basis'}
    if set(context) - allowed or any(not isinstance(v, str) or len(v) > 2048 for v in context.values()):
        raise ValueError('Unknown or oversized snapshot context.')
    result = {key: context.get(key, '') for key in allowed}
    if result['capture_trigger'] and result['capture_trigger'] not in {
            'startup', 'periodic_check', 'foreground_change', 'content_change', 'scroll', 'resume'}:
        raise ValueError('Unknown capture trigger; a content change does not prove typing or sending.')
    for key in ('capture_finished_at', 'process_started_at'):
        if result[key]:
            result[key] = timestamp(result[key])
    if result['url']:
        parsed = urlsplit(result['url'])
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('Context URL must be an HTTP(S) page without credentials.')
        # Query/fragment often contain access tokens. Store origin/path only.
        result['url'] = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, '', ''))
        result['domain'] = parsed.hostname.lower()
    elif result['domain']:
        raise ValueError('A domain requires a source URL.')
    return result


def migrate(db):
    # executescript commits implicitly: use statements so a failure cannot leave
    # a partially persisted schema. The caller owns the SQLite transaction.
    for statement in SCHEMA.split(';'):
        if statement.strip():
            db.execute(statement)


def migrate_v3(db):
    db.execute("ALTER TABLE memory_items ADD COLUMN ownership TEXT NOT NULL DEFAULT 'unknown'")
    db.execute('''CREATE TABLE candidate_suppressions(scope TEXT,source_hash TEXT,text_hash TEXT,layer TEXT,
        PRIMARY KEY(scope,source_hash,text_hash,layer))''')
    db.execute('PRAGMA user_version=3')


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def periods(at):
    dt = datetime.fromisoformat(at).astimezone(timezone.utc)
    iso = dt.isocalendar()
    return {'hour': dt.strftime('%Y-%m-%dT%H'), 'day': dt.strftime('%Y-%m-%d'),
            'week': f'{iso.year}-W{iso.week:02d}', 'month': dt.strftime('%Y-%m'),
            'year': dt.strftime('%Y')}


def index(db, ref, kind, scope, text):
    db.execute('DELETE FROM layer_index WHERE ref=?', (ref,))
    db.execute('INSERT INTO layer_index(text,scope,ref,kind) VALUES (?,?,?,?)', (text, scope, ref, kind))


def timeline(db, scope, event, at):
    keys = periods(at)
    for level, period in keys.items():
        db.execute('INSERT OR IGNORE INTO chronicle_entries VALUES (?,?,?,?)', (scope, level, period, event))
    # Weeks and calendar months overlap, so both roll up days independently.
    for parent, child in (('day', 'hour'), ('week', 'day'), ('month', 'day'), ('year', 'month')):
        db.execute('INSERT OR IGNORE INTO chronicle_links VALUES (?,?,?,?,?)', (scope, parent, keys[parent], child, keys[child]))


def add_page(db, scope, kind, name, event):
    name = ' '.join(name.split())[:160]
    # Group explicit labels within this scope/type only. These are mention groups,
    # not a claim that two people with the same name are the same person.
    found = db.execute('SELECT id FROM wiki_pages WHERE scope=? AND kind=? AND name=?',
                       (scope, kind, name)).fetchone()
    pid = found[0] if found else str(uuid.uuid4())
    if not found:
        db.execute('INSERT INTO wiki_pages(id,scope,kind,name) VALUES (?,?,?,?)', (pid, scope, kind, name))
        index(db, pid, 'wiki', scope, name)
    linked = db.execute('INSERT OR IGNORE INTO wiki_evidence VALUES (?,?)', (pid, event))
    if found and linked.rowcount:
        db.execute("UPDATE wiki_pages SET status='needs_review' WHERE id=? AND summary<>''", (pid,))
    return pid


def derive(db, event):
    row = db.execute('SELECT * FROM events WHERE id=?', (event,)).fetchone()
    if db.execute('SELECT 1 FROM layer_processed WHERE event=?', (event,)).fetchone():
        return
    scope, content = row['scope'], row['content']
    if scope.startswith('project:'):
        add_page(db, scope, 'project', scope.split(':', 1)[1], event)
    if row['kind'] == 'capture' and row['app']:
        add_page(db, scope, 'tool', row['app'], event)
    # Explicit labels are intentionally conservative; no capitalized-word NER.
    labels = {'person': 'person', 'people': 'person', 'project': 'project',
              'company': 'company', 'organization': 'company', 'tool': 'tool', 'topic': 'topic'}
    offset = 0
    extracted = 0
    fence = None
    for original in content.splitlines(keepends=True):
        line = original.strip()
        # Obvious quoted/code examples are not automatically mined as identity
        # or commitments. Other linguistic ambiguity still requires review.
        if line.startswith(('```', '~~~')):
            marker = line[:3]
            fence = None if fence == marker else (fence or marker)
            offset += len(original.encode('utf-8'))
            continue
        if fence or line.startswith('>'):
            offset += len(original.encode('utf-8'))
            continue
        match = re.match(r'(?i)^(person|people|project|company|organization|tool|topic):\s*(.{1,160})$', line)
        if match and extracted < 80:
            add_page(db, scope, labels[match[1].lower()], match[2], event)
            extracted += 1
        layer, category = None, None
        for pattern, candidate_layer, candidate_category in (
            (r'(?i)^(?:todo|task|commitment|i will|i\x27ll)\b', 'open_loops', 'commitment'),
            (r'(?i)^(?:blocker|blocked|waiting for)\b', 'open_loops', 'blocker'),
            (r'(?i)^(?:question|follow.up)\s*:', 'open_loops', 'follow_up'),
            (r'(?i)^(?:preference|i prefer)\b', 'identity', 'preference'),
            (r'(?i)^(?:goal|my goal)\b', 'identity', 'goal'),
            (r'(?i)^identity\s*:', 'identity', 'fact'),
            (r'(?i)^writing (?:rule|style)\s*:', 'writing', 'rule'),
            (r'(?i)^writing example\s*:', 'writing', 'example'),
            (r'(?i)^remind(?:er| me)\b', 'reminders', 'reminder'),
        ):
            if re.search(pattern, line):
                layer, category = candidate_layer, candidate_category
                break
        suppressed = layer and db.execute('SELECT 1 FROM candidate_suppressions WHERE scope=? AND source_hash=? AND text_hash=? AND layer=?',
                                          (scope, digest(row['source']), digest(line), layer)).fetchone()
        if layer and not suppressed and len(line) <= 2000 and extracted < 80:
            begin = offset + len(original[:len(original)-len(original.lstrip())].encode('utf-8'))
            end = begin + len(line.encode('utf-8'))
            mid = str(uuid.uuid4())
            db.execute('INSERT INTO memory_items(id,scope,layer,category,text,event,start_byte,end_byte,created) VALUES (?,?,?,?,?,?,?,?,?)',
                       (mid, scope, layer, category, line, event, begin, end, row['observed']))
            index(db, mid, layer, scope, line)
            extracted += 1
        offset += len(original.encode('utf-8'))
    # Known names/owner-supplied aliases can collect later unlabelled mentions.
    # Bounded candidate list: not an unrestricted semantic entity recognizer.
    for page in db.execute('SELECT * FROM wiki_pages WHERE scope=? ORDER BY name LIMIT 200', (scope,)).fetchall():
        names = [page['name'], *json.loads(page['aliases'])]
        if any(len(name) >= 3 and re.search(r'(?<!\w)'+re.escape(name)+r'(?!\w)', content, re.I) for name in names):
            linked = db.execute('INSERT OR IGNORE INTO wiki_evidence VALUES (?,?)', (page['id'], event))
            if linked.rowcount and page['summary']:
                db.execute("UPDATE wiki_pages SET status='needs_review' WHERE id=?", (page['id'],))
    db.execute('INSERT INTO layer_processed VALUES (?)', (event,))


def observe(db, event, context, at):
    row = db.execute('SELECT * FROM events WHERE id=?', (event,)).fetchone()
    payload = json.dumps(context, ensure_ascii=False)
    timeline(db, row['scope'], event, at)
    if row['kind'] != 'capture':
        db.execute('INSERT OR REPLACE INTO snapshot_context VALUES (?,?)', (event, payload))
        return
    stream = context.get('stream_id') or 'unspecified'
    previous = db.execute('SELECT * FROM observations WHERE scope=? AND stream=? ORDER BY at DESC LIMIT 1',
                          (row['scope'], stream)).fetchone()
    if previous and at <= previous['at']:
        # Late/replayed captures contribute evidence but never rewrite current
        # focus or manufacture elapsed time. Imports are not a time ledger.
        return
    session = str(uuid.uuid4())
    if previous:
        delta = (datetime.fromisoformat(at)-datetime.fromisoformat(previous['at'])).total_seconds()
        if previous['app'] == row['app'] and delta <= 30:
            session = previous['session']
        if previous['app'] == row['app'] and previous['source'] == row['source'] and delta <= 15:
            db.execute('UPDATE observations SET elapsed=? WHERE id=?', (delta, previous['id']))
    db.execute('INSERT INTO observations(event,scope,at,stream,session,app,source,context) VALUES (?,?,?,?,?,?,?,?)',
               (event, row['scope'], at, stream, session, row['app'], row['source'], payload))
    db.execute('INSERT OR REPLACE INTO snapshot_context VALUES (?,?)', (event, payload))


def erase(db, ids, *, keep_decisions=False):
    for event in ids:
        if not keep_decisions:
            source = db.execute('SELECT scope,source FROM events WHERE id=?', (event,)).fetchone()
            if source:
                db.execute('DELETE FROM candidate_suppressions WHERE scope=? AND source_hash=?', (source['scope'], digest(source['source'])))
        affected = [r[0] for r in db.execute('SELECT page FROM wiki_evidence WHERE event=?', (event,))]
        for row in db.execute('SELECT id FROM memory_items WHERE event=?', (event,)).fetchall():
            db.execute('DELETE FROM layer_index WHERE ref=?', (row[0],))
        for table in ('memory_items', 'observations', 'snapshot_context', 'chronicle_entries', 'wiki_evidence', 'layer_processed'):
            db.execute(f'DELETE FROM {table} WHERE event=?', (event,))
        for pid in affected:
            page = db.execute('SELECT * FROM wiki_pages WHERE id=?', (pid,)).fetchone()
            db.execute('DELETE FROM layer_index WHERE ref=?', (pid,))
            if not db.execute('SELECT 1 FROM wiki_evidence WHERE page=?', (pid,)).fetchone():
                db.execute('DELETE FROM wiki_pages WHERE id=?', (pid,))
            else:
                # A reviewed synthesis/alias may depend on erased evidence.
                db.execute("UPDATE wiki_pages SET summary='',aliases='[]',status='extractive' WHERE id=?", (pid,))
                index(db, pid, 'wiki', page['scope'], page['name'])
    if ids:
        db.execute('''DELETE FROM chronicle_links WHERE NOT EXISTS (
            SELECT 1 FROM chronicle_entries c WHERE c.scope=chronicle_links.scope AND c.level=chronicle_links.child_level AND c.period=chronicle_links.child_period)
            OR NOT EXISTS (SELECT 1 FROM chronicle_entries c WHERE c.scope=chronicle_links.scope AND c.level=chronicle_links.parent_level AND c.period=chronicle_links.parent_period)''')


def excerpt(row):
    text = row['content'][:500]
    return {'event_id': row['id'], 'title': row['title'], 'observed': row['observed'], 'text': text,
            'citation': f"memory-engine://evidence/{row['id']}#0:{len(text.encode('utf-8'))}"}


class LayerMemory:
    def link_entity(self, name, kind, event_id, *, scope='project:memory_engine'):
        if kind not in KINDS or not isinstance(name, str) or not name.strip() or len(name) > 160:
            raise ValueError('Choose an entity type and a name up to 160 characters.')
        with self.transaction(write=True) as db:
            self._authorize(db, scope, None)
            if not db.execute('SELECT 1 FROM events WHERE id=? AND scope=?', (event_id, scope)).fetchone():
                raise KeyError('Evidence not found in this scope.')
            return add_page(db, scope, kind, name, event_id)

    def organize(self, *, scope='project:memory_engine', limit=100):
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError('Choose 1 to 500 records per batch.')
        with self.transaction(write=True) as db:
            self._authorize(db, scope, None)
            rows = db.execute('SELECT * FROM events WHERE scope=? AND id NOT IN (SELECT event FROM layer_processed) ORDER BY observed LIMIT ?',
                              (scope, limit)).fetchall()
            for row in rows:
                derive(db, row['id'])
                timeline(db, scope, row['id'], row['observed'])
            remaining = db.execute('SELECT count(*) FROM events WHERE scope=? AND id NOT IN (SELECT event FROM layer_processed)', (scope,)).fetchone()[0]
            return {'processed': len(rows), 'remaining': remaining}

    def snapshot_details(self, event_id, *, scope='project:memory_engine', token=None, before_id=None, limit=50):
        with self.transaction() as db:
            self._authorize(db, scope, token)
            if not db.execute('SELECT 1 FROM eligible_events WHERE id=? AND scope=?', (event_id, scope)).fetchone():
                raise KeyError('Snapshot not found in this scope.')
            row = db.execute('SELECT context FROM snapshot_context WHERE event=?', (event_id,)).fetchone()
            count, duration = db.execute('SELECT count(*),coalesce(sum(elapsed),0) FROM eligible_observations WHERE event=?', (event_id,)).fetchone()
            from . import provenance
            return {'context': json.loads(row[0]) if row else {}, 'sightings': count, 'duration_s': duration,
                    'provenance': provenance.summary(db, event_id),
                    'history': provenance.page(db, event_id, before_id=before_id, limit=limit),
                    'duration_method': 'Consecutive same-source samples at most 15 seconds apart; conservative estimate.'}

    def _wiki(self, db, pid, scope):
        row = db.execute('SELECT * FROM eligible_wiki_pages WHERE id=? AND scope=?', (pid, scope)).fetchone()
        if row is None:
            raise KeyError('Wiki page not found in this scope.')
        result = dict(row)
        result['aliases'] = json.loads(row['aliases'])
        evidence = db.execute('SELECT e.* FROM eligible_wiki_evidence w JOIN eligible_events e ON e.id=w.event WHERE w.page=? AND e.scope=? ORDER BY e.observed DESC LIMIT 12', (pid, scope)).fetchall()
        result['evidence'] = [excerpt(e) for e in evidence]
        result['summary'] = row['summary'] or '\n'.join('• ' + e['title'] + ': ' + e['content'][:160].replace('\n', ' ') for e in evidence[:5])
        result['evidence_count'] = db.execute('SELECT count(*) FROM eligible_wiki_evidence WHERE page=?', (pid,)).fetchone()[0]
        result['related'] = [dict(r) for r in db.execute('''SELECT DISTINCT p.id,p.kind,p.name FROM eligible_wiki_evidence a
            JOIN eligible_wiki_evidence b ON a.event=b.event JOIN eligible_wiki_pages p ON p.id=b.page
            WHERE a.page=? AND p.id<>? AND p.scope=? LIMIT 30''', (pid, pid, scope))]
        result['notice'] = 'Related pages share evidence, not a proven relationship. Extractive pages group mentions; check original sources.'
        return result

    def layer_record(self, ref, *, scope='project:memory_engine', token=None):
        with self.transaction() as db:
            self._authorize(db, scope, token)
            if db.execute('SELECT 1 FROM eligible_wiki_pages WHERE id=? AND scope=?', (ref, scope)).fetchone():
                return self._wiki(db, ref, scope)
            row = db.execute('SELECT * FROM eligible_memory_items WHERE id=? AND scope=?', (ref, scope)).fetchone()
            if row is None:
                raise KeyError('Layer record not found in this scope.')
            result = dict(row)
            result['citation'] = f"memory-engine://evidence/{row['event']}#{row['start_byte']}:{row['end_byte']}"
            result['source_quote'] = db.execute('SELECT content FROM eligible_events WHERE id=?', (row['event'],)).fetchone()[0].encode('utf-8')[row['start_byte']:row['end_byte']].decode('utf-8')
            return result

    def layer_list(self, layer='wiki', *, scope='project:memory_engine', token=None, query='', limit=50, review_state=None):
        if layer not in (*LAYERS, 'wiki', 'sessions', 'time') or type(limit) is not int or not 1 <= limit <= 100 or not isinstance(query, str) or len(query) > 2000:
            raise ValueError('Invalid layer, query or limit.')
        if review_state not in (None,'confirmed'):
            raise ValueError('Invalid review filter.')
        with self.transaction() as db:
            self._authorize(db, scope, token)
            if layer == 'sessions':
                return [dict(r) for r in db.execute('''SELECT session AS id,app,min(at) AS started,max(at) AS last_seen,
                    sum(elapsed) AS duration_s,count(*) AS sightings,count(DISTINCT event) AS snapshots
                    FROM eligible_observations WHERE scope=? GROUP BY session ORDER BY started DESC LIMIT ?''', (scope, limit))]
            if layer == 'time':
                return self._time(db, scope, limit)
            if query.strip():
                words = re.findall(r'\w+', query)[:32]
                if not words:
                    return []
                expr = ' OR '.join('"'+w+'"' for w in words)
                eligible = 'SELECT id FROM eligible_wiki_pages' if layer=='wiki' else "SELECT id FROM eligible_memory_items" + (" WHERE status='confirmed'" if review_state else '')
                refs = [r[0] for r in db.execute('SELECT ref FROM layer_index WHERE layer_index MATCH ? AND scope=? AND kind=? AND ref IN ('+eligible+') ORDER BY bm25(layer_index) LIMIT ?', (expr, scope, layer, limit))]
            elif layer == 'wiki':
                refs = [r[0] for r in db.execute('SELECT id FROM eligible_wiki_pages WHERE scope=? ORDER BY name LIMIT ?', (scope, limit))]
            else:
                refs = [r[0] for r in db.execute("SELECT id FROM eligible_memory_items WHERE scope=? AND layer=? AND status NOT IN ('dismissed','superseded')"+(" AND status='confirmed'" if review_state else '')+" ORDER BY created DESC LIMIT ?", (scope, layer, limit))]
            if layer == 'wiki':
                return [self._wiki(db, ref, scope) for ref in refs]
            return [dict(db.execute('SELECT * FROM eligible_memory_items WHERE id=? AND scope=?', (ref, scope)).fetchone()) for ref in refs]

    def _time(self, db, scope, limit):
        # Split intervals at UTC hour boundaries rather than charging a full
        # interval to the previous hour/day. Evidence ingestion is never work time.
        totals = {}
        for row in db.execute('SELECT at,elapsed,app,context FROM eligible_observations WHERE scope=? AND elapsed>0', (scope,)):
            start = datetime.fromisoformat(row['at'])
            end = start + timedelta(seconds=row['elapsed'])
            while start < end:
                boundary = start.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
                until = min(boundary, end)
                key = (start.strftime('%Y-%m-%dT%H'), row['app'], json.loads(row['context']).get('domain', ''))
                totals[key] = totals.get(key, 0) + (until-start).total_seconds()
                start = until
        return [{'hour_utc': k[0], 'app': k[1], 'domain': k[2], 'duration_s': v,
                 'project': scope, 'estimated': True} for k, v in sorted(totals.items(), reverse=True)[:limit]]

    def chronicle(self, level='day', period='', *, scope='project:memory_engine', token=None, limit=50):
        if level not in LEVELS or not isinstance(period, str) or len(period) > 32 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('Invalid Chronicle period or limit.')
        with self.transaction() as db:
            self._authorize(db, scope, token)
            groups = db.execute('''SELECT period,count(*) AS snapshots FROM eligible_chronicle_entries
                WHERE scope=? AND level=? AND (?='' OR period=?) GROUP BY period ORDER BY period DESC LIMIT ?''',
                (scope, level, period, period, limit)).fetchall()
            result = []
            for group in groups:
                rows = db.execute('''SELECT e.* FROM eligible_chronicle_entries c JOIN eligible_events e ON e.id=c.event
                    WHERE c.scope=? AND c.level=? AND c.period=? ORDER BY e.observed DESC LIMIT 12''',
                    (scope, level, group['period'])).fetchall()
                result.append({'level': level, 'period': group['period'], 'timezone': 'UTC', 'snapshots': group['snapshots'],
                    'children': [dict(r) for r in db.execute('SELECT child_level AS level,child_period AS period FROM chronicle_links WHERE scope=? AND parent_level=? AND parent_period=? ORDER BY child_period', (scope, level, group['period']))],
                    'summary_kind': 'extractive', 'summary': '\n'.join(e['title'] + ': ' + e['content'][:160].replace('\n', ' ') for e in rows[:5]),
                    'evidence': [excerpt(e) for e in rows], 'omitted_evidence': max(0, group['snapshots']-len(rows))})
            return result

    def review_item(self, ref, status, *, scope='project:memory_engine', replacement=None, due_at=None, ownership=None):
        if status not in ('confirmed', 'dismissed', 'closed'):
            raise ValueError('Choose confirmed, dismissed or closed.')
        if ownership not in (None, 'user', 'other', 'unknown'):
            raise ValueError('Invalid commitment ownership.')
        if replacement is not None and (not isinstance(replacement, str) or not replacement.strip() or len(replacement) > 2000):
            raise ValueError('Correction must contain 1 to 2,000 characters.')
        due = timestamp(due_at) if due_at else None
        with self.transaction(write=True) as db:
            self._authorize(db, scope, None)
            row = db.execute('SELECT * FROM memory_items WHERE id=? AND scope=?', (ref, scope)).fetchone()
            if row is None:
                raise KeyError('Layer record not found.')
            if row['status'] == 'superseded':
                raise ValueError('This record is no longer active.')
            if ownership is not None and row['layer'] != 'open_loops':
                raise ValueError('Ownership applies to open loops.')
            owner = ownership or ('user' if row['layer'] == 'open_loops' and status == 'confirmed' else row['ownership'])
            if owner == 'other' and status in ('confirmed', 'closed'):
                raise ValueError('A commitment marked not mine cannot be confirmed or completed as mine.')
            if status == 'closed' and row['layer'] not in ('open_loops', 'reminders'):
                raise ValueError('Only loops and reminders can be closed.')
            source = db.execute('SELECT source,content FROM events WHERE id=?', (row['event'],)).fetchone()
            quote = source['content'].encode('utf-8')[row['start_byte']:row['end_byte']].decode('utf-8')
            key = (scope, digest(source['source']), digest(quote), row['layer'])
            if status in ('dismissed', 'closed'):
                db.execute('INSERT OR IGNORE INTO candidate_suppressions VALUES (?,?,?,?)', key)
            elif status == 'confirmed':
                db.execute('DELETE FROM candidate_suppressions WHERE scope=? AND source_hash=? AND text_hash=? AND layer=?', key)
            if replacement is not None:
                new = str(uuid.uuid4())
                db.execute("UPDATE memory_items SET status='superseded' WHERE id=?", (ref,))
                db.execute('''INSERT INTO memory_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (new, scope, row['layer'], row['category'], replacement, row['event'], row['start_byte'], row['end_byte'], status, due or row['due_at'], timestamp(), ref, owner))
                index(db, new, row['layer'], scope, replacement)
                return new
            db.execute('UPDATE memory_items SET status=?,due_at=coalesce(?,due_at),ownership=? WHERE id=?', (status, due, owner, ref))
            return ref

    def edit_wiki(self, ref, summary, aliases, *, scope='project:memory_engine'):
        if not isinstance(summary, str) or len(summary) > 4000 or not isinstance(aliases, list) or len(aliases) > 20 or any(not isinstance(a, str) or not a.strip() or len(a) > 160 for a in aliases):
            raise ValueError('Use a summary up to 4,000 characters and up to 20 aliases.')
        with self.transaction(write=True) as db:
            self._authorize(db, scope, None)
            row = db.execute('SELECT * FROM wiki_pages WHERE id=? AND scope=?', (ref, scope)).fetchone()
            if row is None:
                raise KeyError('Wiki page not found.')
            db.execute("UPDATE wiki_pages SET summary=?,aliases=?,status='reviewed' WHERE id=?", (summary, json.dumps(aliases), ref))
            index(db, ref, 'wiki', scope, row['name']+' '+ ' '.join(aliases)+' '+summary)

    def relevant_layers(self, query, *, scope='project:memory_engine', token=None, limit=4):
        if type(limit) is not int or not 1 <= limit <= 10:
            raise ValueError('Invalid context limit.')
        # Reading lists rechecks the grant. Only explicit owner-confirmed items
        # enter routine drafting; auto-extracted hypotheses remain inspectable.
        result = []
        for layer in ('wiki', 'identity', 'writing', 'open_loops'):
            for row in self.layer_list(layer, scope=scope, token=token, query=query, limit=limit,
                                       review_state='confirmed' if layer!='wiki' else None):
                if layer != 'wiki' and row['status'] != 'confirmed':
                    continue
                if layer == 'open_loops' and row['ownership'] != 'user':
                    continue
                if layer == 'wiki':
                    if row['status'] == 'needs_review':
                        continue
                    result.append({'layer': layer, 'name': row['name'], 'summary': row['summary'],
                        'status': row['status'], 'evidence': row['evidence'][:2]})
                else:
                    original = self.layer_record(row['id'], scope=scope, token=token)
                    result.append({'layer': layer, 'text': row['text'], 'status': row['status'],
                        'source_quote': original['source_quote'],
                        'citation': f"memory-engine://evidence/{row['event']}#{row['start_byte']}:{row['end_byte']}"})
        return result[:limit*2]
