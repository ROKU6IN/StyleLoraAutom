"""Local copy importer with frozen manifests and F01 item/attempt recovery."""

from __future__ import annotations

import os
import json
from dataclasses import asdict, fields
from pathlib import Path
from uuid import uuid4

from ..project_db import commands
from ..project_db.artifacts import cleanup_receipt, prepare_artifact, receipt_path
from ..project_db.connection import read_snapshot, submit_write
from ..project_db.errors import CommitUnknown, PreviewStale, StateConflict, StorageError, UnsupportedScheme, ValidationError
from ..project_db.recovery import inspect_sent, reconcile_item
from ..project_db.repositories import images, schemes, sources
from ..project_db.repositories.common import row_dto
from ..project_db.types import ItemSeed, RecoveryEvidence, SourceDraft, SourceItemRow
from ..project_db.validation import EXTENSIONS, digest, file_hash, integer, relative_path
from .common import copy_verified, input_root, io_reason, is_link, notify_progress, project_operation, read_bounded, remove_owned
from .errors import ExchangeError
from .imaging import inspect_image
from ..imaging.phash import compute_phash, PhashError
from .sidecar import parse_sidecar
from .types import ImportItemResult, ImportManifest, ImportOptions, ImportReport, ManifestEntry


def validate_options(options):
    if options.encoding not in ("utf-8-sig", "utf-8", "gbk", "auto"):
        raise ExchangeError("encoding:unsupported")
    if options.parser not in ("literal-tags-v1", "catalog-comma-v1"):
        raise ExchangeError("sidecar:unsupported_parser")
    if type(options.recursive) is not bool:
        raise ValidationError("recursive must be boolean")
    for name in ("max_image_bytes", "max_pixels", "max_sidecar_bytes", "max_files"):
        integer(getattr(options, name), name, 1)


def scheme_options(scheme):
    params = scheme.params_json
    if 'tokenizer_version' in params:
        from .schemes import ImportParams
        value = ImportParams.model_validate(params)
        options = ImportOptions(encoding=value.encoding,recursive=value.recursive,
                                parser='catalog-comma-v1' if value.tokenizer_version=='comma-v1' else 'literal-tags-v1')
        validate_options(options)
        return options
    keys = {field.name for field in fields(ImportOptions)}
    if (scheme.reader != "txt_sidecar" or set(params) - keys - {"schema_version", "tag_separator", "paragraph_separator"}
            or params.get("tag_separator", ",") != "," or params.get("paragraph_separator", "\n\n") != "\n\n"):
        raise UnsupportedScheme("Only the explicit first-line comma/blank-line sidecar format is implemented")
    options = ImportOptions(**{key: params[key] for key in keys if key in params})
    validate_options(options)
    return options


