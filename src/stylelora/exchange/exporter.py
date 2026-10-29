"""Snapshot captions, verify copied/linked images, and publish a complete export directory."""

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import re
from pathlib import Path
from uuid import uuid4

from ..project_db.connection import submit_write
from ..project_db.errors import PreviewStale, UnsupportedScheme, ValidationError
from ..project_db.merge import merge_caption
from ..project_db.repositories import images, schemes
from ..project_db.storage import atomic_json
from ..project_db.types import ImageFilter, ImageRow
from ..project_db.repositories.common import row_dto
from ..project_db.validation import digest, encode, primitive, relative_path, utc_now
from ..tagging.plain import parse_tag_argument, validate_plain_tag
from ..tagging.trigger_validation import validate_trigger
from ..caption.sorters import load_pyramid
from .schemes import export_configuration
from .common import copy_verified, io_reason, notify_progress, project_operation, remove_owned, write_new
from .errors import ExchangeError, ExportInterrupted
from .sidecar import serialize_caption
from .hardlink import hardlink_verified, verify_hardlink


@dataclass(frozen=True)
class ExportOptions:
    include_nl: bool | None = None
    include_unreviewed: bool | None = None
    include_json: bool | None = None
    source_ids: tuple[int, ...] | None = None
    blocked_tags: tuple[str, ...] = ()
    blocklist_mode: str | None = None
    allow_existing_trigger: bool = False


@dataclass(frozen=True)
class ExportEntry:
    image: ImageRow
    tags: tuple[str, ...] = ()
    natural_language: str | None = None
    status: str = "ready"
    reason: str | None = None
    warnings: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    output_path: str | None = None
    sorting_counts: dict[str, int] | None = None


