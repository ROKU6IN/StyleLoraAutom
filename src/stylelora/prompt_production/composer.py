"""Frozen text-only R0/R1/R5 execution. No project data or endpoint credentials."""
from copy import deepcopy
import asyncio
import re
from uuid import uuid4

from ..llm.types import LlmRequest, RepairBudget
from ..project_db.validation import digest, normalize_tag
from ..runtime.errors import ServiceError
from ..vision.policy import RATINGS
from .sampling import content_issue, excluded
from .types import Entry


ROLES = ('prompt', 'prompt_nsfw', 'rating')
FATAL = {'llm:unauthorized', 'llm:secret_store_unavailable', 'llm:auth_unavailable', 'llm:authentication_failed', 'llm:secret_unavailable',
         'llm:binding_expired', 'llm:closed', 'llm:role_unconfigured', 'llm:model_mismatch',
         'llm:unsupported_sampling', 'llm:auth_failed', 'llm:no_tokens', 'llm:secret_invalid'}


def dependencies(runtime):
    config = runtime.llm.config
    with config.lock:
        roles = {}
        for role in ROLES:
            binding = config.state['roles'][role]
            if binding is None:
                roles[role] = None
                continue
            ep = config._endpoint(binding['endpoint_id'])
            roles[role] = {**binding, 'protocol': ep['config']['protocol'], 'config_digest': digest(ep['config'])}
    return {'version': 'composer-v2', 'roles': roles,
            'templates': {name: runtime.llm.templates.snapshot(name)
                          for name in ('R0-routing-v2', 'R1-sfw', 'R1-nsfw', 'R5')}}


def bind(runtime, frozen, role):
    expected = frozen['roles'][role]
    if expected is None:
        return None
    config = runtime.llm.config
    with config.lock:
        current = config.state['roles'][role]
        if current != {k: expected[k] for k in ('endpoint_id', 'model')}:
            raise ServiceError(503, 'prompt:frozen_dependencies_changed')
        ep = config._endpoint(current['endpoint_id'])
        if digest(ep['config']) != expected['config_digest']:
            raise ServiceError(503, 'prompt:frozen_dependencies_changed')
        return runtime.llm._bind(role, ep, current['model'])


SHORT = set('mid early recent general safe sensitive explicit cover sample text border landscape square frame'.split())
# R1 §3.5: literal rules are deterministic; unresolved semantic patterns are
# separately reported as review_required, never advertised as a semantic proof.
WORDS = '''artist name|signature|watermark|username|web address|artist logo|masterpiece|best quality|high quality|good quality|normal quality|amazing quality|great quality|worst quality|low quality|bad quality|aesthetic|displeasing|beautiful|absurdres|highres|lowres|hires|8k|4k|uhd|hdr|detailed|intricate details|sharp focus|newest|oldest|questionable|oil painting|watercolor|acrylic paint|gouache|ink|pastel|crayon|colored pencil|charcoal|traditional media|pencil sketch|digital media|digital painting|digital art|photo|photograph|3d|cg|cgi|render|mmd|illustration|artwork|concept art|paper cutout|collage|lineart|line art|outlines|outline|sketch|linework|bold lines|clean lines|monoline|flat color|flat colors|cel shading|anime coloring|soft shading|hard shadows|gradient|painterly|impasto|blending|airbrushed|textured|grainy|monochrome|greyscale|grayscale|limited palette|muted colors|pastel colors|vibrant colors|colorful|spot color|partially colored|sepia|duotone|film grain|halftone|screentones|screentone|dithering|noise|grain|paper texture|canvas texture|scanlines|pixel art|vector trace|vector art|low poly|voxel|chromatic aberration|cross-hatching|hatching|stippling|ambient occlusion|ray tracing|subsurface scattering|realistic shading|retro|vintage|oldschool|old-school|nostalgic|realistic|photorealistic|semi-realistic|hyperrealistic|super deformed|deformed|stylized|simple style|cartoon|comic|manga|webtoon|anime|manhwa|doujin|minimalism|art nouveau|art deco|ukiyo-e|sumi-e|impressionism|expressionism|cubism|surreal|surrealism|abstract|pop art|psychedelic|vaporwave|synthwave|lofi|kawaii|ghibli|disney|pixar|blurry|blur|bokeh|depth of field|soft focus|out of focus|tilt shift|defocus|lens flare|vignetting|vignette|fisheye|wide angle|telephoto|macro|light leaks|anamorphic|glow effect|sparkle effect|sunbeam filter|glitch|vhs artifacts|jpeg artifacts|compression artifacts|scan artifacts|pixelated|distortion|motion blur|speed lines|motion lines|emphasis lines|zoom layer|high contrast|low contrast|oversaturated|desaturated|saturated|color filter|tinted|warm filter|cool filter|black and white|vivid|dramatic lighting|cinematic|volumetric lighting|official art|promotional art|game cg|event cg|official style|third-party edit|cover page|scan|screencap|wallpaper|poster|bad id|bad pixiv id|bad twitter id|bad link|md5 mismatch|resolution mismatch|source smaller|artist request|character request|copyright request|commentary|translation request|translated|check translation|revision|duplicate|alternate version|speech bubble|thought bubble|english text|chinese text|japanese text|korean text|text focus|copyright name|logo|dated|letterboxed|pillarboxed|comic panel|multiple views|split screen|character name|stats|landscape orientation|portrait orientation|square orientation|vertical|horizontal|tall image|wide image|square image|square composition|aspect ratio|widescreen|wide screen'''.split('|')
OBJECTS = ('gradient hair', 'pastel pink dress', 'stained glass window', 'holding paintbrush', 'easel',
           'holding photo', 'border collie', 'gothic architecture', 'steampunk goggles',
           'vertical stripes', 'horizontal stripes', 'square pupils')
