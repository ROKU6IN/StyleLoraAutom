"""Versioned local product library. One atomic envelope keeps aliases and translations consistent."""
from copy import deepcopy
from pathlib import Path
import threading
from uuid import uuid4

from ..exchange.common import is_link
from ..project_db.storage import atomic_json
from ..project_db.validation import digest, encode, normalize_tag
from ..rules.engine import normalize_rule
from ..runtime.contracts import Versions, strict_json, uuid_string
from ..runtime.errors import ServiceError
from .plain import validate_plain_tag

MAX_FILE = 10*1024*1024


def safe_text(value, maximum=4096, empty=False):
    if not isinstance(value, str) or len(value)>maximum or '\0' in value or not empty and not value.strip():
        raise ServiceError(422, 'catalog:invalid')
    return value.strip()


class CatalogSnapshot:
    def __init__(self, state):
        self.revision = digest([state['revision'], state['catalog'], state['translations']])
        self.entries, self.categories, self.translations = {}, {}, {}
        for row in state['catalog']['tags']:
            canonical = validate_plain_tag(row['tag'])
            self.categories[canonical] = row['category']
            for name in [canonical, *row['aliases']]:
                key = validate_plain_tag(name)
                if key in self.entries and self.entries[key] != canonical:
                    raise ServiceError(422, 'catalog:alias_conflict')
                self.entries[key] = canonical
        for name, translation in state['translations'].items():
            key = self.entries.get(name, name)
            if key in self.translations and self.translations[key] != translation:
                raise ServiceError(422, 'catalog:translation_conflict')
            self.translations[key] = translation
        self.available = bool(self.entries or self.translations)
        self.complete = state['catalog']['complete']

    def hit(self, name):
        key = normalize_tag(name)
        canonical = self.entries.get(key, key if key in self.translations else None)
        return {'query': name, 'canonical': canonical, 'category': self.categories.get(canonical),
                'translation': self.translations.get(canonical), 'match': 'unverifiable' if not self.available else
                'unknown' if canonical is None else 'alias' if canonical != key else 'known', 'catalog_revision': self.revision}

    def search(self, query):
        q = normalize_tag(query) if query.strip() else ''
        found = {}
        for key in sorted(set(self.entries) | set(self.translations)):
            hit = self.hit(key)
            if not q or q in key or q in (hit['translation'] or '').lower():
                rank = (0 if q == key else 1 if key.startswith(q) else 2, key)
                canonical = hit['canonical']
                if canonical not in found or rank < found[canonical][0]:
                    found[canonical] = (rank, hit)
        return [hit for _, hit in sorted(found.values(), key=lambda item:item[0])]


