"""Local project, image, annotation, and dataset export commands."""

import argparse
import json
import sys
from dataclasses import asdict, replace
from pathlib import Path

from . import __version__
from .project_db.connection import ReadSession, connect, read_snapshot
from .project_db.errors import ProjectError
from .project_db.migrations import plan_migrations, verify_integrity, version_pair
from .project_db.project import create_project, open_project, update_metadata
from .project_db.validation import primitive, relative_path


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == 'session':
        from .runtime.session_cli import main as session_main
        return session_main(argv[1:])
    if len(argv) > 1 and argv[0] == 'export' and argv[1] in ('preview', 'run', 'list', 'status', 'report', 'cancel', '-h', '--help'):
        from .runtime.export_cli import main as export_main
        return export_main(argv[1:])
    if len(argv) > 1 and argv[0] == 'import' and argv[1] in ('preview', 'run', 'status', 'summary', '-h', '--help'):
        from .runtime.import_cli import main as import_main
        return import_main(argv[1:])
    if argv and argv[0] == "source":
        from .runtime.source_cli import main as source_main
        return source_main(argv[1:])
    if argv and argv[0] == "prompt":
        from .runtime.prompt_cli import main as prompt_main
        return prompt_main(argv[1:])
    if argv and argv[0] == "job":
        from .runtime.job_cli import main as job_main
        return job_main(argv[1:])
    if argv and argv[0] == "project":
        from .runtime.project_cli import main as project_main
        return project_main(argv[1:])
    if argv and argv[0] == "doctor":
        from .runtime.doctor import main as doctor_main
        return doctor_main(argv[1:])
    if argv and argv[0] == "storage":
        from .runtime.storage_cli import main as storage_main
        return storage_main(argv[1:])
    if argv and argv[0] in ("serve", "ui", "grant", "api"):
        from .runtime.cli import main as runtime_main
        return runtime_main(argv)
    parser = argparse.ArgumentParser(prog="stylelora", description="StyleLora local dataset tools")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command, description in (("serve", "Start the local HTTP service"), ("ui", "Open a browser session"),
                                 ("grant", "Authorize a local directory for HTTP"), ("api", "Call the running API"),
                                 ("storage", "Scan storage and preview/confirm cleanup through the running service"),
                                 ("doctor", "Local read-only diagnostics with opt-in network probes"),
                                 ("project", "Create, open, close and inspect projects"),
                                 ("job", "Preview, run and control annotation batches through the local service"),
                                 ("prompt", "Preview and control product prompt tasks; export ready content seeds"),
                                 ("source", "Preview and control generation sources through the running service"),
                                 ("session", "Owned foreground CLI session; JSON lines in, reviewed commands out")):
        sub.add_parser(command, help=description)
    init = sub.add_parser("init", help="Create a local .slproj container")
    init.add_argument("path", type=Path)
    init.add_argument("--name", required=True)
    init.add_argument("--trigger-word", default="")
    for name, help_text in (
        ("info", "Show metadata and database counts without recovery or writes"),
        ("check", "Check database integrity without recovery or writes"),
        ("recover", "Pause interrupted work and reconcile trusted local receipts"),
    ):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("path", type=Path)
    config = sub.add_parser("config", help="Update project name or trigger word")
    config.add_argument("path", type=Path)
    config.add_argument("--name")
    config.add_argument("--trigger-word")
    importing = sub.add_parser("import", help="HTTP preview/run/status/summary, or legacy PROJECT ROOT local copy")
    importing.add_argument("path", type=Path)
    importing.add_argument("input", type=Path)
    importing.add_argument("--name", default="Local images")
    importing.add_argument("--scheme-id", type=int)
    importing.add_argument("--source-id", type=int, help="Resume this frozen import source using the supplied input root")
    importing.add_argument("--retry-failed", action="store_true")
    importing.add_argument("--encoding", choices=("utf-8-sig", "utf-8", "gbk", "auto"),
                           help="auto tries strict UTF-8 then GBK; malformed UTF-8 BOM never falls back")
    importing.add_argument("--parser", choices=("literal-tags-v1", "catalog-comma-v1"))
    importing.add_argument("--no-recursive", action="store_true")
    importing.add_argument("--catalog", type=Path)
    listing = sub.add_parser("images", help="List imported images and their IDs")
    listing.add_argument("path", type=Path)
    listing.add_argument("--source-id", type=int)
    listing.add_argument("--status", choices=("keep", "unreviewed", "reject"))
    listing.add_argument("--limit", type=int, default=100)
    listing.add_argument("--after-id", type=int, default=0)
    show = sub.add_parser("show", help="Show merged annotations, original layers, and revision")
    show.add_argument("path", type=Path)
    show.add_argument("image_id", type=int)
    edit = sub.add_parser("edit", help="Replace manual tags and/or description atomically")
    edit.add_argument("path", type=Path)
    edit.add_argument("image_id", type=int)
    tags = edit.add_mutually_exclusive_group()
    tags.add_argument("--tags", help="Full desired comma-separated tag list")
    tags.add_argument("--clear-tags", action="store_true")
    nl = edit.add_mutually_exclusive_group()
    nl.add_argument("--nl")
    nl.add_argument("--nl-file", type=Path, help="UTF-8 description file")
    nl.add_argument("--clear-nl", action="store_true")
    edit.add_argument("--expected-revision", help="Revision returned by show; reject stale edits")
    bulk = sub.add_parser("edit-many", help="Add/remove tags on selected images in one transaction")
    bulk.add_argument("path", type=Path)
    bulk.add_argument("image_ids", nargs="+", type=int)
    bulk.add_argument("--add", default="")
    bulk.add_argument("--remove", default="")
    review = sub.add_parser("review", help="Set review status on selected images atomically")
    review.add_argument("path", type=Path)
    review.add_argument("image_ids", nargs="+", type=int)
    review.add_argument("--status", choices=("keep", "unreviewed", "reject"), required=True)
    exporting = sub.add_parser("export", help="HTTP preview/run/list/status/report/cancel, or legacy PROJECT export")
    exporting.add_argument("path", type=Path)
    exporting.add_argument("--scheme-id", type=int)
    exporting.add_argument("--source-id", action="append", type=int)
    exporting.add_argument("--include-nl", action="store_true", default=None)
    exporting.add_argument("--include-unreviewed", action="store_true")
    exporting.add_argument("--include-json", action="store_true")
    exporting.add_argument("--block", default="", help="Additional comma-separated blocked tags")
    exporting.add_argument("--blocklist-mode", choices=("remove", "warn"), default="remove")
    exporting.add_argument("--catalog", type=Path)
    exporting.add_argument("--allow-existing-trigger", action="store_true")
    exporting.add_argument("--dry-run", action="store_true")
    exporting.add_argument("--acknowledge-hardlink", action="store_true",
                           help="Confirm that a hardlink scheme shares image content: editing exports changes project originals")
    args = parser.parse_args(argv)
    owner = None
    try:
        import os
        if os.name == "nt" and args.command not in ("info", "check"):
            from .runtime.instance import ProductLock
            from .runtime.errors import ServiceError
            try:
                owner = ProductLock()
            except ServiceError as exc:
                raise ProjectError("Local service is running; use stylelora api to operate on its project") from exc
        exit_code = 0
        if args.command == "init":
            with create_project(args.path, name=args.name, trigger_word=args.trigger_word) as project:
                output = {"path": str(project.root), "metadata": asdict(project.metadata)}
        elif args.command == "recover":
            with open_project(args.path) as project:
                output = asdict(project.recovery_report)
        elif args.command in ("info", "check"):
            root = args.path.resolve()
            for filename in ("project.json", "project.db"):
                relative_path(root, filename)
            metadata, version = version_pair(root)
            needed = plan_migrations(metadata.format_version, version)
            if needed or (root / ".migration-state.json").exists():
                raise ProjectError("Migration or recovery is required before inspection")
            db = connect(root / "project.db", readonly=True)
            try:
                db.execute("BEGIN")
                if args.command == "check":
                    issues = verify_integrity(ReadSession(db), full=True)
                    output = {"ok": not issues, "issues": issues, "format_version": version}
                else:
                    output = {"metadata": asdict(metadata), "counts": {
                        table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                        for table in ("sources", "source_items", "images", "annotations", "jobs", "operation_log")
                    }}
            finally:
                db.close()
        else:
            with open_project(args.path) as project:
                output, exit_code = _dataset_command(project, args)
        print(json.dumps(primitive(output), ensure_ascii=False, indent=2))
        return 1 if args.command == "check" and not output["ok"] else exit_code
    except (ProjectError, OSError, ValueError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        if getattr(exc, "relative_directory", None):
            print(f"Interrupted export: {exc.relative_directory}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted; local receipts are retained for recovery.", file=sys.stderr)
        return 130
    finally:
        if owner:
            owner.close()


def _dataset_command(project, args):
    from .annotations.editing import bulk_edit_manual, edit_manual, get_annotation_view, review_images
    from .project_db.repositories import images
    from .project_db.types import ImageFilter, PageRequest
    from .tagging.catalog import load_catalog
    from .tagging.plain import parse_tag_argument

    catalog = load_catalog(args.catalog) if getattr(args, "catalog", None) else None
    if args.command == "config":
        output = update_metadata(project, name=project.metadata.name if args.name is None else args.name,
                                 trigger_word=project.metadata.trigger_word if args.trigger_word is None else args.trigger_word)
    elif args.command == "import":
        from .exchange.importer import import_images, scheme_options
        options = None
        if args.encoding or args.parser or args.no_recursive:
            from .project_db.repositories import schemes, sources
            with read_snapshot(project) as db:
                if args.source_id is not None:
                    from .exchange.importer import _load_options
                    defaults, _ = _load_options(sources.get_source(db, args.source_id))
                else:
                    scheme = schemes.get_import_scheme(db, args.scheme_id) if args.scheme_id is not None else schemes.get_default_import_scheme(db)
                    defaults = scheme_options(scheme)
            options = replace(defaults, encoding=args.encoding or defaults.encoding, parser=args.parser or defaults.parser,
                              recursive=False if args.no_recursive else defaults.recursive)
        output = import_images(project, args.input, name=args.name, options=options, scheme_id=args.scheme_id,
                               source_id=args.source_id, retry_failed=args.retry_failed, catalog=catalog)
        return output, 2 if output.failed or output.pending or output.unresolved else 0
    elif args.command == "images":
        with read_snapshot(project) as db:
            output = images.list_images(db, ImageFilter(
                source_ids=(args.source_id,) if args.source_id is not None else None,
                review_statuses=(args.status,) if args.status else None, include_missing=True),
                PageRequest(limit=args.limit, after_id=args.after_id))
    elif args.command == "show":
        output = get_annotation_view(project, args.image_id)
    elif args.command == "edit":
        content = args.nl_file.read_text(encoding="utf-8-sig") if args.nl_file else args.nl
        output = edit_manual(project, args.image_id,
                             tags=() if args.clear_tags else parse_tag_argument(args.tags) if args.tags is not None else None,
                             natural_language=content, write_nl=args.clear_nl or content is not None,
                             expected_revision=args.expected_revision)
    elif args.command == "edit-many":
        if not args.add.strip() and not args.remove.strip():
            raise ValueError("Specify --add and/or --remove")
        output = bulk_edit_manual(project, args.image_ids, add=parse_tag_argument(args.add), remove=parse_tag_argument(args.remove))
    elif args.command == "review":
        output = review_images(project, args.image_ids, args.status)
    elif args.command == "export":
        from .exchange.exporter import ExportOptions, export_images, preview_export
        options = ExportOptions(include_nl=args.include_nl, include_unreviewed=args.include_unreviewed,
                                include_json=args.include_json, source_ids=tuple(args.source_id) if args.source_id else None,
                                blocked_tags=parse_tag_argument(args.block), blocklist_mode=args.blocklist_mode,
                                allow_existing_trigger=args.allow_existing_trigger)
        if args.dry_run:
            output = preview_export(project, scheme_id=args.scheme_id, options=options, catalog=catalog)
        elif args.acknowledge_hardlink:
            preview = preview_export(project, scheme_id=args.scheme_id, options=options, catalog=catalog)
            if preview['hardlink_ack_digest'] is None:
                raise ValueError('--acknowledge-hardlink requires a hardlink export scheme')
            output = export_images(project, scheme_id=args.scheme_id, options=options, catalog=catalog,
                                   expected_digest=preview['plan_digest'], hardlink_ack_digest=preview['hardlink_ack_digest'])
        else:
            output = export_images(project, scheme_id=args.scheme_id, options=options, catalog=catalog)
        failed = (any(item["status"] == "failed" for item in output["images"]) if args.dry_run else output["counts"]["failed"])
        return output, 2 if failed else 0
    return output, 0


if __name__ == "__main__":
    raise SystemExit(main())
