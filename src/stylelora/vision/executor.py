from dataclasses import replace
import json
import re
import time
from uuid import uuid4

from ..llm.types import LlmFailure, LlmRequest, RepairBudget
from ..project_db.cache import make_cache_key
from ..project_db.connection import read_snapshot, submit_write
from ..project_db.errors import Busy, CommitUnknown
from ..project_db.repositories import images, jobs
from ..project_db.types import AnnotationInput, JobOutcome, JobSnapshot, RatingValue, Tag
from ..project_db.validation import digest, utc_now
from ..runtime.errors import ServiceError
from ..tagging.plain import validate_plain_tag
from ..tagging.predictor import read_original
from .evidence import read_evidence, unchanged
from .policy import VisionConfig, keyword_rating

FATAL = {'llm:unauthorized', 'llm:secret_unavailable', 'llm:secret_store_unavailable', 'llm:binding_expired',
         'llm:closed', 'llm:role_unconfigured', 'llm:canceled', 'llm:model_mismatch', 'llm:unsupported_sampling',
         'vision:snapshot_unavailable', 'vision:policy_unavailable', 'operation:canceled'}


def frozen_roles(runtime, config, *, require_vision=True):
    result, bindings = {}, {}
    service = runtime.llm
    with service.config.lock:
        for role in ('vision', 'vision_nsfw', 'rating'):
            value = service.config.state['roles'].get(role) if role != 'rating' or config.use_rating_model else None
            if value is None:
                if role == 'vision' and require_vision or role == 'rating' and config.use_rating_model:
                    raise ServiceError(503, 'vision:role_unconfigured')
                result[role], bindings[role] = None, None
                continue
            ep = service.config.snapshot(value['endpoint_id'])
            model = next(m for m in ep['config']['models'] if m['name'] == value['model'])
            if role.startswith('vision') and not model['supports_images']:
                raise ServiceError(503, 'llm:image_capability_required')
            result[role] = {'model': model['name'], 'protocol': ep['config']['protocol'],
                            'supports_images': model['supports_images'], 'capability_source': model['capability_source'],
                            'response_aliases': sorted(model['response_aliases'])}
            bindings[role] = service._bind(role, ep, model['name'])
    return result, bindings


def templates(runtime):
    try:
        values = {name: runtime.llm.templates.snapshot(name) for name in ('R0', 'R2', 'R5')}
        return values, {name: item['hashes'] for name, item in values.items()}
    except LlmFailure:
        raise ServiceError(503, 'vision:skill_unavailable') from None


def snapshot(runtime, context, config, *, rating_only=False):
    _, hashes = templates(runtime)
    roles, _ = frozen_roles(runtime, config, require_vision=not rating_only)
    policy = runtime.vision.policy.view()
    protected = [context.handle.metadata.trigger_word] if context.handle.metadata.trigger_word else []
    return JobSnapshot(roles['vision']['model'] if roles['vision'] else 'rating-only', ('tags', 'nl'), 'vision',
        roles['vision']['protocol'] if roles['vision'] else 'none', digest(hashes),
        params={'pipeline_version': 'vision-runner-v1', 'config': config.model_dump(), 'roles': roles,
                'rating_only': rating_only,
                'template_hashes': hashes, 'policy_hash': policy['hash'], 'protected_tags': protected,
                'encoder': 'jpeg-white-exif-v1', 'tiers': [0, 1, 2], 'guard_version': 'literal-style-v1'},
        routing={'fallback': config.fallback, 'use_rating_model': config.use_rating_model},
        concurrency=2, timeout=config.timeout_s, retries=config.transport_retry_limit)