def _snapshot(db, scheme_id, options, catalog):
    for key in ("allow_existing_trigger",):
        if type(getattr(options, key)) is not bool:
            raise ValidationError(f"{key} must be boolean")
    if options.include_nl is not None and type(options.include_nl) is not bool:
        raise ValidationError("include_nl must be boolean or None")
    if options.include_json is not None and type(options.include_json) is not bool:
        raise ValidationError('include_json must be boolean or None')
    if options.include_unreviewed is not None and type(options.include_unreviewed) is not bool:
        raise ValidationError('include_unreviewed must be boolean or None')
    if options.blocklist_mode not in (None, "remove", "warn"):
        raise ExchangeError("export:unsupported_blocklist_mode")
    if scheme_id is None:
        scheme_id = db.execute("SELECT scheme_id FROM export_schemes ORDER BY scheme_id LIMIT 1").fetchone()[0]
    scheme = schemes.get_export_scheme(db, scheme_id)
    schemes.require_executable_export(scheme)
    if scheme.exporter != "image_txt":
        raise UnsupportedScheme("Only image_txt export is implemented")
    try:
        configuration, legacy = export_configuration(scheme)
    except ValueError as exc:
        raise UnsupportedScheme('Unsupported export configuration') from exc
    out = configuration['output_config_json']
    out['include_json'] = options.include_json if options.include_json is not None else out['include_json']
    filters = configuration['filter_json']
    statuses = filters.get("review_statuses", ["keep"])
    if not isinstance(statuses, (list, tuple)) or any(status not in ("keep", "unreviewed", "reject") for status in statuses):
        raise ValidationError("Invalid export review filter")
    accepted = set(statuses) - {"reject"}
    if options.include_unreviewed:
        accepted.add("unreviewed")
    elif options.include_unreviewed is False:
        accepted.discard('unreviewed')
    config = configuration['caption_config_json']
    blocklist_mode = options.blocklist_mode or ('remove' if legacy else config['blocklist_mode'])
    if options.include_nl is not None:
        config["include_nl"] = options.include_nl
    # Validate resources even for an empty selection; never silently change modes.
    sorter = load_pyramid(catalog) if config['tag_sort'] == 'prompt_pyramid' else None
    metadata = db.project.metadata
    if not metadata.trigger_word.strip():
        raise ExchangeError("trigger:required")
    trigger = validate_plain_tag(metadata.trigger_word, trigger=True)
    sources = {row["source_id"]: row["style_prompt"] for row in db.execute("SELECT source_id,style_prompt FROM sources")}
    validation = validate_trigger(trigger, style_prompts=[p or '' for p in sources.values()], catalog=catalog)
    if any(trigger in prompt.lower().replace("_", " ") for prompt in sources.values() if prompt):
        raise ExchangeError("trigger:style_conflict")
    warnings = []
    if out['image_mode'] == 'hardlink':
        warnings.append('export:hardlink_shared_content')
    if catalog is None or not catalog.entries:
        trigger_check = "unverified_no_catalog"
        warnings.append("trigger:catalog_unverified")
    else:
        known = catalog.lookup(trigger)
        trigger_check = "found" if known else "absent_in_supplied_catalog"
        if known and not options.allow_existing_trigger:
            raise ExchangeError("trigger:existing_tag", "Use explicit allow_existing_trigger to acknowledge the collision")
        if known:
            warnings.append("trigger:existing_tag")
        if not catalog.declared_complete:
            warnings.append("trigger:partial_catalog")
    block = {validate_plain_tag(value) for value in options.blocked_tags}
    if catalog:
        block.update(key for key, category in catalog.categories.items() if category == "artist")
        block.update(key for key, canonical in catalog.entries.items() if canonical in block)
    if trigger in block:
        raise ExchangeError("trigger:blocklist_conflict")
    source_ids = options.source_ids if options.source_ids is not None else filters.get("source_ids")
    where, params = images.image_where(ImageFilter(source_ids=source_ids, include_missing=True))
    selected = tuple(row_dto(row, ImageRow) for row in db.execute(f"SELECT * FROM images WHERE {where} ORDER BY image_id", params))
    entries, occupied = [], set()
    for image in selected:
        skip = "image:missing" if image.file_missing else "review:reject" if image.review_status == "reject" else (
            "review:filtered" if image.review_status not in accepted else None)
        if skip:
            entries.append(ExportEntry(image, status="skipped", reason=skip))
            continue
        try:
            source_block = set(parse_tag_argument(sources[image.source_id] or ""))
            local_block = block | source_block
            merged = merge_caption(db, image.image_id, config=config, trigger_word=trigger,
                                   sorter=sorter.sort if sorter else None)
            tags = tuple(validate_plain_tag(tag) for tag in merged.tags)
            matches = tuple(tag for tag in tags if tag in local_block and tag != trigger)
            notes = []
            if matches:
                notes.append("caption:blocklist_match")
            # Match whole normalized phrases; never delete a substring from prose.
            normalized_nl = merged.natural_language.lower().replace("_", " ") if merged.natural_language else ""
            nl_matches = [tag for tag in local_block if re.search(r"(?<!\w)" + re.escape(tag) + r"(?!\w)", normalized_nl)]
            if nl_matches and blocklist_mode == "remove":
                raise ExchangeError("caption:nl_requires_review")
            if nl_matches:
                notes.append("caption:nl_blocklist_match")
            removed = matches if blocklist_mode == "remove" else ()
            tags = tuple(tag for tag in tags if tag not in removed)
            if out['require_nonempty_tags'] and not any(tag != trigger for tag in tags):
                raise ExchangeError('caption:empty_content')
            serialize_caption(tags, merged.natural_language)
            filename = image.filename if legacy else out['filename'].format(image_id=image.image_id, stem=Path(image.filename).stem, ext=Path(image.filename).suffix)
            target = Path(out['subdirectory']) / filename
            names = {str(target).casefold(), str(target.with_suffix('.txt')).casefold()}
            if out['include_json']:
                names.add(str(target.with_suffix('.json')).casefold())
            if names & occupied:
                if out['overwrite'] == 'error':
                    raise ValidationError('Export filename collision')
                entries.append(ExportEntry(image,status='skipped',reason='export:filename_collision'))
                continue
            occupied.update(names)
            sorting_counts = sorter.counts(tuple(tag for tag in tags if tag != trigger)) if sorter else None
            if sorting_counts and sorting_counts['unclassified']:
                notes.append('sorter:unclassified')
            entries.append(ExportEntry(image, tags, merged.natural_language, warnings=tuple(notes), removed=removed,
                                       output_path=target.as_posix(), sorting_counts=sorting_counts))
        except ExchangeError as exc:
            entries.append(ExportEntry(image, status="failed", reason=exc.code))
    snapshot = {"schema_version": 1, "created_at": utc_now(), "project": primitive(metadata),
                "scheme": primitive(scheme), "options": primitive(options), "caption_config": config, 'output_config': out, 'legacy_output': legacy,
                'trigger_validation': validation, 'sorter': sorter.metadata() if sorter else None,
                "trigger_word": trigger, "trigger_check": trigger_check,
                "catalog_revision": catalog.revision if catalog else None, "warnings": warnings}
    snapshot["snapshot_sha256"] = digest({"snapshot": snapshot, "entries": entries})
    snapshot["plan_digest"] = digest({"snapshot": {key: value for key, value in snapshot.items() if key not in ("created_at", "snapshot_sha256")}, "entries": entries})
    return snapshot, tuple(entries)


