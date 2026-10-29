"""Explicit inspection and disposition of interrupted export directories.

Publishing requires a complete report and every recorded byte. Otherwise the
only write offered is an audited, reversible-by-path archive; nothing is deleted.
"""
from collections import Counter
import hashlib
import os
from pathlib import PurePosixPath
from uuid import uuid4

from pydantic import field_validator
from typing import Literal

from ..api.contracts import StrictModel, Identifier, Digest
from ..exchange.common import project_operation
from ..project_db.connection import read_snapshot
from ..project_db.storage import atomic_json
from ..project_db.validation import digest, relative_path, utc_now
from ..runtime.contracts import strict_json, Versions, uuid_string
from ..runtime.errors import ServiceError
from .storage_files import identity, inventory, verify
from .verified_file import pinned

LIMIT = 32 * 1024 * 1024


class RecoveryAction(StrictModel):
    preview_id: Identifier
    digest: Digest
    candidate_id: Digest
    action: Literal['publish', 'archive']
    confirm: Literal[True]

    @field_validator('confirm', mode='before')
    @classmethod
    def confirmed(cls, value):
        if value is not True:
            raise ValueError('Explicit confirmation required')
        return value


def json_file(path):
    with path.open('rb') as stream:
        raw = stream.read(LIMIT+1)
    if len(raw) > LIMIT:
        raise ServiceError(413, 'export:recovery_limit')
    return strict_json(raw)


def contents(context, rel, canceled):
    root = context.handle.root
    files, errors, directories = inventory(root, rel, canceled=canceled)
    if errors:
        raise ServiceError(409, 'export:recovery_unsafe')
    total = 0
    for row in files:
        total += row['bytes']
        if total > 20 * 1024**3 or row['bytes'] > 100 * 1024**2:
            raise ServiceError(413, 'export:recovery_limit')
        path = verify(root, row, directories)
        with pinned(path) as fd:
            if identity(os.fstat(fd)) != row['identity']:
                raise ServiceError(409, 'export:recovery_stale')
            hashed = hashlib.sha256()
            while chunk := os.read(fd, 1024 * 1024):
                if canceled():
                    raise ServiceError(409, 'operation:canceled')
                hashed.update(chunk)
            if identity(os.fstat(fd)) != row['identity']:
                raise ServiceError(409, 'export:recovery_stale')
            verify(root, row, directories)
            row['sha256'] = hashed.hexdigest()
    return {'files': files, 'directories': directories}


