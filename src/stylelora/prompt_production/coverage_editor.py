"""Lossless browser form projection and pure quota planning; never sample a pool."""
from copy import deepcopy
import random
import re
from typing import Annotated, Literal

from pydantic import Field, ValidationError

from ..project_db.validation import encode
from ..runtime.errors import ServiceError
from .coverage import MAX_TEMPLATE, Template, parse, view
from .sampling import constraint_bounds, coverage_cells, quotas
from .types import Predicate, Strict

NumericText = Annotated[str, Field(max_length=64)]


class EditorConstraint(Strict):
    dimension: str = Field(max_length=64)
    tag: str = Field(max_length=200)
    minimum_ratio: NumericText = ''
    maximum_ratio: NumericText = ''
    minimum_count: NumericText = ''
    maximum_count: NumericText = ''


class EditorPlan(Strict):
    schema_version: int = Field(default=1, ge=1, le=1)
    algorithm_version: Literal['coverage-v1'] = 'coverage-v1'
    primary_dimensions: list[str] = Field(min_length=2, max_length=3)
    cell_values: dict[str, list[str]]
    cell_weights: dict[str, NumericText] = Field(default_factory=dict, max_length=10000)
    exclude: list[Predicate] = Field(default_factory=list, max_length=100)
    no_humans_excludes: list[str] = Field(default_factory=list, max_length=20)
    target_count: NumericText
    random_seed: NumericText = ''
    constraints: list[EditorConstraint] = Field(default_factory=list, max_length=100)


class EditorTemplate(Strict):
    schema_version: int = Field(default=1, ge=1, le=1)
    name: str = Field(max_length=200)
    plan: EditorPlan


def number(value, *, optional=False, ratio=False):
    if optional and value == '':
        return None
    pattern = r'(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?' if ratio else r'\d+'
    if not value.isascii() or re.fullmatch(pattern, value) is None:
        raise ValueError('Numeric text required')
    return float(value) if ratio else int(value)


def from_editor(value):
    try:
        template = EditorTemplate.model_validate(value).model_dump()
        plan = template['plan']
        plan['random_seed'] = number(plan['random_seed'], optional=True)
        plan['target_count'] = number(plan['target_count'])
        plan['cell_weights'] = {key: number(weight) for key, weight in plan['cell_weights'].items()}
        for constraint in plan['constraints']:
            for key in ('minimum_ratio', 'maximum_ratio', 'minimum_count', 'maximum_count'):
                constraint[key] = number(constraint[key], optional=True, ratio=key.endswith('ratio'))
        template = Template.model_validate(template).model_dump()
        if len(encode(template).encode('utf-8')) > MAX_TEMPLATE:
            raise ServiceError(413, 'coverage:template_too_large')
        return template
    except (ValueError, TypeError, KeyError, OverflowError, ValidationError):
        raise ServiceError(422, 'coverage:invalid_editor') from None


def editor_view(template):
    form = deepcopy(template)
    plan = template['plan']
    for key in ('random_seed', 'target_count'):
        form['plan'][key] = '' if plan[key] is None else str(plan[key])
    form['plan']['cell_weights'] = {key: str(value) for key, value in plan['cell_weights'].items()}
    for constraint in form['plan']['constraints']:
        for key in ('minimum_ratio', 'maximum_ratio', 'minimum_count', 'maximum_count'):
            constraint[key] = '' if constraint[key] is None else str(constraint[key])
    active = coverage_cells(plan)
    issues, targets = [], None
    if not active:
        issues.append('coverage:all_cells_excluded')
    elif plan['target_count'] < len(active):
        issues.append('coverage:target_below_cells')
    if set(plan['cell_weights']) - set(active):
        issues.append('coverage:excluded_cell_weight')
    elif active and plan['target_count'] >= len(active) and plan['random_seed'] is not None:
        targets, _ = quotas(plan, random.Random(plan['random_seed']))
    bounds = constraint_bounds(plan)
    if any(lower > upper for lower, upper in bounds.values()):
        issues.append('coverage:contradictory_constraints')
    return {**view(template), 'form': form, 'layout': {
        'active_cells': active, 'cell_targets': targets, 'issues': issues,
        'constraint_bounds': {key: list(value) for key, value in bounds.items()},
        'seed_required': plan['random_seed'] is None}}


def open_editor(document):
    return editor_view(parse(document))


def preview_editor(value):
    return editor_view(from_editor(value))