def _result(entry):
    return {"image_id": entry.image.image_id, "source_id": entry.image.source_id,
            "filename": entry.image.filename, "status": entry.status, "reason": entry.reason,
            "warnings": list(entry.warnings), "removed_tags": list(entry.removed), "files": [], 'output_path': entry.output_path,
            'sorting_counts': entry.sorting_counts}


def _report(snapshot, results, frequencies, directory, status):
    counts = Counter(result["status"] for result in results)
    sorted_results = [result for result in results if result['status'] == 'exported' and result.get('sorting_counts')]
    return {**snapshot, "status": status, "directory": directory,
            "counts": {"selected": len(results), **{key: counts[key] for key in ("exported", "skipped", "failed")}},
            "reason_counts": dict(sorted(Counter(result["reason"] for result in results if result["reason"]).items())),
            "tag_frequencies": dict(sorted(frequencies.items())), "trigger_count": frequencies[snapshot["trigger_word"]],
            "sort_summary": {'images_with_unclassified': sum(result['sorting_counts']['unclassified'] > 0 for result in sorted_results),
                             'unclassified_tag_occurrences': sum(result['sorting_counts']['unclassified'] for result in sorted_results)} if snapshot['sorter'] else None,
            "images": results}


def preview_export(project, *, scheme_id=None, options=ExportOptions(), catalog=None):
    with project_operation(project):
        snapshot, entries = submit_write(project, lambda db: _snapshot(db, scheme_id, options, catalog)).result()
        return {**snapshot, "dry_run": True,
                "hardlink_ack_digest": snapshot['plan_digest'] if snapshot['output_config']['image_mode'] == 'hardlink' else None,
                "images": [
            {**_result(entry), "tags": entry.tags, "natural_language": entry.natural_language} for entry in entries]}