def scan_import(project, root, *, options=ImportOptions()):
    validate_options(options)
    root = input_root(project, root)
    candidates, warnings = [], []
    def scan_error(exc):
        raise ExchangeError("input:scan_failed") from exc
    for directory, dirs, filenames in os.walk(root, followlinks=False, onerror=scan_error):
        parent = Path(directory)
        safe_dirs = []
        for name in dirs:
            child = parent / name
            if is_link(child):
                warnings.append("input:linked_directory_skipped")
            elif child.suffix.lower() != ".slproj":
                safe_dirs.append(name)
        dirs[:] = sorted(safe_dirs, key=lambda name: (name.casefold(), name)) if options.recursive else []
        sidecars, image_stems = {}, {}
        for name in filenames:
            path = parent / name
            if is_link(path):
                warnings.append("input:linked_file_skipped")
                continue
            if path.suffix.lower() == ".txt":
                sidecars.setdefault(path.stem.casefold(), []).append(path)
            elif path.suffix.lower() in EXTENSIONS:
                image_stems[path.stem.casefold()] = image_stems.get(path.stem.casefold(), 0) + 1
        for name in filenames:
            path = parent / name
            if path.suffix.lower() not in EXTENSIONS or is_link(path):
                continue
            relative = path.relative_to(root).as_posix()
            path = relative_path(root, relative)
            matches = sidecars.get(path.stem.casefold(), [])
            error = "sidecar:ambiguous" if len(matches) > 1 else None
            sidecar = matches[0] if len(matches) == 1 else None
            notes = []
            if not matches:
                notes.append("sidecar:missing")
            if image_stems[path.stem.casefold()] > 1 and sidecar:
                notes.append("sidecar:shared_stem")
            size = path.stat().st_size
            image_digest = None
            if size > options.max_image_bytes:
                error = "image:too_large"
            else:
                image_digest = file_hash(path)
            sidecar_digest = None
            if sidecar:
                sidecar = relative_path(root, sidecar.relative_to(root).as_posix())
                if sidecar.stat().st_size > options.max_sidecar_bytes:
                    error = error or "sidecar:too_large"
                else:
                    sidecar_digest = file_hash(sidecar)
            candidates.append((relative, image_digest, size,
                               sidecar.relative_to(root).as_posix() if sidecar else None,
                               sidecar_digest, error, tuple(notes)))
            if len(candidates) > options.max_files:
                raise ExchangeError("input:file_limit")
    candidates.sort(key=lambda item: (item[0].casefold(), item[0]))
    entries = tuple(ManifestEntry(index, *value) for index, value in enumerate(candidates, 1))
    return ImportManifest(entries, tuple(sorted(set(warnings))))


def _load_options(source):
    config = source.config_json
    if config.get("importer") != "local-copy-v1" or config.get("manifest_version") != 1:
        raise ExchangeError("import:unsupported_snapshot")
    keys = {field.name for field in fields(ImportOptions)}
    try:
        options = ImportOptions(**{key: config["params"][key] for key in keys})
        entries = tuple(ManifestEntry(**entry) for entry in config["import_manifest"])
        validate_options(options)
        return options, entries
    except (KeyError, TypeError, ValueError) as exc:
        raise ExchangeError("import:invalid_snapshot") from exc


class SidecarIndex:
    """Operation-local directory index, refreshed on directory identity/change.

    Content is never cached: each use still validates the path and SHA-256.
    Rechecking the directory stamp avoids a full directory walk for every image.
    """
    def __init__(self):
        self.directories = {}

    @staticmethod
    def stamp(parent):
        stat = parent.stat()
        return stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns

    def matches(self, path):
        parent = path.parent
        stamp = self.stamp(parent)
        cached = self.directories.get(parent)
        if cached is None or cached[0] != stamp:
            for _ in range(3):
                names = {}
                for child in parent.iterdir():
                    if child.suffix.lower() == '.txt':
                        names.setdefault(child.stem.casefold(), []).append(child)
                after = self.stamp(parent)
                if after == stamp:
                    cached = stamp, names
                    self.directories[parent] = cached
                    break
                stamp = after
            else:
                raise ExchangeError('input:sidecar_changed')
        return tuple(cached[1].get(path.stem.casefold(), ()))


def _read_inputs(root, entry, options, sidecars=None):
    path = relative_path(root, entry.path)
    if not path.is_file():
        raise ExchangeError("input:missing")
    if path.stat().st_size != entry.file_size:
        raise ExchangeError("input:changed")
    if entry.image_sha256 is not None and file_hash(path) != entry.image_sha256:
        raise ExchangeError("input:changed")
    # Discover matching sidecars again, including newly added/removed files.
    matches = (sidecars or SidecarIndex()).matches(path)
    if entry.preflight_error == "sidecar:ambiguous":
        return path, None
    expected = relative_path(root, entry.sidecar_path) if entry.sidecar_path else None
    if (expected is None and matches) or (expected is not None and (len(matches) != 1 or matches[0] != expected)):
        raise ExchangeError("input:sidecar_changed")
    if expected is None:
        return path, None
    if entry.preflight_error == "sidecar:too_large":
        return path, None
    data = read_bounded(expected, options.max_sidecar_bytes)
    import hashlib
    if hashlib.sha256(data).hexdigest() != entry.sidecar_sha256:
        raise ExchangeError("input:sidecar_changed")
    return path, data


