"""Occurrence-level origin records, kept with encrypted evidence and its scope."""
import json


ACTIONS = {
    'capture': 'screen_observed', 'note': 'note_saved', 'document': 'document_imported',
    'dictation': 'transcript_saved', 'reference': 'reference_saved',
}


def metadata(row, context, *, legacy=False):
    result = dict(context)
    result.update(app=row['app'], title=row['title'], source=row['source'], locator=row['locator'],
                  kind=row['kind'], quality=row['quality'], attribution=row['attribution'])
    result['action'] = 'unknown' if legacy else ACTIONS[row['kind']]
    result['action_basis'] = 'unavailable' if legacy else 'capture_observation' if row['kind'] == 'capture' else 'save_operation'
    result['url_status'] = 'origin_path_only' if context.get('url') else 'unavailable'
    result['url_source'] = (context.get('url_source') or 'caller_supplied') if context.get('url') else 'unavailable'
    result['metadata_status'] = 'legacy_partial' if legacy else 'recorded'
    if legacy:
        result['observation_basis'] = 'legacy_observation_time'
    return result


def migrate(db):
    db.execute('''CREATE TABLE evidence_occurrences(
        id INTEGER PRIMARY KEY, event TEXT NOT NULL, observed_at TEXT NOT NULL,
        recorded_at TEXT NOT NULL, metadata TEXT NOT NULL)''')
    db.execute('CREATE INDEX occurrence_event_time ON evidence_occurrences(event,observed_at,id)')
    # Preserve actual historical sightings. Never invent ingestion times/actions.
    for event in db.execute('SELECT * FROM events').fetchall():
        observations = db.execute('SELECT at,context FROM observations WHERE event=? ORDER BY id', (event['id'],)).fetchall()
        if observations:
            for occurrence in observations:
                payload = metadata(event, json.loads(occurrence['context']), legacy=True)
                db.execute('INSERT INTO evidence_occurrences(event,observed_at,recorded_at,metadata) VALUES (?,?,?,?)',
                           (event['id'], occurrence['at'], '', json.dumps(payload, ensure_ascii=False)))
        else:
            # Latest snapshot context cannot establish the original occurrence's location.
            payload = metadata(event, {}, legacy=True)
            db.execute('INSERT INTO evidence_occurrences(event,observed_at,recorded_at,metadata) VALUES (?,?,?,?)',
                       (event['id'], event['observed'], '', json.dumps(payload, ensure_ascii=False)))
    db.execute('PRAGMA user_version=4')


def record(db, event, context, observed_at, recorded_at, *, title, app, locator, kind, quality, attribution):
    row = dict(db.execute('SELECT * FROM events WHERE id=?', (event,)).fetchone())
    # A deduplicated event retains its first text revision. Each new occurrence
    # must still describe this caller's app/title/location, not stale event columns.
    row.update(title=title, app=app, locator=locator, kind=kind, quality=quality, attribution=attribution)
    payload = metadata(row, context)
    cursor = db.execute('INSERT INTO occurrences(revision_id,observed_at,recorded_at,metadata) VALUES (?,?,?,?)',
                       (event, observed_at, recorded_at, json.dumps(payload, ensure_ascii=False)))
    return cursor.lastrowid


def decode(row):
    result = {'id': row['id'], 'observed_at': row['observed_at'], 'recorded_at': row['recorded_at'],
              **json.loads(row['metadata'])}
    for key in ('revision_id','authored_at','known_at','knowledge_basis','author_id','author_name',
                'action_state','action_basis','origin_kind','sensitivity','trust_class','completeness',
                'account_id','workspace_id','thread_id'):
        if key in row.keys():
            # Keep the legacy capture-operation basis distinct from action outcome.
            result['outcome_basis' if key == 'action_basis' else key] = row[key]
    return result


def summary(db, event):
    first = db.execute('SELECT * FROM occurrences WHERE revision_id=? ORDER BY observed_at,id LIMIT 1', (event,)).fetchone()
    latest = db.execute('SELECT * FROM occurrences WHERE revision_id=? ORDER BY observed_at DESC,id DESC LIMIT 1', (event,)).fetchone()
    count = db.execute('SELECT count(*) FROM evidence_occurrences WHERE event=?', (event,)).fetchone()[0]
    return {'first': decode(first) if first else None, 'latest': decode(latest) if latest else None, 'occurrences': count}


def page(db, event, *, before_id=None, limit=50):
    if type(limit) is not int or not 1 <= limit <= 100 or (before_id is not None and (type(before_id) is not int or before_id <= 0)):
        raise ValueError('Choose 1 to 100 occurrences and a positive before_id cursor.')
    rows = db.execute('SELECT * FROM occurrences WHERE revision_id=? AND (? IS NULL OR id<?) ORDER BY id DESC LIMIT ?',
                      (event, before_id, before_id, limit+1)).fetchall()
    return {'items': [decode(row) for row in rows[:limit]],
            'next_before_id': rows[limit-1]['id'] if len(rows) > limit else None,
            'order': 'recording_sequence_descending'}


def display_time(value):
    if not value:
        return 'Unknown'
    from datetime import datetime
    return datetime.fromisoformat(value).astimezone().strftime('%Y-%m-%d %H:%M:%S %z')


def describe(item):
    """Plain text for evidence details, never HTML interpreted from a source."""
    if not item:
        return 'Origin details unavailable.'
    lines = [f"Observed: {display_time(item['observed_at'])}",
             f"Recorded: {display_time(item['recorded_at'])}",
             f"App: {item.get('app') or 'Unknown'} · Window/document: {item.get('title') or 'Unknown'}",
             f"URL: {item.get('url') or 'Unavailable'}" + (' (query and fragment omitted)' if item.get('url') else ''),
             f"Action: {item.get('action', 'unknown').replace('_', ' ')} · Trigger: {(item.get('capture_trigger') or 'Not recorded').replace('_', ' ')}"]
    if item.get('capture_finished_at'):
        lines.append('Capture completed: ' + display_time(item['capture_finished_at']))
    if item.get('locator'):
        lines.append('Location: ' + item['locator'])
    if item.get('observation_basis') == 'save_time':
        lines.append('Observation time is the save time; original authoring/speech time is unknown.')
    if item.get('capture_method'):
        lines.append('Method: ' + item['capture_method'] + ' · Coverage: ' + (item.get('completeness') or 'Unknown'))
    if item.get('focused_role'):
        lines.append('Focused control: ' + item['focused_role'] + ' · ' + item.get('focused_name', ''))
    if item.get('metadata_status') == 'legacy_partial':
        lines.append('Historical record: missing origin details were not reconstructed.')
    return '\n'.join(lines)