WORD_PATTERN = re.compile(r'(?<!\w)(?:' + '|'.join(re.escape(word.replace('-', ' ')) for word in WORDS) + r')(?!\w)')


def has(text, term):
    return bool(re.search(r'(?<!\w)' + re.escape(term) + r'(?!\w)', text))


def gate(entry, original, snapshot):
    issue = content_issue(entry, snapshot['catalog'], snapshot['policy'])
    if issue:
        return issue
    original_tags = {normalize_tag(t) for t in original['tags']}
    allowed = {t for t in OBJECTS if t in original_tags}
    for raw in entry['tags'] + [entry['natural_language'] or '']:
        if not raw:
            continue
        text = normalize_tag(raw).replace('-', ' ')
        is_tag = raw in entry['tags']
        if is_tag and (has(text, 'camera') or text.startswith(('no ', 'without ')) and text != 'no humans'):
            return 'forbidden_content'
        if is_tag and normalize_tag(raw) in SHORT:
            return 'forbidden_content'
        for exception in allowed:
            text = re.sub(r'(?<!\w)' + re.escape(exception.replace('-', ' ')) + r'(?!\w)', '', text)
        if WORD_PATTERN.search(text):
            return 'forbidden_content'
        if (re.search(r'\((?:medium|style|artist)\)|(?:artist|rating)\s*:|\byear \d{4}\b|\d+\s*[x:]\s*\d+|\b(?:score|source) ', text)
                or any(c in raw for c in '{}[]|@') or '::' in raw or re.search(r'\([^)]*:\s*[\d.]|\(\(|^\(|\b(?:BREAK|AND)\b', raw)):
            return 'forbidden_content'
        if re.search(r'\b(?:art by|drawn by|illustrated by|in the style of|inspired by)\b', text):
            return 'forbidden_content'
        if re.search(r'\b(?:brushstrokes?|rendering|dreamy|hazy background|chibi|gothic|steampunk|cyberpunk|painted|line work)\b', text):
            return 'review_required'
    nl = entry['natural_language'] or ''
    if nl and (re.search(r'\b(?:camera|not|without|never|no)\b', nl, re.I) or '\n' in nl or re.search(r'[^\x00-\x7f]', nl)):
        return 'review_required'
    if excluded(set(entry['tags']), snapshot['request']['plan']['exclude']):
        return 'excluded'
    return None