def export_images(project, *, scheme_id=None, options=ExportOptions(), catalog=None, cancelled=None,
                  expected_digest=None, export_id=None, progress=None, trigger_override_digest=None, hardlink_ack_digest=None):
    with project_operation(project):
        snapshot, entries = submit_write(project, lambda db: _snapshot(db, scheme_id, options, catalog)).result()
        if expected_digest is not None and snapshot["plan_digest"] != expected_digest:
            raise PreviewStale("Export inputs changed after preview")
        hardlink = snapshot['output_config']['image_mode'] == 'hardlink'
        if hardlink and hardlink_ack_digest != snapshot['plan_digest']:
            raise ExchangeError('export:hardlink_confirmation_required')
        if not hardlink and hardlink_ack_digest is not None:
            raise PreviewStale('Hardlink confirmation does not match the export mode')
        snapshot["export_id"] = export_id or str(uuid4())
        snapshot['trigger_override_digest'] = trigger_override_digest
        snapshot['hardlink_ack_digest'] = hardlink_ack_digest
        project._fault("export_after_snapshot")
        parent = relative_path(project.root, f"exports/{snapshot['scheme']['scheme_id']}")
        parent.mkdir(parents=True, exist_ok=True)
        run = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid4().hex[:12]
        final = relative_path(project.root, f"exports/{snapshot['scheme']['scheme_id']}/{run}")
        staging = relative_path(project.root, f"exports/{snapshot['scheme']['scheme_id']}/.partial-{run}")
        staging.mkdir(exist_ok=False)
        relative = final.relative_to(project.root).as_posix()
        state = {"schema_version": 1, "status": "writing", "directory": relative,
                 "image_mode": snapshot['output_config']['image_mode'],
                 "export_id": snapshot["export_id"], "snapshot_sha256": snapshot["snapshot_sha256"]}
        receipt = staging / ".export-state.json"
        results, frequencies, linked_paths, link_checks = [], Counter(), [], []
        def emit():
            counts = Counter(result["status"] for result in results)
            notify_progress(progress, {"selected": len(entries), "exported": counts["exported"],
                                       "skipped": counts["skipped"], "failed": counts["failed"]})
        try:
            atomic_json(receipt, state)
            emit()
            for entry in entries:
                if cancelled and cancelled():
                    raise ExchangeError("export:canceled")
                result = _result(entry)
                if entry.status != "ready":
                    results.append(result)
                    emit()
                    continue
                pair = staging / (".pair-" + str(entry.image.image_id))
                pair.mkdir(exist_ok=False)
                owned, published = [], []
                try:
                    src = relative_path(project.root, "images/" + entry.image.filename)
                    output_path = Path(entry.output_path)
                    output_dir = relative_path(staging, output_path.parent.as_posix()) if output_path.parent.as_posix() != '.' else staging
                    output_dir.mkdir(parents=True, exist_ok=True)
                    image_target = pair / output_path.name
                    if hardlink:
                        size, sha = hardlink_verified(src, image_target, entry.image.sha256, entry.image.file_size)
                        linked_paths.append(image_target)
                        owned.append(image_target)
                    else:
                        owned.append(image_target)
                        size, sha = copy_verified(src, image_target, entry.image.sha256, entry.image.file_size, entry.image.file_size)
                    files = [{"path": image_target.name, "bytes": size, "sha256": sha}]
                    project._fault("export_after_image_copy")
                    txt_target = pair / (output_path.stem + ".txt")
                    owned.append(txt_target)
                    files.append({"path": txt_target.name, **write_new(txt_target, serialize_caption(entry.tags, entry.natural_language))})
                    project._fault("export_after_caption")
                    if snapshot['output_config']['include_json']:
                        json_target = pair / (output_path.stem + ".json")
                        owned.append(json_target)
                        files.append({"path": json_target.name, **write_new(json_target, (encode({
                            "schema_version": 1, "image_id": entry.image.image_id,
                            "sha256": entry.image.sha256, "png_metadata": entry.image.png_meta_json,
                        } if snapshot['legacy_output'] else entry.image.png_meta_json) + "\n").encode("utf-8"))})
                    for path in owned:
                        destination = output_dir / path.name
                        if destination.exists():
                            raise ExchangeError("export:destination_exists")
                        path.rename(destination)
                        published.append(destination)
                        if hardlink and path == image_target:
                            linked_paths.append(destination)
                    pair.rmdir()
                    for item in files:
                        item['path'] = (output_path.parent / item['path']).as_posix()
                    result.update(status="exported", reason=None, files=files)
                    if hardlink:
                        link_checks.append((src, output_dir / image_target.name, entry.image.sha256, entry.image.file_size))
                    frequencies.update(set(entry.tags))
                except (ExchangeError, OSError) as exc:
                    remove_owned(staging, (*owned, *published))
                    pair.rmdir()
                    reason = io_reason(exc)
                    if reason in ("storage:full", "storage:permission_denied", "export:destination_exists"):
                        raise
                    result.update(status="failed", reason=reason, files=[])
                results.append(result)
                emit()
            if cancelled and cancelled():
                raise ExchangeError("export:canceled")
            report = _report(snapshot, results, frequencies, relative, "complete")
            atomic_json(staging / "export_report.json", report)
            project._fault("export_before_publish")
            if hardlink:
                for check in link_checks:
                    if cancelled and cancelled():
                        raise ExchangeError('export:canceled')
                    verify_hardlink(*check)
            if cancelled and cancelled():
                raise ExchangeError('export:canceled')
            atomic_json(receipt, {**state, "status": "complete"})
            if final.exists():
                raise ExchangeError("export:destination_exists")
            staging.rename(final)
            return report
        except BaseException as exc:
            partial = staging.relative_to(project.root).as_posix()
            reason = io_reason(exc) if isinstance(exc, (ExchangeError, OSError)) else "export:interrupted"
            if hardlink:
                try:
                    remove_owned(staging, linked_paths)
                except OSError:
                    # Keep the interrupted receipt if the OS prevents unlinking;
                    # no formal directory is published and sources are never removed.
                    state['hardlink_cleanup_pending'] = True
            try:
                atomic_json(receipt, {**state, "status": "interrupted", "reason": reason})
            except OSError:
                pass  # The writing receipt remains evidence when a full disk prevents updating it.
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise ExportInterrupted(reason, partial) from exc