class VisionEngine:
    def __init__(self, runtime, context, job):
        self.runtime, self.context, self.job = runtime, context, job
        self.params = job.config_snapshot_json.get('params', {})
        try:
            if self.params['pipeline_version'] != 'vision-runner-v1':
                raise ValueError()
            self.config = VisionConfig.model_validate(self.params['config'])
            roles, self.bindings = frozen_roles(runtime, self.config, require_vision=not self.params.get('rating_only', False))
            self.templates, hashes = templates(runtime)
            if roles != self.params['roles'] or hashes != self.params['template_hashes']:
                raise ValueError()
            self.policy = runtime.vision.policy.load(self.params['policy_hash'])
        except ServiceError:
            raise
        except (KeyError, ValueError):
            raise ServiceError(503, 'vision:snapshot_unavailable') from None

    @staticmethod
    def decision(value, fallback='normal'):
        if value and value.verified:
            return {'rating': value.rating, 'rating_source': value.source, 'rating_verified': True,
                'route': 'nsfw' if value.rating in ('questionable', 'explicit') else 'normal',
                'route_reason': {'keyword': 'keyword', 'model': 'rating_model', 'refusal': 'refused_nsfw'}.get(value.source, 'default')}
        return {'rating': 'questionable' if fallback == 'nsfwfallback' else None, 'rating_source': 'fallback' if fallback == 'nsfwfallback' else None,
                'rating_verified': False, 'route': 'nsfw' if fallback == 'nsfwfallback' else 'normal',
                'route_reason': 'nsfwfallback' if fallback == 'nsfwfallback' else 'default'}

    @staticmethod
    def canceled(token):
        if token.is_set():
            raise ServiceError(409, 'operation:canceled')

    def key(self, image_id):
        return self.context.project_id, getattr(self.job, 'cache_id', self.job.job_id), image_id

    def fresh_rating(self, evidence):
        with read_snapshot(self.context.handle) as db:
            fresh = read_evidence(db, evidence.image.image_id)
        value = images.rating_value(fresh.image)
        if not value or not value.verified or fresh.refs != evidence.refs:
            raise ServiceError(409, 'vision:rating_conflict')
        return fresh

    def _rating_write(self, evidence, value, token):
        self.canceled(token)
        expected = images.rating_value(evidence.image)
        def write(db):
            self.canceled(token)
            if self.job.job_id and jobs.get_job(db, self.job.job_id).status not in ('running', 'paused'):
                raise ServiceError(409, 'operation:canceled')
            if not unchanged(db, evidence, outputs=False):
                raise ServiceError(409, 'vision:evidence_conflict')
            current = images.get_image(db, evidence.image.image_id)
            if (current.sha256, current.filename, current.file_missing) != (evidence.image.sha256, evidence.image.filename, False):
                raise ServiceError(409, 'vision:evidence_conflict')
            if images.rating_value(current) != expected:
                raise ServiceError(409, 'vision:rating_conflict')
            return images.set_rating(db, current.image_id, expected=expected, value=value).value
        # Serialize rating commits with pause/cancel and outcome commits.
        with self.runtime.vision.lock:
            key = self.key(evidence.image.image_id)
            self.runtime.vision.rating_pending[key] = (evidence, value)
            try:
                for attempt in range(3):
                    try:
                        row = submit_write(self.context.handle, write).result()
                        break
                    except Busy:
                        if attempt == 2:
                            raise
                        time.sleep(.05*(attempt+1))
            except CommitUnknown:
                with read_snapshot(self.context.handle) as db:
                    row = images.get_image(db, evidence.image.image_id)
                if images.rating_value(row) != value:
                    raise
            self.runtime.vision.rating_pending.pop(key, None)
        self.runtime.emit('image.changed', {'image_id': row.image_id}, project_id=self.context.project_id,
                          resource={'kind': 'image', 'id': row.image_id})
        return replace(evidence, image=row)

    def sampling(self, role):
        values = {'max_tokens': self.config.max_tokens}
        if self.params['roles'][role]['protocol'] != 'anthropic_messages':
            values['temperature'] = 0
        return values

    def request(self, evidence, decision, role, content, repair, tier=0):
        name = 'R5' if role == 'rating' else 'R2'
        template = self.templates[name]
        payload = {'tags': list(evidence.tags), 'rating_verified': decision['rating_verified'],
                   'rating_policy': 'nsfw' if decision['route'] == 'nsfw' else 'sfw'}
        if role == 'rating':
            payload['natural_language'] = evidence.natural_language
        else:
            payload['dataset_mode_policy'] = self.config.dataset_mode_policy
        supplied_image = content if self.params['roles'][role]['supports_images'] else None
        system = self.templates['R0']['system_text'] + '\n' + template['system_text']
        system += '\nProduct mode: describe visible content only; exclude style, artist, medium and quality terms.'
        system += '\nProtected metadata markers (do not invent visual traits from these): ' + json.dumps(self.params['protected_tags'], ensure_ascii=False)
        return LlmRequest(str(uuid4()), self.bindings[role], system, payload, template['response_schema'],
            self.config.timeout_s, self.config.transport_retry_limit, image_input=supplied_image,
            timeout_policy='vision_downgrade' if supplied_image else 'fail_unknown',
            sampling=self.sampling(role), repair_budget=repair, initial_tier=tier)

    async def rating(self, evidence, content, token):
        current = images.rating_value(evidence.image)
        if current and current.verified:
            return evidence, self.decision(current), []
        found = keyword_rating(evidence.tags, self.policy)
        diagnostics = []
        if found:
            value = RatingValue(found, 'keyword', True, utc_now())
        elif self.config.use_rating_model:
            req = replace(self.request(evidence, self.decision(None), 'rating', content, RepairBudget()), cancel=token)
            try:
                result = await self.runtime.llm.complete_json(req)
                diagnostics.append({'role': 'rating', 'actual_model': result.reply.actual_model, 'attempts': result.reply.attempts,
                                    'actual_tier': result.reply.actual_tier})
                if result.value.get('status') == 'refused_nsfw':
                    value = RatingValue('questionable', 'refusal', True, utc_now())
                elif result.value['rating'] != 'uncertain':
                    value = RatingValue(result.value['rating'], 'model', True, utc_now())
                else:
                    value = None
            except LlmFailure as error:
                if error.code in FATAL:
                    raise
                diagnostics.append({'role': 'rating', 'error': error.code})
                value = None
        else:
            value = None
        if value is None and self.config.fallback == 'nsfwfallback':
            value = RatingValue('questionable', 'fallback', False, utc_now())
        if value is not None:
            evidence = self._rating_write(evidence, value, token)
        return evidence, self.decision(value, self.config.fallback), diagnostics

    def keys(self, evidence, decision, role, model, tier):
        effective = {'role': role, 'sampling': self.sampling(role), 'mode': self.config.dataset_mode_policy,
            'rating': decision, 'encoder': self.params['encoder'], 'actual_tier': tier,
            'routing_config': {'fallback': self.config.fallback, 'use_rating_model': self.config.use_rating_model,
                               'rating_model': self.params['roles'].get('rating')},
            'protected_hash': digest(self.params['protected_tags']), 'guard_version': self.params['guard_version']}
        return {kind: make_cache_key(source='llm', type=kind, image_sha256=evidence.image.sha256,
            model=model, protocol=self.params['roles'][role]['protocol'], skill_hash=self.job.config_snapshot_json['skill_hash'],
            actual_params=effective, route=decision['route'], keyword_hash=self.params['policy_hash'], evidence_digest=evidence.checksum)
            for kind in ('tags', 'nl')}

    def covered(self, evidence, decision):
        if self.job.mode == 'all':
            return False
        role = 'vision_nsfw' if decision['route'] == 'nsfw' else 'vision'
        projection = self.params['roles'].get(role)
        a, b = evidence.current
        if not projection or not a or not b or a.job_id != b.job_id or a.model_name != b.model_name or a.params_json != b.params_json:
            return False
        for model in (projection['model'], *projection['response_aliases']):
            for tier in self.params['tiers']:
                keys = self.keys(evidence, decision, role, model, tier)
                if a.cache_key == keys['tags'] and b.cache_key == keys['nl']:
                    return True
        return False

    def validate_output(self, value):
        tags = []
        try:
            for tag in value['vision_tags']:
                normalized = validate_plain_tag(tag)
                if normalized not in tags:
                    tags.append(normalized)
        except Exception:
            raise ServiceError(422, 'vision:contract_violation') from None
        text = value['vision_description']
        corpus = '\n'.join([*tags, text]).lower().replace('_', ' ')
        for term in self.policy['blocked_terms']:
            if re.search(r'(?<!\w)' + re.escape(term) + r'(?!\w)', corpus) or any('\u4e00' <= c <= '\u9fff' for c in term) and term in corpus:
                raise ServiceError(422, 'vision:contract_violation')
        if re.search(r'\b(?:style|artist|medium|brushwork)\b', corpus):
            raise ServiceError(422, 'vision:review_required')
        return tuple(Tag(tag, None) for tag in tags), text

    async def prepare(self, image_id, token):
        with read_snapshot(self.context.handle) as db:
            evidence = read_evidence(db, image_id)
        try:
            content = read_original(self.context.handle, evidence.image)
            self.canceled(token)
            key = self.key(image_id)
            saved = self.runtime.vision.rating_pending.get(key)
            if saved:
                old, value = saved
                # Reconcile an already committed rating without replaying R5/R2.
                if images.rating_value(evidence.image) == value:
                    self.runtime.vision.rating_pending.pop(key, None)
                else:
                    evidence = self._rating_write(old, value, token)
            current = images.rating_value(evidence.image)
            # Cheap reliable routing and keyword checks precede paid R5 calls.
            initial = self.decision(current, self.config.fallback)
            found = keyword_rating(evidence.tags, self.policy) if not current or not current.verified else None
            if found:
                try:
                    evidence = self._rating_write(evidence, RatingValue(found, 'keyword', True, utc_now()), token)
                except ServiceError as error:
                    if error.code != 'vision:rating_conflict':
                        raise
                    evidence = self.fresh_rating(evidence)
                initial = self.decision(images.rating_value(evidence.image))
            if self.covered(evidence, initial):
                return evidence, JobOutcome(image_id, 'skipped', reason='cache_hit', annotation_ids=tuple(row.annotation_id for row in evidence.current))
            try:
                evidence, decision, calls = await self.rating(evidence, content, token)
            except ServiceError as conflict:
                if conflict.code != 'vision:rating_conflict':
                    raise
                # A user rating change before R2 permits one local re-evaluation,
                # with no extra R5 request. Unknown new values fail closed.
                evidence = self.fresh_rating(evidence)
                decision, calls = self.decision(images.rating_value(evidence.image)), []
            if self.covered(evidence, decision):
                return evidence, JobOutcome(image_id, 'skipped', reason='cache_hit', annotation_ids=tuple(row.annotation_id for row in evidence.current))
            step = self.runtime.vision.steps.setdefault(key, {'repair': RepairBudget(), 'tier': 0, 'calls': []})
            repair, tier = step['repair'], step['tier']
            step['calls'].extend(calls)
            calls = step['calls']
            for route_attempt in range(2):
                self.canceled(token)
                role = 'vision_nsfw' if decision['route'] == 'nsfw' else 'vision'
                if self.bindings.get(role) is None:
                    return evidence, JobOutcome(image_id, 'skipped', reason='role_empty')
                req = replace(self.request(evidence, decision, role, content, repair, tier), cancel=token)
                result = await self.runtime.llm.complete_json(req)
                tier = result.reply.actual_tier or 0
                step['tier'] = tier
                calls.append({'role': role, 'actual_model': result.reply.actual_model, 'attempts': result.reply.attempts,
                              'actual_tier': tier, 'repair_used': result.repair_used})
                if result.value.get('status') == 'refused_nsfw':
                    if route_attempt or role == 'vision_nsfw':
                        return evidence, JobOutcome(image_id, 'skipped', reason='refusal_repeated')
                    evidence = self._rating_write(evidence, RatingValue('questionable', 'refusal', True, utc_now()), token)
                    decision = self.decision(images.rating_value(evidence.image))
                    continue
                tags, text = self.validate_output(result.value)
                keys = self.keys(evidence, decision, role, result.reply.actual_model, tier)
                params = {'schema_version': 1, 'requested': {'role': role, 'model': self.params['roles'][role]['model'],
                    'protocol': self.params['roles'][role]['protocol'], 'tier': 0, 'sampling': self.sampling(role)},
                    'actual': {'role': role, 'model': result.reply.actual_model, 'protocol': self.params['roles'][role]['protocol'],
                        'tier': tier, 'sampling': self.sampling(role), **decision},
                    'evidence_digest': evidence.checksum, 'evidence_refs': list(evidence.refs[:3]),
                    'template_hashes': self.params['template_hashes'], 'policy_hash': self.params['policy_hash'],
                    'calls': calls, 'semantic_review': 'literal_checks_only'}
                values = tuple(AnnotationInput(image_id, 'llm', kind, data, model_name=result.reply.actual_model,
                    params=params, job_id=self.job.job_id, cache_key=keys[kind]) for kind, data in (('tags', tags), ('nl', text)))
                return evidence, JobOutcome(image_id, 'done', values)
        except (Busy, CommitUnknown):
            # Never resend after an unresolved database commit in this run.
            raise
        except ServiceError as error:
            if error.code in FATAL:
                raise
            missing = error.code == 'image:file_missing'
            return evidence, JobOutcome(image_id, 'skipped' if missing else 'failed', reason='file_missing' if missing else error.code)
