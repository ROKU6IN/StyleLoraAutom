"""Local diagnostics and explicit network probes; never Runtime/recovery."""
import argparse
from contextlib import contextmanager, ExitStack
import os
from pathlib import Path
import sqlite3
import stat
import sys
import json

from .. import __version__
from ..llm.secrets import safe_path, dpapi
from ..project_db.validation import utc_now
from ..services.verified_file import pinned
from .errors import ServiceError
from .paths import locations


def result(code='doctor:ok', exit_code=0, **details):
    return {'status': 'error' if exit_code == 5 else 'warning' if exit_code else 'ok',
            'code': code, 'exit_code': exit_code, 'details': details}


def canonical_path(path):
    # Resolve Windows package filesystem virtualization/short names only after
    # rejecting reparse links, then validate the resulting path as well.
    return safe_path(safe_path(path).resolve())


@contextmanager
def bounded(path, limit, *, optional=False):
    """Pin existing regular files; never create missing files or follow links."""
    path = canonical_path(path)
    with ExitStack() as stack:
        try:
            fd = stack.enter_context(pinned(path))
        except FileNotFoundError:
            if not optional:
                raise
            fd = None
        if fd is not None:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise ServiceError(422, 'doctor:invalid_file')
        yield fd


def directory_access(path):
    path = safe_path(path)
    existing = path
    while not existing.exists():
        if existing.parent == existing:
            raise ServiceError(503, 'doctor:directory_unavailable')
        existing = existing.parent
    if not existing.is_dir():
        raise ServiceError(422, 'doctor:not_directory')
    # Request directory add-file/add-subdirectory access without creating a probe
    # file. This checks the current token's permissions, not disk capacity or a
    # promise that a later atomic replace of every child file will succeed.
    if os.name == 'nt':
        from .instance import _windows
        import ctypes
        kernel, _ = _windows()
        handle = kernel.CreateFileW(str(existing), 0x6, 7, None, 3, 0x02200000, None)
        if handle in (None, ctypes.c_void_p(-1).value):
            raise ServiceError(503, 'doctor:directory_access_denied')
        kernel.CloseHandle(handle)
        method = 'windows_directory_access'
    else:
        if not os.access(existing, os.W_OK | os.X_OK):
            raise ServiceError(503, 'doctor:directory_access_denied')
        method = 'os_access_advisory'
    return result(exists=path.exists(), checked_existing_ancestor=existing != path,
                  method=method, write_performed=False)


def instance_status():
    from .instance import ProductLock
    if os.name != 'nt':
        return result('doctor:windows_required', 6)
    try:
        owner = ProductLock()
    except ServiceError as exc:
        if exc.code == 'instance:already_running':
            return result('instance:running', active=True, attached=False)
        raise
    try:
        return result('instance:idle', active=False, abandoned=owner.abandoned)
    finally:
        owner.close()


def sqlite_capability():
    if sqlite3.sqlite_version_info < (3, 38, 0):
        return result('doctor:sqlite_too_old', 5, version=sqlite3.sqlite_version)
    db = sqlite3.connect(':memory:')
    try:
        if db.execute("SELECT json_extract('{\"ok\":1}', '$.ok')").fetchone()[0] != 1:
            raise ValueError()
        db.execute('CREATE TABLE probe(value INTEGER) STRICT')
    finally:
        db.close()
    return result(version=sqlite3.sqlite_version, json_functions=True, strict_tables=True,
                  database='memory', project_database_opened=False)


def dpapi_capability():
    payload = os.urandom(32)
    if dpapi(dpapi(payload), decrypt=True) != payload:
        raise ServiceError(503, 'doctor:dpapi_unavailable')
    return result(scope='CurrentUser', persisted=False, stored_secrets_decrypted=False)


