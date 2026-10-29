"""Explicit local exchange formats, with frozen row diagnostics before any write."""
from copy import deepcopy
import csv
from hashlib import sha256
import io
from zipfile import ZipFile, ZipInfo, ZIP_STORED

from ..project_db.validation import digest, encode
from ..runtime.contracts import strict_json
from ..runtime.errors import ServiceError
from .dictionaries import compact
from .types import ImportDocument, DictionaryInput, Entry, Feature, dimension as valid_dimension

MAX_IMPORT = 10 * 1024 * 1024


def summarize(records, kind, format, catalog_revision, *, issues=(), source_digest, selection=None):
    entries, dimensions = [], {}
    diagnostics = list(issues)
    for row in records:
        diagnostics.extend(row['issues'])
        if row['value'] is None or row['issues']:
            continue
        value = deepcopy(row['value'])
        if kind == 'prompts':
            entries.append(value)
        else:
            name = value.pop('dimension')
            dimensions.setdefault(name, []).append(value)
    payload = {'schema_version': 1, 'kind': kind, 'name': '导入内容',
               **({'entries': entries} if kind == 'prompts' else {'dimensions': dimensions})}
    count = len(entries) if kind == 'prompts' else sum(map(len, dimensions.values()))
    duplicates = 0
    try:
        DictionaryInput.model_validate(payload)
        payload, duplicates, warnings = compact(payload)
        diagnostics.extend(warnings)
    except ServiceError as error:
        diagnostics.append({'row': None, 'field': None, 'severity': 'error', 'code': error.code})
    except Exception:
        diagnostics.append({'row': None, 'field': None, 'severity': 'error', 'code': 'prompt:invalid_row'})
    examples = payload['entries'][:100] if kind == 'prompts' else [
        {'dimension': name, **row} for name, rows in payload['dimensions'].items() for row in rows][:100]
    report = {'kind': kind, 'format': format, 'count': count, 'duplicates': duplicates,
              'valid': not any(v['severity'] == 'error' for v in diagnostics), 'issue_count': len(diagnostics),
              'issues': sorted(diagnostics, key=lambda v: v['severity'] != 'error')[:100],
              'originals': [{'row': row['row'], 'text': row['original']} for row in records if row['original'] is not None][:100],
              'catalog_revision': catalog_revision, 'content_digest': digest(payload), 'examples': examples,
              'source_digest': source_digest, 'rows_digest': digest(records), 'row_count': len(records),
              'selectable_count': sum(row['value'] is not None and not row['issues'] for row in records),
              'selection_available': bool(records) and not issues, 'selection': selection}
    return payload, report


def parse(data, format, kind, dimension, catalog, *, with_rows=False):
    if len(data) > MAX_IMPORT:
        raise ServiceError(413, 'prompt:import_limit')
    if format not in ('json', 'csv', 'txt') or kind not in ('features', 'prompts'):
        raise ServiceError(422, 'schema:invalid')
    try:
        text = data.decode('utf-8-sig')
    except UnicodeError:
        raise ServiceError(422, 'prompt:invalid_encoding') from None
    records, issues = [], []

    def add(value, number, original=None, errors=()):
        row = {'row': number, 'original': original[:4096] if original is not None else None, 'value': None,
               'issues': [{'row': number, 'field': None, 'severity': 'error', 'code': code} for code in errors]}
        try:
            if kind == 'prompts':
                entry = Entry.model_validate(value).model_dump()
                entry['tags'] = list(dict.fromkeys(catalog.entries.get(tag, tag) for tag in entry['tags']))
            else:
                value = dict(value)
                name = valid_dimension(value.pop('dimension'))
                entry = Feature.model_validate(value).model_dump()
                entry['tag'] = catalog.entries.get(entry['tag'], entry['tag'])
                entry = {'dimension': name, **entry}
            # An unsupported segment invalidates the whole TXT row.
            if not row['issues']:
                row['value'] = entry
        except Exception:
            row['issues'].append({'row': number, 'field': None, 'severity': 'error', 'code': 'prompt:invalid_row'})
        records.append(row)

    try:
        if format == 'json':
            value = ImportDocument.model_validate(strict_json(text)).model_dump()
            if value['kind'] != kind:
                raise ValueError()
            if kind == 'prompts':
                for number, row in enumerate(value['entries'], 1):
                    add(row, number)
            else:
                number = 0
                for name, rows in value['dimensions'].items():
                    for row in rows:
                        number += 1
                        add({'dimension': name, **row} if isinstance(row, dict) and 'dimension' not in row else None, number)
        elif format == 'csv':
            reader = csv.DictReader(io.StringIO(text, newline=''), strict=True)
            columns = ['tags', 'natural_language', 'aspect', 'rating'] if kind == 'prompts' else ['dimension', 'tag', 'rating']
            if reader.fieldnames is None or len(set(reader.fieldnames)) != len(reader.fieldnames) or set(reader.fieldnames) != set(columns):
                raise ValueError()
            for number, row in enumerate(reader, 2):
                if number > 10001:
                    raise ServiceError(413, 'prompt:import_limit')
                try:
                    if None in row or any(v is None for v in row.values()):
                        raise ValueError()
                    if kind == 'prompts':
                        row = {'tags': strict_json(row['tags']), 'natural_language': row['natural_language'] or None,
                               'aspect': row['aspect'] or None, 'rating': row['rating'] or None}
                    else:
                        row['rating'] = row['rating'] or None
                except Exception:
                    row = None
                add(row, number)
        else:
            if kind == 'features':
                dimension = valid_dimension(dimension)
            elif not catalog.entries:
                raise ServiceError(422, 'prompt:catalog_required')
            lines = text.splitlines()
            if len(lines) > 10000:
                raise ServiceError(413, 'prompt:import_limit')
            for number, line in enumerate(lines, 1):
                if not line.strip():
                    continue
                if kind == 'features':
                    parts = line.split('\t')
                    row = {'dimension': dimension, 'tag': parts[0],
                           'rating': parts[1].strip() or None if len(parts) == 2 else None} if len(parts) <= 2 else None
                    add(row, number, line)
                else:
                    from .types import tag as plain_tag
                    tags, nl, errors = [], [], []
                    for part in line.split(','):
                        try:
                            tag = plain_tag(part)
                            known = catalog.entries.get(tag)
                            if known:
                                tags.append(known)
                            else:
                                nl.append(part.strip())
                        except ValueError:
                            errors.append('prompt:unsupported_syntax')
                    add({'tags': tags, 'natural_language': ', '.join(nl) or None}, number, line, errors)
    except ServiceError:
        raise
    except Exception:
        issues.append({'row': None, 'field': None, 'severity': 'error', 'code': 'prompt:invalid_format'})
    payload, report = summarize(records, kind, format, catalog.revision, issues=issues, source_digest=sha256(data).hexdigest())
    return ({'payload': payload, 'rows': records} if with_rows else payload), report