def publication(context, rel, manifest):
    """Return the exact originally intended directory, or a blocking reason."""
    try:
        root = context.handle.root
        state = json_file(relative_path(root, rel+'/.export-state.json'))
        report = json_file(relative_path(root, rel+'/export_report.json'))
        target = report['directory']
        parts = PurePosixPath(target).parts
        if (len(parts) != 3 or parts[:2] != PurePosixPath(rel).parts[:2] or parts[2].startswith('.')
                or target != PurePosixPath(target).as_posix() or '\\' in target
                or relative_path(root, target).exists() or state['directory'] != target
                or report['status'] != 'complete' or type(report['schema_version']) is not int or report['schema_version'] != 1
                or state.get('schema_version') != 1 or state.get('image_mode') != report['output_config']['image_mode']
                or state['snapshot_sha256'] != report['snapshot_sha256']
                or uuid_string(state['export_id']) != uuid_string(report['export_id'])
                or report['project']['created_at'] != context.handle.metadata.created_at):
            raise ValueError()
        rows = report['images']
        if not isinstance(rows, list) or len(rows) > 50000:
            raise ValueError()
        counts = Counter(row['status'] for row in rows)
        if any(type(v) is not int for v in report['counts'].values()) or set(counts) - {'exported', 'skipped', 'failed'} or report['counts'] != {
                'selected': len(rows), **{k: counts[k] for k in ('exported', 'skipped', 'failed')}}:
            raise ValueError()
        actual = {row['path'][len(rel)+1:]: row for row in manifest['files']}
        expected = {'.export-state.json', 'export_report.json'}
        ids = set()
        with read_snapshot(context.handle) as db:
            for row in rows:
                if type(row['image_id']) is not int or row['image_id'] in ids:
                    raise ValueError()
                ids.add(row['image_id'])
                image = db.execute('SELECT source_id,filename,sha256,file_size FROM images WHERE image_id=?', (row['image_id'],)).fetchone()
                if image is None or image['source_id'] != row['source_id'] or image['filename'] != row['filename']:
                    raise ValueError()
                if row['status'] != 'exported':
                    if row['files']:
                        raise ValueError()
                    continue
                names = {row['output_path'], str(PurePosixPath(row['output_path']).with_suffix('.txt'))}
                if report['output_config']['include_json']:
                    names.add(str(PurePosixPath(row['output_path']).with_suffix('.json')))
                if len(row['files']) != len(names) or {f['path'] for f in row['files']} != names:
                    raise ValueError()
                for item in row['files']:
                    name = item['path']
                    if name in expected or name not in actual:
                        raise ValueError()
                    expected.add(name)
                    if actual[name]['sha256'] != item['sha256'] or actual[name]['bytes'] != item['bytes']:
                        raise ValueError()
                    if name == row['output_path'] and (item['sha256'] != image['sha256'] or item['bytes'] != image['file_size']):
                        raise ValueError()
                    if actual[name]['links'] > 1:
                        return None, 'export:recovery_hardlink_requires_new_export', None
        if expected != set(actual):
            raise ValueError()
        if report['output_config']['image_mode'] == 'hardlink':
            # The source or a shared inode may change later. Publishing an old
            # hardlink acknowledgement in a fresh recovery action is forbidden.
            return None, 'export:recovery_hardlink_requires_new_export', None
        return target, None, report['export_id']
    except (OSError, KeyError, ValueError, TypeError, ServiceError):
        return None, 'export:recovery_incomplete_or_changed', None