def validate(value, candidate, snapshot):
    if value['status'] != 'ok':
        return None if value.get('reason', '').strip() and len(value['reason']) <= 2048 else 'invalid_contract'
    options = snapshot['request']['options']
    tags = value['tags']
    canon = lambda t: snapshot['catalog']['entries'].get(normalize_tag(t), normalize_tag(t))
    original = {canon(t) for t in candidate['entry']['tags']}
    out, added, dropped = ([canon(t) for t in value['out_of_frame']], [canon(t) for t in value['added']],
                           [canon(t['tag']) for t in value['dropped']])
    final = [canon(t) for t in tags]
    hint = candidate['entry']['aspect'] or options['aspect_hint']
    nl = value['natural_language']
    if (any(t != normalize_tag(t) for t in tags) or len(set(final)) != len(final)
            or not final or not (final[0] == 'no humans' or re.fullmatch(r'\d+(?:girl|boy|girls|boys|other|others)', final[0]))
            or not options['tag_range'][0] <= len(tags) <= options['tag_range'][1]
            or hint is not None and value['aspect'] != hint
            or options['nl_enabled'] and (not nl or len(nl) > 4096 or not re.search('[a-zA-Z]', nl))
            or not options['nl_enabled'] and nl is not None
            or len(set(out + dropped)) != len(out + dropped) or len(set(added)) != len(added)
            or set(added) != set(final) - original or set(out + dropped) != original - set(final)
            or any(not r['reason'].strip() for r in value['dropped'])
            or any(snapshot['catalog']['categories'].get(t) in ('character', 'copyright') for t in added)
            or 'no humans' in final and any(re.fullmatch(r'\d+(?:girl|boy|girls|boys|other|others)', t) for t in final)
            or set(out + dropped) & set(final) or any(has(normalize_tag(nl or ' '), t) for t in out if nl)):
        return 'invalid_contract'
    try:
        entry = Entry(tags=tags, natural_language=nl, aspect=value['aspect'], rating=candidate['rating']).model_dump()
    except ValueError:
        return 'invalid_contract'
    return gate(entry, candidate['entry'], snapshot)


def features(candidate, snapshot):
    result = {}
    for tag in candidate['entry']['tags']:
        names = []
        if candidate['source'] == 'feature_pool':
            names = [name for d in snapshot['dictionaries'] if d['kind'] == 'features'
                     for name, rows in d['dimensions'].items()
                     if any(snapshot['catalog']['entries'].get(r['tag'], r['tag']) == tag for r in rows)]
        name = sorted(names)[0] if names else snapshot['catalog']['categories'].get(tag, 'content')
        result.setdefault(name, []).append(tag)
    return result


