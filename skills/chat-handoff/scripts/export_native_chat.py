"""Read native Harness/ZCode transcripts; never modify native data or old exports."""
from archive_layout import ARCHIVE, BASE, REGISTRY, configured_zone, relative_from_archive, find_root
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

from export_chat import generate_markdown
from output_guard import fresh_output


def timestamp(value):
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat()
    if isinstance(value, str):
        datetime.fromisoformat(value.replace('Z', '+00:00'))
        return value
    raise ValueError('Missing or invalid message timestamp')


def add_model(models, provider, model):
    if isinstance(model, str) and model:
        name = f'{provider}/{model}' if provider else model
        if name not in models:
            models.append(name)


def zstd_jsonl(path, limit=512 * 1024 * 1024):
    import zstandard
    # The ordinary one-shot decoder stops at the first frame. Check every frame
    # and its EOF, so a torn final frame cannot become a successful partial export.
    pending = b''
    total = 0
    decoder = None
    frames = 0
    line_number = 0
    with Path(path).open('rb') as source:
        while chunk := source.read(64 * 1024):
            while chunk:
                if decoder is None:
                    decoder = zstandard.ZstdDecompressor().decompressobj()
                    frames += 1
                try:
                    decoded = decoder.decompress(chunk)
                except zstandard.ZstdError as exc:
                    raise ValueError('Invalid Zstandard frame; no partial export written') from exc
                total += len(decoded)
                if total > limit:
                    raise ValueError('Decoded transcript exceeds safety limit')
                pending += decoded
                while b'\n' in pending:
                    line, pending = pending.split(b'\n', 1)
                    line_number += 1
                    if not line.strip():
                        raise ValueError(f'Blank JSONL record at line {line_number}')
                    obj = json.loads(line.decode('utf-8'))
                    if not isinstance(obj, dict):
                        raise ValueError(f'Non-object record at line {line_number}')
                    yield obj
                if decoder.eof:
                    chunk = decoder.unused_data
                    decoder = None
                else:
                    chunk = b''
    if not frames or decoder is not None:
        raise ValueError('Empty or truncated Zstandard transcript')
    if pending:
        obj = json.loads(pending.decode('utf-8'))
        if not isinstance(obj, dict):
            raise ValueError('Non-object final record')
        yield obj


INTERNAL_SOURCES = {
    'agent-instructions', 'skill-catalog', 'runtime-context', 'user-approval',
    'tool-jobs', 'compact-checkpoint', 'subagent-settled', 'subagent-report',
    'model-selection', 'plugin:dsh-agent-teams',
    'coordinator', 'agent-message', 'goal', 'skill-invocation', 'repeat-tool-reminder',
    'plugin:@yuxianglin/dsh-bridge-browser', 'time-context',
}
INTERNAL_PLUGINS = {
    'user-approval', '@deepseek-ai/dsh-system-prompt', 'tool-jobs',
    'repeat-tool-reminder', 'compact', 'dsh-agent-teams', 'tool-goal',
    '@yuxianglin/dsh-bridge-browser',
}


def human_source(source):
    if not isinstance(source, dict):
        raise ValueError('User record has no explicit source; cannot safely classify it')
    kind = source.get('kind')
    if kind in ('user', 'plugin:dsh-wechat'):
        return True
    if kind in INTERNAL_SOURCES:
        return False
    if kind == 'plugin':
        plugin = source.get('plugin')
        if plugin in ('dsh-wechat', 'dsh-wx-bridge'):
            return True
        if plugin in INTERNAL_PLUGINS:
            return False
    raise ValueError(f'Unclassified user source: {kind}; review native schema first')


def content_text(content, stats):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise ValueError('Invalid message content')
    texts = []
    for part in content:
        kind = part.get('type')
        if kind in ('text', 'input_text', 'output_text'):
            if not isinstance(part.get('text'), str):
                raise ValueError('Text part is not a string')
            texts.append(part['text'])
        elif kind in ('reasoning', 'tool-call', 'tool-result', 'tool_use', 'tool_result'):
            stats['excluded_' + kind] += 1
        elif kind in ('image', 'file', 'audio', 'video'):
            stats['attachment_parts'] += 1
            texts.append(f'[附件：{kind}；本导出仅保留文字，附件仍须另存到项目证据目录]')
        else:
            raise ValueError(f'Unsupported content part: {kind}')
    return '\n\n'.join(texts)