class CatalogLibrary:
    def __init__(self, runtime, directory=None):
        self.runtime, self.lock = runtime, threading.RLock()
        self.path = Path(directory).absolute()/'tagging-library.json' if directory else None
        self.state = {'schema_version': 1, 'kind': 'tagging_library', 'revision': 0,
                      'catalog': {'tags': [], 'complete': False}, 'translations': {}, 'rules': [], 'operations': {}}
        if self.path:
            self._safe_path()
            if self.path.exists():
                if self.path.stat().st_size>32*1024*1024:
                    raise ServiceError(413, 'catalog:too_large')
                self.state = strict_json(self.path.read_bytes())
                self._validate_state()
        self._snapshot = CatalogSnapshot(self.state)
        self.frozen = {}

    def freeze(self):
        """Archive parsing inputs outside projects; old imports never use a newer index."""
        from .catalog import TagCatalog
        with self.lock:
            snapshot = self._snapshot
            entries = dict(snapshot.entries)
            entries.update({k:k for k in snapshot.translations if k not in entries})
            catalog = TagCatalog(snapshot.revision,entries,dict(snapshot.categories),snapshot.complete)
            if self.path:
                payload = {'schema_version':1,'revision':catalog.revision,'entries':catalog.entries,
                           'categories':catalog.categories,'declared_complete':catalog.declared_complete}
                envelope = {**payload,'content_digest':digest(payload)}
                if len(encode(envelope).encode('utf-8'))>32*1024*1024:
                    raise ServiceError(413,'catalog:too_large')
                target = self.path.parent/'catalog-versions'/f'{catalog.revision}.json'
                if any(is_link(p) for p in (target,*target.parents)):
                    raise ServiceError(422,'path:unsafe')
                if target.exists():
                    if self.load_frozen(catalog.revision) != catalog:
                        raise ServiceError(409,'catalog:snapshot_mismatch')
                else:
                    target.parent.mkdir(parents=True,exist_ok=True)
                    atomic_json(target,envelope)
            self.frozen[catalog.revision] = catalog
            return catalog

    def load_frozen(self, revision):
        from .catalog import TagCatalog
        if not isinstance(revision,str) or len(revision)!=64 or any(c not in '0123456789abcdef' for c in revision):
            raise ServiceError(422,'catalog:snapshot_unavailable')
        with self.lock:
            if not self.path:
                if revision not in self.frozen:
                    raise ServiceError(409,'catalog:snapshot_unavailable')
                return self.frozen[revision]
            target = self.path.parent/'catalog-versions'/f'{revision}.json'
            if any(is_link(p) for p in (target,*target.parents)):
                raise ServiceError(422,'path:unsafe')
            try:
                if target.stat().st_size>32*1024*1024:
                    raise ValueError()
                payload = strict_json(target.read_bytes())
                checksum = payload.pop('content_digest')
                if set(payload)!={'schema_version','revision','entries','categories','declared_complete'} or type(payload['schema_version']) is not int or payload['schema_version']!=1 or payload['revision']!=revision or digest(payload)!=checksum:
                    raise ValueError()
                if type(payload['declared_complete']) is not bool or not isinstance(payload['entries'],dict) or not isinstance(payload['categories'],dict):
                    raise ValueError()
                for key, value in payload['entries'].items():
                    if validate_plain_tag(key)!=key or validate_plain_tag(value)!=value:
                        raise ValueError()
                if any(not isinstance(value,str) or key not in payload['entries'] for key,value in payload['categories'].items()):
                    raise ValueError()
                return TagCatalog(revision,payload['entries'],payload['categories'],payload['declared_complete'])
            except (OSError,KeyError,ValueError,TypeError,AttributeError,ServiceError) as exc:
                raise ServiceError(409,'catalog:snapshot_unavailable') from exc

    def _safe_path(self):
        if any(is_link(p) for p in (self.path, *self.path.parents)):
            raise ServiceError(422, 'path:unsafe')

    def _validate_state(self):
        value = self.state
        if not isinstance(value, dict) or set(value) != {'schema_version','kind','revision','catalog','translations','rules','operations'} or type(value['schema_version']) is not int or value['schema_version'] != 1 or value['kind'] != 'tagging_library' or type(value['revision']) is not int or value['revision'] < 0 or not isinstance(value['operations'], dict):
            raise ServiceError(422, 'catalog:invalid')
        if not isinstance(value['catalog'],dict) or set(value['catalog']) != {'tags','complete'}:
            raise ServiceError(422, 'catalog:invalid')
        catalog = self.catalog_payload({'schema_version':1,'kind':'tag_catalog',**value['catalog']})
        translations, skipped = self.translation_payload(value['translations'])
        rules = self.rule_payload(value['rules'], legacy=False)
        if catalog != value['catalog'] or translations != value['translations'] or skipped or rules != value['rules'] or len(value['operations'])>4096:
            raise ServiceError(422,'catalog:invalid')
        for key, operation in value['operations'].items():
            uuid_string(key)
            if not isinstance(operation,dict) or set(operation) != {'digest','result'} or not isinstance(operation['result'],dict):
                raise ServiceError(422,'catalog:invalid')
            result = operation['result']
            if set(result) != {'revision','version','inserted','skipped'} or any(type(result[k]) is not int or result[k]<0 for k in ('revision','inserted','skipped')) or result['revision']>value['revision']:
                raise ServiceError(422,'catalog:invalid')
            for checksum in (operation['digest'],result['version']):
                if not isinstance(checksum,str) or len(checksum)!=64 or any(c not in '0123456789abcdef' for c in checksum):
                    raise ServiceError(422,'catalog:invalid')

    def revision(self):
        with self.lock:
            return self._snapshot.revision

    def snapshot(self):
        with self.lock:
            return self._snapshot

    def version(self):
        return digest([self.state['revision'], self.state['catalog'], self.state['translations'], self.state['rules']])

    def status(self):
        with self.lock:
            return {'version': self.version(), 'revision': self.state['revision'], 'catalog_revision': self.revision(),
                    'available': self._snapshot.available, 'declared_complete': self._snapshot.complete,
                    'coverage': 'provider_declared_complete' if self._snapshot.complete else 'partial' if self._snapshot.available else 'unavailable',
                    'tag_count': len(self._snapshot.categories), 'alias_count': sum(k!=v for k,v in self._snapshot.entries.items()),
                    'translation_count': len(self._snapshot.translations), 'launcher_available': False}

    def search(self, q, *, limit=50, cursor=None):
        safe_text(q, empty=True)
        with self.lock:
            version = self.version()
            identity = digest(['catalog-search', q])
            state = self.runtime.cursors.decode(cursor, identity) if cursor else None
            if state and state['revision'] != version:
                raise ServiceError(409, 'page:stale')
            values = self._snapshot.search(q)
            start = state['last'] if state else 0
            more = start+limit<len(values)
            return {'items': values[start:start+limit], 'version': version, 'available': self._snapshot.available,
                    'limit': limit, 'has_more': more, 'upper_id': len(values), 'next_cursor': self.runtime.cursors.encode(
                        {'identity': identity, 'revision': version, 'last': start+limit}) if more else None}

    @staticmethod
    def catalog_payload(value):
        if not isinstance(value, dict) or set(value)-{'schema_version','kind','revision','tags','source','source_revision','complete'} or type(value.get('schema_version')) is not int or value.get('schema_version') != 1 or value.get('kind') != 'tag_catalog' or not isinstance(value.get('tags'), list) or type(value.get('complete',False)) is not bool:
            raise ServiceError(422, 'catalog:invalid')
        rows = {}
        for row in value['tags']:
            if not isinstance(row, dict) or set(row)-{'tag','category','aliases'} or not isinstance(row.get('aliases',[]),list):
                raise ServiceError(422, 'catalog:invalid')
            canonical = validate_plain_tag(row.get('tag'))
            item = {'tag': canonical, 'category': safe_text(row.get('category','general'),128),
                    'aliases': list(dict.fromkeys(validate_plain_tag(alias) for alias in row.get('aliases',[])))}
            if canonical in rows and rows[canonical] != item:
                raise ServiceError(422, 'catalog:alias_conflict')
            rows[canonical] = item
        return {'tags': list(rows.values()), 'complete': value.get('complete',False)}

    @staticmethod
    def translation_payload(value):
        if isinstance(value, dict) and value.get('kind') == 'translations':
            if set(value)-{'schema_version','kind','revision','entries'} or type(value.get('schema_version')) is not int or value.get('schema_version') != 1:
                raise ServiceError(422, 'catalog:invalid')
            value = value.get('entries')
        if not isinstance(value, dict):
            raise ServiceError(422, 'catalog:invalid')
        result, skipped = {}, 0
        for tag, translation in value.items():
            key = validate_plain_tag(tag)
            translation = safe_text(translation,empty=True)
            if not translation:
                skipped += 1
                continue
            if key in result and result[key] != translation:
                raise ServiceError(422, 'catalog:translation_conflict')
            result[key] = translation
        return result, skipped

    @staticmethod
    def rule_payload(value, *, legacy):
        if not isinstance(value,list) or len(value)>1000:
            raise ServiceError(422, 'rule:schema_invalid')
        rows, seen = [], set()
        for index, row in enumerate(value):
            if not isinstance(row,dict):
                raise ServiceError(422,'rule:schema_invalid')
            if legacy:
                if set(row)-{'id','name','delete','write','condition','deleteMode','writeMode','conditionMode'}:
                    raise ServiceError(422,'rule:schema_invalid')
                spec = {'schema_version':1}
                for group in ('condition','delete','write'):
                    tags = row.get(group,[])
                    if isinstance(tags,str):
                        tags = [t.strip() for t in tags.split(',') if t.strip()]
                    spec[group] = {'mode':row.get(group+'Mode','any'),'tags':tags} if tags else None
                row = {'id':str(uuid4()),'name':row.get('name') or f'导入规则 {index+1}','rule':spec}
            elif set(row) != {'id','name','rule'}:
                raise ServiceError(422,'rule:schema_invalid')
            identifier = uuid_string(row['id'])
            if identifier in seen:
                raise ServiceError(422,'rule:schema_invalid')
            seen.add(identifier)
            rows.append({'id':identifier,'name':safe_text(row['name'],512),'rule':normalize_rule(row['rule'])})
        return rows

    def preview(self, kind, data, format='json'):
        if len(data)>MAX_FILE:
            raise ServiceError(413,'catalog:too_large')
        if kind not in ('catalog','translations','rules') or format not in ('json','tsv','tagcleaner'):
            raise ServiceError(422,'catalog:invalid')
        if format == 'tsv':
            if kind != 'translations':
                raise ServiceError(422,'catalog:unsupported_format')
            value = {}
            for line in data.decode('utf-8-sig').splitlines():
                if not line.strip():
                    continue
                cells = line.split('\t')
                if len(cells)!=2 or cells[0] in value:
                    raise ServiceError(422,'catalog:invalid')
                value[cells[0]] = cells[1]
        else:
            value = strict_json(data.decode('utf-8-sig'))
        with self.lock:
            base = self.version()
            candidate = deepcopy(self.state)
            skipped = 0
            if kind == 'catalog':
                payload = self.catalog_payload(value)
                candidate['catalog'] = payload
                count = len(payload['tags'])
                examples = payload['tags'][:30]
            elif kind == 'translations':
                payload, skipped = self.translation_payload(value)
                candidate['translations'].update(payload)
                count, examples = len(payload), list(payload.items())[:30]
            else:
                if format=='json' and isinstance(value,dict) and value.get('kind')=='replacement_rules' and value.get('schema_version')==1 and not set(value)-{'schema_version','kind','revision','rules'}:
                    value = value['rules']
                payload = self.rule_payload(value,legacy=format=='tagcleaner')
                candidate['rules'] = payload
                count, examples = len(payload), payload[:30]
            CatalogSnapshot(candidate)  # Detect aliases/translation collisions before any write.
            content = {'kind':kind,'base_revision':base,'count':count,'skipped':skipped,'examples':examples,
                       'payload_digest':digest(payload),'mode':'merge' if kind=='translations' else 'replace',
                       'issues':[{'code':'migration:rule_semantics_changed','severity':'warning'}] if format=='tagcleaner' and kind=='rules' else []}
            return self.runtime.previews.add(None,'catalog-import',content,{'kind':kind,'payload':payload,'base':base})

    def _replay(self, key, signature):
        uuid_string(key)
        previous = self.state['operations'].get(key)
        if previous:
            if previous['digest'] != signature:
                raise ServiceError(409,'idempotency:key_conflict')
            return deepcopy(previous['result'])
        if len(self.state['operations'])>=4096:
            raise ServiceError(429,'catalog:operation_limit')
        return None

    def _save(self, candidate, key, signature, count=0, skipped=0):
        candidate['revision'] = self.state['revision']+1
        snapshot = CatalogSnapshot(candidate)
        version = digest([candidate['revision'],candidate['catalog'],candidate['translations'],candidate['rules']])
        result = {'revision':candidate['revision'],'version':version,'inserted':count,'skipped':skipped}
        candidate['operations'][key] = {'digest':signature,'result':result}
        if len(encode(candidate).encode())>32*1024*1024:
            raise ServiceError(413,'catalog:too_large')
        if self.path:
            self._safe_path()
            self.path.parent.mkdir(parents=True,exist_ok=True)
            atomic_json(self.path,candidate)
        self.state,self._snapshot = candidate,snapshot
        self.runtime.emit('catalog.changed', {'revision':candidate['revision']})
        return result

    def commit(self, value, expected, key):
        signature = digest(['import',value])
        with self.lock:
            replay = self._replay(key,signature)
            if replay:
                return replay
            preview,frozen = self.runtime.previews.get(None,'catalog-import',value['preview_id'],value['digest'])
            Versions.require(expected,preview['version'])
            if self.version()!=frozen['base']:
                raise ServiceError(409,'catalog:revision_conflict')
            candidate=deepcopy(self.state)
            if frozen['kind']=='translations':
                candidate['translations'].update(frozen['payload'])
            else:
                candidate[frozen['kind']]=frozen['payload']
            return self._save(candidate,key,signature,preview['count'],preview['skipped'])

    def put_translation(self, value, expected, key):
        signature = digest(['translation',value])
        with self.lock:
            replay=self._replay(key,signature)
            if replay:
                return replay
            Versions.require(expected,self.version())
            tag=validate_plain_tag(value['tag']); text=safe_text(value['value'],empty=True)
            canonical=self._snapshot.entries.get(tag,tag)
            candidate=deepcopy(self.state)
            # Replace/remove every alias spelling for this canonical key in one atomic file.
            candidate['translations']={k:v for k,v in candidate['translations'].items() if self._snapshot.entries.get(k,k)!=canonical}
            if text:
                candidate['translations'][canonical]=text
            return self._save(candidate,key,signature,int(bool(text)))

    def rules(self):
        with self.lock:
            return {'schema_version':1,'kind':'replacement_rules','revision':self.state['revision'],
                    'version':self.version(),'rules':deepcopy(self.state['rules'])}

    def put_rules(self, value, expected, key):
        signature=digest(['rules',value])
        with self.lock:
            replay=self._replay(key,signature)
            if replay:
                return replay
            Versions.require(expected,self.version())
            candidate=deepcopy(self.state)
            candidate['rules']=self.rule_payload(value['rules'],legacy=False)
            return self._save(candidate,key,signature,len(candidate['rules']))