class Composer:
    def __init__(self, runtime, snapshot, cancel, before_call=lambda: None):
        self.runtime, self.snapshot, self.cancel = runtime, snapshot, cancel
        self.before_call = before_call
        self.frozen = snapshot['composer']
        if self.frozen['version'] not in ('composer-v1', 'composer-v2') or any(runtime.llm.templates.snapshot(k) != v
                for k, v in self.frozen['templates'].items()):
            raise ServiceError(503, 'prompt:frozen_dependencies_changed')

    def request(self, role, template, payload, budget, hint=''):
        self.before_call()
        binding = bind(self.runtime, self.frozen, role)
        template = self.frozen['templates'][template]
        sampling = {'max_tokens': 4096}
        if binding.protocol != 'anthropic_messages':
            sampling['temperature'] = 0.0
        request = LlmRequest(str(uuid4()), binding,
            self.frozen['templates']['R0' if self.frozen['version'] == 'composer-v1' else 'R0-routing-v2']['system_text'] + '\n' + template['system_text'] +
            '\nFinal response must match the supplied JSON schema, including the complete non-ok branch when refusing.' + hint,
            payload, template['response_schema'], cancel=self.cancel, sampling=sampling, repair_budget=budget)
        return asyncio.run(self.runtime.llm.complete_json(request)).value

    def compose(self, outcome, checkpoint):
        candidate = deepcopy(outcome['input'])
        options, fence = self.snapshot['request']['options'], self.snapshot['request']['fence']
        rating, verified = outcome.get('rating', candidate['rating']), outcome.get('verified', candidate['verified'])
        budget = RepairBudget(used=outcome.get('repair_used', 0))
        def save(phase):
            checkpoint({'phase': phase, 'rating': rating, 'verified': verified, 'repair_used': budget.used})
        def call(role, template, payload, hint=''):
            try:
                return self.request(role, template, payload, budget, hint)
            except ServiceError:
                checkpoint({'repair_used': budget.used})
                raise
        if not verified and fence['use_rating_model'] and outcome.get('phase') not in ('rated', 'r1'):
            if self.frozen['roles']['rating'] is None:
                raise ServiceError(503, 'prompt:rating_role_missing')
            save('rating')
            judged = call('rating', 'R5', {'tags': candidate['entry']['tags'],
                'natural_language': candidate['entry']['natural_language'], 'rating_verified': False,
                'rating_policy': 'nsfw' if rating in ('questionable', 'explicit') else 'sfw'})
            found = 'questionable' if judged.get('status') == 'refused_nsfw' else judged.get('rating')
            if found in RATINGS:
                rating = max([r for r in (rating, found) if r is not None], key=RATINGS.index)
                verified = True
            save('rated')
        route = 'nsfw' if rating in ('questionable', 'explicit') or not verified and fence['nsfwfallback'] else 'normal'
        def fenced():
            return fence['enabled'] and (rating is not None and RATINGS.index(rating) > RATINGS.index(fence['threshold'])
                                         or not verified and fence['nsfwfallback'])
        candidate.update(rating=rating, verified=verified, route=route)
        candidate['entry']['rating'] = rating
        if fenced():
            return {'status': 'fenced', 'reason': 'fenced'}
        def route_contract():
            role, template = ('prompt_nsfw', 'R1-nsfw') if route == 'nsfw' else ('prompt', 'R1-sfw')
            reason = ('nsfw_contract_missing' if template not in self.frozen['templates'] else
                      'role_empty' if self.frozen['roles'][role] is None else None)
            return role, template, reason
        role, template, reason = route_contract()
        reason = 'disabled' if not self.snapshot['request']['optimize'] else reason
        if reason:
            issue = gate(candidate['entry'], candidate['entry'], self.snapshot)
            return {'status': 'review_required' if issue == 'review_required' else 'failed' if issue else 'skipped',
                    'reason': issue or reason, 'result': None if issue else candidate,
                    **({'review_candidate': candidate} if issue == 'review_required' else {})}
        payload = {'features': features(candidate, self.snapshot), 'rating_verified': verified, 'rating_policy': 'nsfw' if route == 'nsfw' else 'sfw',
                   **options, 'aspect_hint': candidate['entry']['aspect'] or options['aspect_hint']}
        save('r1')
        value = call(role, template, payload)
        issue = validate(value, candidate, self.snapshot)
        if value['status'] == 'ok' and issue == 'invalid_contract' and budget.take():
            save('r1')
            value = call(role, template, payload,
                '\nReturn the required JSON contract. Check tag range, added/dropped accounting, NL and aspect_hint. Do not repeat invalid fields.')
            issue = validate(value, candidate, self.snapshot)
        if value['status'] != 'ok' and issue:
            return {'status': 'failed', 'reason': issue}
        if value['status'] == 'refused_nsfw':
            rating = max([r for r in (rating, 'questionable') if r is not None], key=RATINGS.index)
            verified = True
            candidate.update(rating=rating, verified=True, route='nsfw')
            candidate['entry']['rating'] = rating
            was_nsfw, route = route == 'nsfw', 'nsfw'
            role, template, reason = route_contract()
            issue = 'fenced' if fenced() else gate(candidate['entry'], candidate['entry'], self.snapshot)
            if was_nsfw or reason or issue:
                return {'status': 'fenced' if issue == 'fenced' else 'failed' if issue else 'skipped',
                        'reason': issue or reason or 'upstream_refused', 'result': None if issue else candidate}
            # One explicit reroute, sharing the original repair budget. No
            # recursive refusal handling or fallback to the ordinary role.
            payload.update(rating_policy='nsfw', rating_verified=True)
            save('r1')
            value = call(role, template, payload)
            issue = validate(value, candidate, self.snapshot)
            if value['status'] == 'ok' and issue == 'invalid_contract' and budget.take():
                save('r1')
                value = call(role, template, payload, '\nCorrect the JSON contract and exact added/dropped accounting.')
                issue = validate(value, candidate, self.snapshot)
            if value['status'] != 'ok':
                return {'status': 'failed', 'reason': issue or ('upstream_refused' if value['status'] == 'refused_nsfw' else 'invalid_input')}
        if value['status'] == 'invalid_input' or issue and issue != 'review_required':
            return {'status': 'failed', 'reason': issue or 'invalid_input'}
        candidate['entry'] = Entry(tags=value['tags'], natural_language=value['natural_language'],
                                  aspect=candidate['entry']['aspect'] or value['aspect'], rating=rating).model_dump()
        candidate['entry']['tags'] = [self.snapshot['catalog']['entries'].get(t, t) for t in candidate['entry']['tags']]
        # Reapply the immutable D1 lower bound to newly added tags.
        known = [r['rating'] for r in self.snapshot['policy']['keywords'] if r['tag'] in candidate['entry']['tags']]
        rating = max(known + ([rating] if rating else []), key=RATINGS.index) if known or rating else None
        candidate.update(rating=rating)
        candidate['entry']['rating'] = rating
        if fenced():
            return {'status': 'fenced', 'reason': 'fenced'}
        if issue == 'review_required':
            return {'status': 'review_required', 'reason': issue, 'review_candidate': candidate, 'reply': value}
        return {'status': 'ok', 'reason': None, 'result': candidate, 'reply': value}