def _write(project, fn, *args, **kwargs):
    return submit_write(project, lambda db: fn(db, *args, **kwargs)).result()


def _source_and_items(project, source_id):
    with read_snapshot(project) as db:
        source = sources.get_source(db, source_id)
        items = tuple(row_dto(row, SourceItemRow) for row in db.execute(
            "SELECT * FROM source_items WHERE source_id=? ORDER BY seq", (source_id,),
        ))
        return source, items


def _pause(project, source_id, reason):
    def pause(db):
        source = sources.get_source(db, source_id)
        if source.status == "running":
            commands.transition_source(db, source_id, expected="running", target="paused", reason=reason)
    _write(project, pause)


def _report(project, source_id, entries, warnings=(), item_warnings=None):
    source, items = _source_and_items(project, source_id)
    item_warnings = item_warnings or {}
    by_seq = {entry.seq: entry for entry in entries}
    # Recover decoding evidence from committed originals, including after a
    # restart/receipt reconciliation; never re-read potentially changed inputs.
    with read_snapshot(project) as db:
        evidence = {row['item_id']: {
            'encoding': row['encoding'], 'encoding_fallback': bool(row['fallback']),
            'warnings': json.loads(row['warnings']) if row['warnings'] is not None else [],
        } for row in db.execute(
            "SELECT i.item_id, json_extract(a.params_json, '$.encoding') AS encoding, "
            "json_extract(a.params_json, '$.encoding_fallback') AS fallback, "
            "json_extract(a.params_json, '$.warnings') AS warnings "
            "FROM source_items i JOIN annotations a ON a.image_id=i.image_id "
            "WHERE i.source_id=? AND a.source='original' AND a.type='tags' AND a.is_current=1", (source_id,))}
    results = tuple(ImportItemResult(
        item.item_id, by_seq[item.seq].path, item.status, item.image_id, item.error,
        tuple(dict.fromkeys((*by_seq[item.seq].warnings, *item_warnings.get(item.item_id, ()),
                            *evidence.get(item.item_id, {}).get('warnings', ())))),
        evidence.get(item.item_id, {}).get('encoding'),
        evidence.get(item.item_id, {}).get('encoding_fallback', False),
    ) for item in items)
    counts = {state: sum(item.status == state for item in items) for state in ("done", "skipped", "failed", "pending", "sent")}
    return ImportReport(source_id, source.status, len(items), counts["done"], counts["skipped"], counts["failed"],
                        counts["pending"], counts["sent"], results, tuple(dict.fromkeys(warnings)))