def config_checks(paths):
    from ..services.product_config import ProductConfig
    from ..llm.config import ConfigStore
    from ..llm.secrets import SecretStore
    from ..vision.policy import PolicyStore
    from ..generation.presets import PresetStore, MAX_BYTES as PRESET_BYTES
    from ..services.recent_projects import RecentProjects, MAX_BYTES as RECENT_BYTES
    from ..tagging.library import CatalogLibrary
    definitions = [
        ('config.product', paths.config/'product-config.json', 16384, lambda: ProductConfig(None, paths.config).get()),
        ('config.llm', paths.config/'llm.json', 4*1024**2, lambda: ConfigStore(None, paths.config)),
        ('config.secrets', paths.private/'secrets.json', 4*1024**2, lambda: SecretStore(paths.private)),
        ('config.vision', paths.config/'vision-policy'/'current.json', 4*1024**2, lambda: PolicyStore(None, paths.config)),
        ('config.presets', paths.config/'generation_presets.json', PRESET_BYTES, lambda: PresetStore(None, paths.config)._load()),
        ('config.recent', paths.config/'known-projects.json', RECENT_BYTES, lambda: RecentProjects(None, paths.config)._load()),
        ('config.catalog', paths.product/'tagging-library.json', 32*1024**2, lambda: CatalogLibrary(None, paths.product)),
    ]
    for identifier, path, limit, validate in definitions:
        def check(path=path, limit=limit, validate=validate):
            with bounded(path, limit, optional=True) as fd:
                validate()  # Read-only loaders; do not return user content.
                return result('config:valid' if fd is not None else 'config:defaults', present=fd is not None)
        yield identifier, check


def settings_recovery(paths):
    from ..generation.settings import SettingsJournal, MAX_BYTES
    journal = SettingsJournal(paths.private/'recovery'/'launcher')
    with bounded(journal.path, MAX_BYTES, optional=True):
        state = journal.load()
    record = state['record']
    return result('profile:recovery_required' if record else 'profile:idle', 7 if record else 0,
                  phase=record['phase'] if record else 'idle', blocked=record is not None,
                  external_state_checked=False)


def model_metadata(paths, manifest):
    from .contracts import strict_json
    root = canonical_path(paths.models/manifest.model_id/manifest.revision)
    details = {'manifest_id': manifest.digest, 'content_verified': False, 'inference_run': False}
    if not root.exists():
        return result('tagger:not_installed', **details)
    with bounded(root/'ready.json', 32768) as fd:
        marker = strict_json(os.read(fd, 32769))
        if marker != {'schema_version': 1, 'manifest': manifest.model_dump(mode='json')}:
            raise ServiceError(503, 'tagger:model_invalid')
    for file in manifest.files:
        with bounded(root/file.name, file.size) as fd:
            if os.fstat(fd).st_size != file.size:
                raise ServiceError(503, 'tagger:checksum_mismatch')
    return result('tagger:metadata_valid', **details)


def project_check(project, *, verify_content=False, content_timeout=120):
    from ..services.recent_projects import read_lock
    from ..project_db.storage import read_metadata
    from ..project_db.migrations import SUPPORTED_VERSION
    root = canonical_path(project)
    if root.suffix.lower() != '.slproj' or not root.is_dir():
        raise ServiceError(422, 'doctor:invalid_project')
    with pinned(root, directory=True), bounded(root/'.project.lock', 65536), read_lock(root), bounded(root/'project.json', 65536):
        metadata = read_metadata(root)
        if metadata.format_version != SUPPORTED_VERSION:
            return result('project:unsupported_version', 7, format_version=metadata.format_version)
        with bounded(root/'project.db', 2**63-1) as fd:
            header = os.read(fd, 100)
        if len(header) != 100 or header[:16] != b'SQLite format 3\0':
            raise ServiceError(422, 'doctor:invalid_database_header')
        # The header's user_version can lag committed WAL; do not treat it as a
        # current schema check or attach SQLite (which may create WAL/SHM files).
        indicators = []
        for name in ('.migration-state.json', 'project.db-journal'):
            if safe_path(root/name).exists():
                indicators.append(name)
        pending = safe_path(root/'images'/'.pending')
        if pending.exists():
            with os.scandir(pending) as entries:
                if next(entries, None) is not None:
                    indicators.append('images/.pending')
        details = dict(format_version=metadata.format_version, database_check='header_only', integrity_checked=False,
                       recovery_indicators=indicators, wal_present=safe_path(root/'project.db-wal').exists(), opened=False)
        if verify_content:
            from .doctor_content import database_content
            with bounded(root/'project.db', 2**63-1) as fd:
                return database_content(root, fd, metadata, details, timeout=content_timeout)
        return result('project:recovery_indicators' if indicators else 'project:container_readable',
                      7 if indicators else 0, **details)