class ExportRecovery:
    def __init__(self, runtime):
        self.runtime = runtime

    def scan(self, context):
        def work(task):
            with project_operation(context.handle):
                root = relative_path(context.handle.root, 'exports')
                candidates, private = [], {}
                for scheme in root.iterdir():
                    if not scheme.name.isdecimal():
                        continue
                    checked = relative_path(context.handle.root, scheme.relative_to(context.handle.root).as_posix())
                    if not checked.is_dir():
                        continue
                    for directory in checked.iterdir():
                        rel = directory.relative_to(context.handle.root).as_posix()
                        path = relative_path(context.handle.root, rel)
                        if not path.is_dir():
                            continue
                        # A formal directory with a readable complete report is
                        # already published; it belongs to ordinary export history.
                        if not directory.name.startswith('.partial-'):
                            try:
                                if json_file(relative_path(context.handle.root, rel+'/export_report.json')).get('status') == 'complete':
                                    continue
                            except (OSError, ServiceError, ValueError, AttributeError):
                                pass
                        if len(candidates) >= 100:
                            raise ServiceError(413, 'export:recovery_limit')
                        manifest = contents(context, rel, task.cancel.is_set)
                        target, reason, eid = publication(context, rel, manifest)
                        cid = digest([rel, manifest])
                        candidates.append({'candidate_id': cid, 'directory': rel, 'files': len(manifest['files']),
                            'bytes': sum(r['bytes'] for r in manifest['files']), 'can_publish': target is not None,
                            'publish_directory': target, 'reason': reason, 'can_archive': True})
                        private[cid] = {'relative': rel, 'manifest': manifest, 'target': target, 'export_id': eid}
                decisions = self.history(context)
                return self.runtime.previews.add(context.project_id, 'export-recovery',
                    {'candidates': candidates, 'decisions': decisions}, private)
        return self.runtime.supervisor.submit('export.recovery_scan', work, context=context)

    def history(self, context):
        root = relative_path(context.handle.root, 'exports/.recovery')
        if not root.exists():
            return []
        result = []
        for item in root.iterdir():
            if len(result) >= 1000:
                raise ServiceError(413, 'export:recovery_limit')
            if item.suffix != '.json':
                continue
            path = relative_path(context.handle.root, item.relative_to(context.handle.root).as_posix())
            try:
                value = json_file(path)
                content = value['content']
                if (digest(content) != value['digest'] or content['schema_version'] != 1
                        or content['action'] not in ('publish', 'archive')
                        or uuid_string(content['decision_id']) != item.stem
                        or not isinstance(content['created_at'], str)
                        or not isinstance(content['directory_identity'], list)
                        or len(content['directory_identity']) != 2
                        or any(type(i) is not int for i in content['directory_identity'])):
                    raise ValueError()
                source, target = (relative_path(context.handle.root, content[k]) for k in ('source', 'target'))
            except (OSError, ValueError, TypeError, KeyError, ServiceError):
                raise ServiceError(409, 'export:recovery_record_corrupt') from None
            observed = 'moved' if not source.exists() and target.is_dir() and identity(target.stat())[:2] == content['directory_identity'] else 'unresolved'
            result.append({k: content[k] for k in ('decision_id', 'action', 'source', 'target', 'created_at')} | {'observed': observed})
        return result

    def act(self, context, body, expected):
        preview, private = self.runtime.previews.get(context.project_id, 'export-recovery', body['preview_id'], body['digest'])
        Versions.require(expected, preview['version'])
        selected = private.get(body['candidate_id'])
        if selected is None or body['action'] == 'publish' and not selected['target']:
            raise ServiceError(409, 'export:recovery_not_publishable')
        decision_id = str(uuid4())
        def work(task):
            root = context.handle.root
            with project_operation(context.handle):
                current = contents(context, selected['relative'], task.cancel.is_set)
                if current != selected['manifest']:
                    raise ServiceError(409, 'export:recovery_stale')
                source = relative_path(root, selected['relative'])
                target_rel = selected['target'] if body['action'] == 'publish' else 'exports/.archived/'+decision_id
                target = relative_path(root, target_rel)
                if target.exists():
                    raise ServiceError(409, 'export:destination_exists')
                if body['action'] == 'publish' and publication(context, selected['relative'], current)[0] != target_rel:
                    raise ServiceError(409, 'export:recovery_stale')
                record = {'schema_version': 1, 'decision_id': decision_id, 'action': body['action'],
                    'source': selected['relative'], 'target': target_rel, 'directory_identity': identity(source.stat())[:2],
                    'manifest_digest': digest(current), 'created_at': utc_now()}
                journal = relative_path(root, 'exports/.recovery/'+decision_id+'.json')
                journal.parent.mkdir(exist_ok=True)
                target.parent.mkdir(exist_ok=True)
                if task.cancel.is_set():
                    raise ServiceError(409, 'operation:canceled')
                atomic_json(journal, {'content': record, 'digest': digest(record)})
                # Both absolute targets are validated within the project before
                # the single same-volume rename. No recursive delete or overwrite.
                relative_path(root, selected['relative']); relative_path(root, target_rel)
                if identity(source.stat())[:2] != record['directory_identity']:
                    raise ServiceError(409, 'export:recovery_stale')
                try:
                    source.rename(target)
                except OSError:
                    if source.exists() or not target.is_dir() or identity(target.stat())[:2] != record['directory_identity']:
                        raise ServiceError(503, 'export:recovery_unknown', outcome='unknown') from None
                with self.runtime.dataset.exchange.lock:
                    self.runtime.dataset.exchange.exports = {k: v for k, v in self.runtime.dataset.exchange.exports.items()
                        if not (k[0] == context.project_id and (v.get('relative_dir') == selected['relative'] or k[1] == selected['export_id']))}
                self.runtime.emit('export.finished', {'export_id': selected['export_id'], 'recovery_action': body['action']}, project_id=context.project_id)
                return {'decision_id': decision_id, 'action': body['action'], 'directory': target_rel,
                        'status': 'published' if body['action'] == 'publish' else 'archived', 'deleted_files': 0}
        return self.runtime.supervisor.submit('export.recovery_action', work, context=context)