def select_rows(report, private, numbers):
    if not report['selection_available']:
        raise ServiceError(422, 'prompt:selection_unavailable')
    selected = set(numbers)
    records = [row for row in private['rows'] if row['row'] in selected]
    if len(records) != len(selected) or any(row['value'] is None or row['issues'] for row in records):
        raise ServiceError(422, 'prompt:import_invalid_selection')
    payload, result = summarize(records, report['kind'], report['format'], report['catalog_revision'],
                                source_digest=report['source_digest'],
                                selection={'parent_preview_id': report['preview_id'], 'parent_digest': report['digest'],
                                           'rows': sorted(selected), 'excluded_count': report['row_count'] - len(records)
                                           + (report.get('selection') or {}).get('excluded_count', 0)})
    return {'payload': payload, 'rows': deepcopy(records)}, result


def export(value, format):
    value = {key: content for key, content in value.items() if key not in ('version', '_operations')}
    if format == 'json':
        return (encode(value) + '\n').encode('utf-8'), 'application/json', []
    output = io.StringIO(newline='')
    if format == 'csv':
        fields = ['tags', 'natural_language', 'aspect', 'rating'] if value['kind'] == 'prompts' else ['dimension', 'tag', 'rating']
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        if value['kind'] == 'prompts':
            for row in value['entries']:
                writer.writerow({**row, 'tags': encode(row['tags'])})
        else:
            for name, rows in value['dimensions'].items():
                for row in rows:
                    writer.writerow({'dimension': name, **row})
        return output.getvalue().encode('utf-8-sig'), 'text/csv', []
    if format == 'txt' and value['kind'] == 'prompts':
        return ('\n'.join(', '.join(row['tags']) + (', ' + ' '.join(row['natural_language'].split()) if row['natural_language'] else '')
                          for row in value['entries']) + '\n').encode('utf-8'), 'text/plain', ['aspect', 'rating', 'structure']
    if format == 'txt' and value['kind'] == 'features':
        archive, mapping = io.BytesIO(), []
        # Never use user dimension names as filesystem paths.
        with ZipFile(archive, 'w', compression=ZIP_STORED) as zip:
            def write(name, data):
                info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system, info.external_attr = 3, 0o100644 << 16
                zip.writestr(info, data)
            for index, (name, rows) in enumerate(value['dimensions'].items(), 1):
                filename = f'{index:03d}.txt'
                data = ('\n'.join(row['tag'] + ('\t' + row['rating'] if row['rating'] else '') for row in rows) + '\n').encode('utf-8')
                write(filename, data)
                mapping.append({'filename': filename, 'dimension': name, 'count': len(rows), 'sha256': sha256(data).hexdigest()})
            write('dimensions.json', (encode({'schema_version': 1, 'format': 'feature-txt-v1', 'files': mapping}) + '\n').encode('utf-8'))
        return archive.getvalue(), 'application/zip', ['dictionary_envelope']
    raise ServiceError(501, 'prompt:format_unavailable')