def diagnose(*, project=None, runtime_dir=None, product_dir=None, verify_content=False, content_timeout=120,
             probe_network=False, network_timeout=30):
    checks = []
    def check(identifier, function):
        try:
            value = function()
        except ServiceError as exc:
            code = exc.code
            if identifier == 'project' and code == 'image:file_busy':
                code = 'project:locked'
            value = result(code, 4 if code == 'project:locked' else 5)
        except Exception:
            # No raw exception, URL, path, filename, configuration or secret text.
            value = result('doctor:check_failed', 5)
        checks.append({'id': identifier, **value})
    check('sqlite', sqlite_capability)
    check('instance', instance_status)
    check('dpapi', dpapi_capability)
    paths = None
    try:
        paths = locations(runtime_dir, product_dir)
    except ServiceError:
        check('locations', lambda: result('config:locations_unavailable', 5))
    else:
        for name in ('runtime', 'product', 'config', 'private', 'models'):
            check('directory.'+name, lambda name=name: directory_access(getattr(paths, name)))
        for identifier, function in config_checks(paths):
            check(identifier, function)
        check('recovery.launcher', lambda: settings_recovery(paths))
        from ..tagging.manifests import REVIEWED_MANIFESTS
        for index, manifest in enumerate(REVIEWED_MANIFESTS):
            if verify_content:
                from .doctor_content import model_content
                check('model.'+str(index+1), lambda manifest=manifest: model_content(paths, manifest, timeout=content_timeout))
            else:
                check('model.'+str(index+1), lambda manifest=manifest: model_metadata(paths, manifest))
    if project is not None:
        check('project', lambda: project_check(project, verify_content=verify_content, content_timeout=content_timeout))
        check('directory.project', lambda: directory_access(project))
    if probe_network:
        from .doctor_network import diagnose_network, entry
        if paths is None:
            checks.append(entry('network', 'doctor:network_config_unavailable', 5, attempted=False))
        else:
            try:
                checks.extend(diagnose_network(paths, timeout=network_timeout))
            except Exception:
                # Unexpected runner failure may happen after sending a read.
                checks.append(entry('network', 'doctor:network_failed', 5, attempted=True, request_state='unknown'))
    code = next((code for code in (5, 4, 7, 6) if any(c['exit_code'] == code for c in checks)), 0)
    return {'schema_version': 1, 'version': __version__, 'as_of': utc_now(),
            'scope': 'local_and_network' if probe_network else 'offline_content' if verify_content else 'offline_metadata',
            'status': 'failed' if code == 5 else 'attention' if code in (4, 7) else 'partial' if code else 'ok',
            'exit_code': code, 'network_requested': probe_network,
            'network_probed': any(c['id'].startswith('network') and c['details'].get('attempted') for c in checks),
            'content_verification_requested': verify_content, 'repairs_performed': False, 'checks': checks}


def main(argv=None):
    parser = argparse.ArgumentParser(prog='stylelora doctor', description='Read-only diagnostics; network probes require --probe-network; no recovery or inference')
    parser.add_argument('--project', type=Path, help='Inspect an existing container without opening or recovering it')
    parser.add_argument('--runtime-dir', type=Path, help='Same directory selection as serve')
    parser.add_argument('--product-dir', type=Path, help='Same directory selection as serve')
    parser.add_argument('--json', action='store_true', help='Print one JSON result (also the default)')
    parser.add_argument('--verify-content', action='store_true', help='Hash installed reviewed model files and check idle project SQLite; never repair or run inference')
    parser.add_argument('--content-timeout', type=int, help='Cooperative deadline per content check (5-600 seconds, default 120); requires --verify-content')
    parser.add_argument('--probe-network', action='store_true', help='Explicitly send configured credentials to LLM models / NovelAI subscription GET endpoints; no inference or retries')
    parser.add_argument('--network-timeout', type=int, help='Total network phase deadline (5-120 seconds, default 30); requires --probe-network')
    args = parser.parse_args(argv)
    if args.content_timeout is not None and (not args.verify_content or not 5 <= args.content_timeout <= 600):
        parser.error('--content-timeout requires --verify-content and 5-600 integer seconds')
    if args.network_timeout is not None and (not args.probe_network or not 5 <= args.network_timeout <= 120):
        parser.error('--network-timeout requires --probe-network and 5-120 integer seconds')
    try:
        report = diagnose(project=args.project, runtime_dir=args.runtime_dir, product_dir=args.product_dir,
                          verify_content=args.verify_content, content_timeout=args.content_timeout or 120,
                          probe_network=args.probe_network, network_timeout=args.network_timeout or 30)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return report['exit_code']
    except KeyboardInterrupt:
        print('Diagnostics interrupted; no repair was performed.', file=sys.stderr)
        return 130