def parse_dsh(path, session_id):
    path = Path(path)
    before = path.stat()
    with path.open('rb') as source:
        before_hash = hashlib.file_digest(source, 'sha256').hexdigest()
    messages, models, stats = [], [], Counter()
    header = None
    last_seq = -1
    message_ids = set()
    for record in zstd_jsonl(path):
        stats['records'] += 1
        if header is None:
            header = record
            if record.get('type') != 'session' or record.get('version') not in (0, 4):
                raise ValueError('Unsupported Harness header/version')
            if record.get('id') != session_id:
                raise ValueError('Harness session ID mismatch')
            continue
        seq = record.get('seq')
        # Legacy packed chunk records represent several physical sequence slots.
        seq0 = record.get('seq0')
        if seq is not None:
            if not isinstance(seq, int) or seq <= last_seq:
                raise ValueError('Harness sequence is not strictly increasing')
            last_seq = seq
        elif seq0 is not None:
            if not isinstance(seq0, int) or seq0 <= last_seq:
                raise ValueError('Harness packed sequence is not strictly increasing')
            last_seq = seq0
        kind, data = record.get('type'), record.get('data', {})
        if kind == 'model/selection':
            add_model(models, data.get('provider'), data.get('model'))
        if kind not in ('user/message', 'assistant/message'):
            continue
        message = data.get('message', data)
        source = message.get('source') or {}
        if kind == 'user/message' and not human_source(source):
            stats['internal_user_messages'] += 1
            continue
        if kind == 'assistant/message':
            add_model(models, source.get('provider'), source.get('model'))
        identity = message.get('id')
        if identity and identity in message_ids:
            raise ValueError('Duplicate durable message ID; review surface operations')
        if identity:
            message_ids.add(identity)
        text = content_text(message.get('content', []), stats)
        if not text:
            continue
        messages.append({'role': 'User' if kind == 'user/message' else 'Assistant (DSH)',
                         'text': text, 'time': timestamp(record.get('time'))})
    with path.open('rb') as source:
        after_hash = hashlib.file_digest(source, 'sha256').hexdigest()
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before_hash) != (after.st_size, after.st_mtime_ns, after_hash):
        raise ValueError('Native transcript changed during read; wait until idle and retry')
    stats['version'] = header['version']
    stats['source_sha256'] = after_hash
    return messages, models, dict(stats)


def parse_zcode(path, session_id):
    path = Path(path).resolve(strict=True)
    messages, models, stats = [], [], Counter()
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    try:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        if db.execute('SELECT id FROM session WHERE id=?', (session_id,)).fetchone() is None:
            raise ValueError('ZCode session ID not found')
        rows = db.execute('SELECT id,time_created,data FROM message WHERE session_id=? '
                          'ORDER BY time_created,rowid', (session_id,)).fetchall()
        for message_id, created, raw in rows:
            data = json.loads(raw)
            stats['native_messages'] += 1
            semantics = data.get('semantics') or {}
            if semantics.get('uiVisibility') == 'hidden' or semantics.get('transcriptVisibility') == 'hidden':
                stats['hidden_messages'] += 1
                continue
            role = data.get('role')
            if role not in ('user', 'assistant'):
                stats['other_roles'] += 1
                continue
            add_model(models, data.get('providerId'), data.get('modelId'))
            parts = []
            for (raw_part,) in db.execute('SELECT data FROM part WHERE message_id=? '
                                         'ORDER BY sequence,time_created,rowid', (message_id,)):
                part = json.loads(raw_part)
                if part.get('synthetic') or part.get('ignored'):
                    stats['hidden_parts'] += 1
                    continue
                kind = part.get('type')
                if kind in ('text', 'file'):
                    parts.append(part)
                elif kind in ('reasoning', 'tool', 'step-start', 'step-finish', 'snapshot',
                              'patch', 'agent', 'retry', 'compaction', 'subtask', 'timeline'):
                    stats['excluded_' + kind] += 1
                else:
                    raise ValueError(f'Unsupported ZCode part: {kind}')
            text = content_text(parts, stats)
            if text:
                messages.append({'role': 'User' if role == 'user' else 'Assistant (ZCode)',
                                 'text': text, 'time': timestamp(created)})
        db.rollback()
    finally:
        db.close()
    return messages, models, dict(stats)


def select_messages(messages, markers=(), from_text=None):
    if not messages:
        raise ValueError('No visible messages')
    for marker in markers:
        if not any(marker in m['text'] for m in messages):
            raise ValueError('Marker not found in visible transcript')
    if from_text is not None:
        matches = [i for i, m in enumerate(messages) if from_text in m['text']]
        if len(matches) != 1:
            raise ValueError('Start text must match exactly one visible message')
        messages = messages[matches[0]:]
    return messages


def write_export(messages, source, models, output, topic=None):
    path = fresh_output(output)
    markdown = generate_markdown(messages, source, topic=topic, models=models)
    with path.open('x', encoding='utf-8', newline='\n') as handle:
        handle.write(markdown)
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--agent', choices=('dsh', 'zcode'), required=True)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--session-id', required=True)
    parser.add_argument('--marker', action='append', default=[])
    parser.add_argument('--from-text')
    parser.add_argument('--topic')
    parser.add_argument('--validate', action='store_true', help='Metadata only; no output file')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    try:
        if args.validate and args.output:
            raise ValueError('--validate must not be combined with --output')
        messages, models, stats = (parse_dsh if args.agent == 'dsh' else parse_zcode)(args.source, args.session_id)
        messages = select_messages(messages, args.marker, args.from_text)
        result = {'agent': args.agent, 'session_id': args.session_id, 'messages': len(messages),
                  'user_messages': sum(m['role'] == 'User' for m in messages),
                  'models': models, 'stats': stats,
                  'warnings': ['附件未复制；另行保存所需原始证据'] if stats.get('attachment_parts') else []}
        if not args.validate:
            if not args.output or len(args.marker) < 2:
                raise ValueError('Export requires --output and at least two current-chat markers')
            result['output'] = str(write_export(messages, args.source, models, args.output, args.topic))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, sqlite3.Error, ImportError) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