def import_images(project, root, *, name="Local images", options=None, scheme_id=None,
                  source_id=None, retry_failed=False, catalog=None, cancelled=None, expected_manifest=None, progress=None, expected_scheme=None):
    """Import or explicitly resume a frozen source; original inputs are never modified."""
    if type(retry_failed) is not bool:
        raise ValidationError("retry_failed must be boolean")
    with project_operation(project):
        warnings, notes = [], {}
        sidecars = SidecarIndex()
        if source_id is None:
            with read_snapshot(project) as db:
                scheme = schemes.get_import_scheme(db, scheme_id) if scheme_id is not None else schemes.get_default_import_scheme(db)
            defaults = scheme_options(scheme)
            options = options or defaults
            validate_options(options)
            if options.parser == "catalog-comma-v1" and catalog is None:
                raise ExchangeError("catalog:required")
            root = input_root(project, root)
            manifest = scan_import(project, root, options=options)
            if expected_manifest is not None and digest(manifest) != expected_manifest:
                raise PreviewStale("Import inputs changed after preview")
            if not manifest.entries:
                raise ExchangeError("input:no_images")
            entries = manifest.entries
            warnings.extend(manifest.warnings)
            params = {"schema_version": 1, **asdict(options)}
            config = {
                "schema_version": 1, "reader": "txt_sidecar", "params": params,
                "importer": "local-copy-v1", "manifest_version": 1,
                "catalog_revision": catalog.revision if options.parser == "catalog-comma-v1" else None,
                "tokenizer_version": 'comma-v1' if options.parser == 'catalog-comma-v1' else 'literal-tags-v1',
                "scheme_snapshot": asdict(scheme),
                "import_manifest": [asdict(entry) for entry in entries],
            }
            from ..project_db.validation import encode
            if len(encode(config).encode("utf-8")) > 16 * 1024 * 1024:
                raise ExchangeError("input:manifest_limit")
            seeds = tuple(ItemSeed(entry.seq, Path(entry.path).name) for entry in entries)
            def create(db):
                if expected_scheme is not None and digest(schemes.get_import_scheme(db,scheme.scheme_id)) != expected_scheme:
                    raise PreviewStale('Import scheme changed after preview')
                source = sources.create_source(db, SourceDraft("import", name, config))
                return commands.start_source(db, source.source_id, seeds)
            source = _write(project, create)
            source_id = source.source_id
        else:
            source, items = _source_and_items(project, source_id)
            if source.type != "import" or source.status == "canceled":
                raise StateConflict("Only an active or completed import source can be resumed")
            frozen_options, entries = _load_options(source)
            if options is not None and options != frozen_options:
                raise StateConflict("Resume must use the frozen reader configuration")
            options = frozen_options
            if source.status == "done" and not retry_failed:
                return _report(project, source_id, entries)
            if options.parser == "catalog-comma-v1" and (
                catalog is None or catalog.revision != source.config_json["catalog_revision"]
            ):
                raise ExchangeError("catalog:snapshot_mismatch")
            root = input_root(project, root)
            # This adapter exclusively copies local files. With the OS project lock
            # and file-operation lease, a receipt-less old copy is no longer in flight.
            for item in items:
                if item.status == "sent":
                    evidence = inspect_sent(project, item)
                    if evidence.verdict == "complete":
                        _write(project, reconcile_item, evidence)
                    elif not receipt_path(project.root, item.item_id, item.attempts).exists():
                        _write(project, reconcile_item, RecoveryEvidence(
                            item.item_id, item.attempts, "absent", reason="import:interrupted_copy"))
                    else:
                        _pause(project, source_id, "import:unverified_receipt")
                        return _report(project, source_id, entries, ("import:unverified_receipt",))
            source, items = _source_and_items(project, source_id)
            for item in items:
                if item.status == "failed" and retry_failed:
                    try:
                        _read_inputs(root, entries[item.seq - 1], options, sidecars)
                    except (OSError, ExchangeError) as exc:
                        return _report(project, source_id, entries, (io_reason(exc),))
                    _write(project, commands.retry_failed_item, item.item_id, expected_attempt=item.attempts)
            source, items = _source_and_items(project, source_id)
            if source.status == "paused":
                _write(project, commands.transition_source, source_id, expected="paused", target="running", reason="import:resume")
            elif source.status == "done":
                return _report(project, source_id, entries)
        def emit(item_id=None):
            with read_snapshot(project) as db:
                state = sources.get_source_state(db, source_id)
                counts = sources.count_item_states(db, source_id)
                item = sources.get_item(db, item_id) if item_id is not None else None
            notify_progress(progress, {"source_id": source_id, "status": state.status, "counts": counts,
                            "item_id": item_id, "attempt": item.attempts if item else None,
                            "image_id": item.image_id if item else None, "outcome": item.status if item else None,
                            "reason_code": item.error if item else None})
        emit()
        project._fault("import_after_manifest")
        _, items = _source_and_items(project, source_id)
        try:
            for item in items:
                if item.status != "pending":
                    continue
                if cancelled and cancelled():
                    _pause(project, source_id, "import:canceled")
                    warnings.append("import:canceled")
                    break
                with read_snapshot(project) as db:
                    current = sources.get_source_state(db, source_id)
                if current.status != "running":
                    break
                entry = entries[item.seq - 1]
                try:
                    original_path, sidecar = _read_inputs(root, entry, options, sidecars)
                except (OSError, ExchangeError) as exc:
                    reason = io_reason(exc)
                    _pause(project, source_id, reason)
                    warnings.append(reason)
                    break
                if entry.image_sha256:
                    def skip_if_duplicate(db):
                        if images.find_by_sha256(db, entry.image_sha256):
                            return commands.skip_duplicate(db, item.item_id, sha256=entry.image_sha256)
                        return None
                    if _write(project, skip_if_duplicate) is not None:
                        emit(item.item_id)
                        continue
                sent = _write(project, commands.mark_sent, item.item_id).value
                project._fault("import_after_mark_sent")
                attempt_dir = relative_path(project.root, f"images/.pending/{sent.item_id}/{sent.attempts}")
                attempt_dir.mkdir(parents=True, exist_ok=True)
                relative = f"images/.pending/{sent.item_id}/{sent.attempts}/out{original_path.suffix.lower()}"
                staging = relative_path(project.root, relative)
                partial = relative_path(project.root, f"images/.pending/{sent.item_id}/{sent.attempts}/copy-{uuid4().hex}.part")
                owned = []
                try:
                    if entry.preflight_error:
                        raise ExchangeError(entry.preflight_error)
                    if staging.exists():
                        raise ExchangeError("import:staging_exists")
                    parsed = parse_sidecar(sidecar, options=options, catalog=catalog)
                    notes[item.item_id] = parsed.warnings
                    owned.append(partial)
                    copy_verified(original_path, partial, entry.image_sha256, entry.file_size, options.max_image_bytes)
                    partial.rename(staging)
                    owned = [staging]
                    project._fault("import_after_copy")
                    inspection = inspect_image(staging, options)
                    try:
                        phash = compute_phash(staging)
                    except PhashError:
                        phash = None
                        notes[item.item_id] = (*notes[item.item_id], 'phash:unavailable')
                    artifact = prepare_artifact(
                        project, item_id=sent.item_id, attempt=sent.attempts, relative_staging_path=relative,
                        width=inspection.width, height=inspection.height, png_meta=inspection.png_meta,
                        originals=parsed.originals, phash=phash,
                    )
                    project._fault("import_after_receipt")
                    def register(db):
                        # Recheck in the registration transaction, so a competing
                        # producer cannot create a second imported image.
                        if images.find_by_sha256(db, artifact.sha256):
                            commands.skip_duplicate(db, sent.item_id, sha256=artifact.sha256)
                            db.after_commit.append(lambda: cleanup_receipt(project.root, sent.item_id, sent.attempts))
                            return None
                        return commands.complete_item(db, artifact, parsed.originals)
                    registered = _write(project, register)
                    if registered is None:
                        remove_owned(attempt_dir, owned)
                    project._fault("import_after_register")
                except (ExchangeError, OSError, StorageError, CommitUnknown) as exc:
                    with read_snapshot(project) as db:
                        latest = sources.get_item(db, item.item_id)
                    if latest.status in ("done", "skipped"):
                        emit(item.item_id)
                        continue  # Commit outcome was confirmed by persisted identity.
                    if receipt_path(project.root, sent.item_id, sent.attempts).exists():
                        _pause(project, source_id, "import:recovery_required")
                        warnings.append("import:recovery_required")
                        break
                    reason = io_reason(exc)
                    if isinstance(exc, StorageError):
                        _pause(project, source_id, "import:database_error")
                        warnings.append("import:database_error")
                        break
                    _write(project, commands.fail_item, item.item_id, attempt=sent.attempts, error=reason)
                    remove_owned(attempt_dir, owned)
                    if reason in ("storage:full", "input:changed", "input:sidecar_changed"):
                        emit(item.item_id)
                        _pause(project, source_id, reason)
                        warnings.append(reason)
                        break
                emit(item.item_id)
            def finish(db):
                source = sources.get_source(db, source_id)
                counts = sources.count_item_states(db, source_id)
                if source.status in ("running", "paused") and not counts["pending"] and not counts["sent"]:
                    commands.transition_source(db, source_id, expected=source.status, target="done", reason="import:finished")
            _write(project, finish)
            emit()
            return _report(project, source_id, entries, warnings, notes)
        except BaseException:
            _pause(project, source_id, "import:interrupted")
            raise
