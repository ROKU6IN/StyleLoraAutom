"""F22 diagnostics and explicit existing-tag acknowledgements."""
from ..exchange.errors import ExchangeError
from ..project_db.validation import digest
from .plain import validate_plain_tag
from .tokenizer import tokenize_prompt


def validate_trigger(trigger, style_prompts=(), *, catalog=None):
    issues = []
    plain = True
    try:
        normalized = validate_plain_tag(trigger, trigger=True)
    except ExchangeError:
        plain = False
        normalized = trigger.strip()
        issues.append({'code':'trigger:invalid','severity':'error'})
    prompts = tuple(style_prompts)
    normalized = ' '.join(normalized.split())
    if normalized and any(normalized in ' '.join(prompt.lower().replace('_',' ').split()) for prompt in prompts):
        issues.append({'code':'trigger:in_style_prompt','severity':'error'})
    syntax_uncertain = any(any(t.reason for t in tokenize_prompt(prompt, catalog=catalog)) for prompt in prompts if prompt.strip())
    known = catalog.lookup(normalized) if catalog and plain else None
    if known:
        issues.append({'code':'trigger:existing_tag','severity':'warning','canonical':known})
    if not catalog or not catalog.entries or not catalog.declared_complete or syntax_uncertain:
        issues.append({'code':'trigger:unverifiable','severity':'warning'})
    status = 'invalid' if any(i['severity']=='error' for i in issues) else 'existing_tag' if known else 'unverifiable' if issues else 'valid'
    content = {'trigger':normalized,'catalog_revision':catalog.revision if catalog else None,
               'style_prompts_digest':digest(prompts),'issues':issues,'status':status}
    checksum = digest(content)
    return {**content,'digest':checksum,'override_digest':checksum if known and status!='invalid' else None,
            'can_override':bool(known and status!='invalid')}
