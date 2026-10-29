"""Deterministic finite-pool sampling with exact quotas and bounded search."""
from collections import Counter
from copy import deepcopy
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import itertools
import math
import random
import re

from ..project_db.validation import encode, normalize_tag
from ..runtime.errors import ServiceError
from ..vision.policy import RATINGS


class SamplingStopped(Exception):
    pass


def cell_id(names, values):
    return encode(list(zip(names, values)))


def excluded(tags, predicates):
    return any(set(p['all_tags']) <= tags and not set(p['none_tags']) & tags for p in predicates)


def coverage_cells(plan):
    names = plan['primary_dimensions']
    universe = {v for values in plan['cell_values'].values() for v in values}
    predicates = [p for p in plan['exclude'] if set(p['all_tags'] + p['none_tags']) <= universe]
    return [cell_id(names, values) for values in itertools.product(*(plan['cell_values'][name] for name in names))
            if not excluded(set(values), predicates)]


def quotas(plan, rng):
    cells = coverage_cells(plan)
    if set(plan['cell_weights']) - set(cells):
        raise ServiceError(422, 'coverage:invalid_cell_weight')
    if not cells or plan['target_count'] < len(cells):
        return {key: 1 for key in cells}, False
    ordered = sorted(cells)
    rng.shuffle(ordered)
    rest = plan['target_count'] - len(cells)
    weight = {key: plan['cell_weights'].get(key, 1) for key in cells}
    total = sum(weight.values())
    result = {key: 1 + rest * weight[key] // total for key in cells}
    remainders = sorted(ordered, key=lambda key: -(rest * weight[key] % total))
    for key in remainders[:plan['target_count'] - sum(result.values())]:
        result[key] += 1
    return result, True


def constraint_bounds(plan):
    result = {}
    for constraint in plan['constraints']:
        tag = constraint['tag']
        lower, upper = result.get(tag, (0, plan['target_count']))
        for field, rounding, mode in (('minimum_ratio', ROUND_CEILING, max), ('maximum_ratio', ROUND_FLOOR, min)):
            value = constraint[field]
            if value is not None:
                count = int((Decimal(str(value)) * plan['target_count']).to_integral_value(rounding=rounding))
                if mode is max:
                    lower = max(lower, count)
                else:
                    upper = min(upper, count)
        lower = max(lower, constraint['minimum_count'] or 0)
        upper = min(upper, constraint['maximum_count'] if constraint['maximum_count'] is not None else plan['target_count'])
        result[tag] = (lower, upper)
    return result


def classify(tags, plan):
    values = []
    for name in plan['primary_dimensions']:
        found = [value for value in plan['cell_values'][name] if value in tags]
        if len(found) != 1:
            return None, 'ambiguous' if len(found) > 1 else 'unclassified'
        values.append(found[0])
    return cell_id(plan['primary_dimensions'], values), None


def canonical_entry(row, catalog):
    row = deepcopy(row)
    row['tags'] = list(dict.fromkeys(catalog['entries'].get(t, t) for t in row['tags']))
    return row


def content_issue(row, catalog, policy):
    if any(catalog['categories'].get(t) == 'artist' for t in row['tags']):
        return 'forbidden_content'
    corpus = normalize_tag(' '.join(row['tags']) + ' ' + (row['natural_language'] or ''))
    for term in policy['blocked_terms']:
        if re.search(r'(?<!\w)' + re.escape(term) + r'(?!\w)', corpus) or any('\u4e00' <= c <= '\u9fff' for c in term) and term in corpus:
            return 'forbidden_content'
    if re.search(r'\b(?:in the style of|style of|by artist|inspired by)\b', corpus):
        return 'review_required'
    # Do not interpret weighted/control prompt syntax hidden in natural language.
    nl = row['natural_language'] or ''
    if any(c in nl for c in '{}[]\\|') or '::' in nl or re.search(r'\([^)]*:\s*\d|\bBREAK\b', nl):
        return 'forbidden_content'
    return None


def candidate_pool(snapshot, rng, limit, stop):
    dictionaries, catalog = snapshot['dictionaries'], snapshot['catalog']
    prompts, dimensions, diagnostic = {}, {}, Counter()
    for dictionary in sorted(dictionaries, key=lambda v: v['dictionary_id']):
        stop()
        if dictionary['kind'] == 'prompts':
            for row in dictionary['entries']:
                entry = canonical_entry(row, catalog)
                prompts[encode(entry)] = entry
        else:
            for name, rows in dictionary['dimensions'].items():
                values = dimensions.setdefault(name, {})
                for row in rows:
                    key = catalog['entries'].get(row['tag'], row['tag'])
                    previous = values.get(key)
                    if previous:
                        known = [v for v in (previous['rating'], row['rating']) if v is not None]
                        previous['rating'] = max(known, key=RATINGS.index) if known else None
                        previous['verified'] &= row['rating'] is not None
                    else:
                        values[key] = {'tag': key, 'rating': row['rating'], 'verified': row['rating'] is not None}
    dictionary_pool = [(row, row['rating'] is not None, 'dictionary_pool') for _, row in sorted(prompts.items())]
    feature_pool, complete = [], True
    if dimensions:
        names = sorted(dimensions)
        rows = [sorted(dimensions[name].values(), key=lambda r: r['tag']) for name in names]
        total = math.prod(map(len, rows))
        if total <= limit:
            indices = range(total)
        else:
            complete = False
            chosen = set()
            while len(chosen) < limit:
                chosen.add(rng.randrange(total))
            indices = sorted(chosen)
        for index in indices:
            stop()
            selected = []
            for values in reversed(rows):
                selected.append(values[index % len(values)])
                index //= len(values)
            selected.reverse()
            if any(row['tag'] == 'no humans' for row in selected):
                selected = [row for name, row in zip(names, selected) if name not in snapshot['request']['plan']['no_humans_excludes']]
            known = [r['rating'] for r in selected if r['rating'] is not None]
            entry = {'tags': list(dict.fromkeys(r['tag'] for r in selected)), 'natural_language': None,
                     'aspect': None, 'rating': max(known, key=RATINGS.index) if known else None}
            feature_pool.append((entry, all(r['verified'] for r in selected), 'feature_pool'))
    # Collapse identical candidates in a pool before giving any random priority.
    # Extra dictionary copies or suppressed no-human dimensions cannot add weight.
    pools = {}
    for entry, verified, source in dictionary_pool + feature_pool:
        key = source, encode(entry)
        if key in pools:
            verified = verified and pools[key][1]
        pools[key] = (entry, verified, source)
    prioritized = []
    for key in sorted(pools):
        entry, verified, source = pools[key]
        priority = -math.log(max(rng.random(), 1e-300)) / snapshot['request']['source_weights'][source]
        prioritized.append((priority, encode(entry), entry, verified, source))
    prioritized.sort(key=lambda row: row[:2])
    if len(prioritized) > limit:
        complete = False
        diagnostic['candidate_limit'] = len(prioritized) - limit
        prioritized = prioritized[:limit]
    candidates, seen = [], set()
    keywords = {v['tag']: v['rating'] for v in snapshot['policy']['keywords']}
    fence = snapshot['request']['fence']
    for _, _, entry, verified, source in prioritized:
        stop()
        tags = set(entry['tags'])
        if snapshot.get('content_gate') == 'r1-v1' and not snapshot['request']['optimize']:
            from .composer import gate
            issue = gate(entry, entry, snapshot)
        else:
            issue = content_issue(entry, catalog, snapshot['policy'])
        if issue:
            diagnostic[issue] += 1
            continue
        known = [keywords[tag] for tag in tags if tag in keywords]
        if entry['rating'] is not None:
            known.append(entry['rating'])
        rating = max(known, key=RATINGS.index) if known else None
        verified = verified or (all(t in keywords for t in tags) if snapshot.get('content_gate') else any(t in keywords for t in tags))
        fallback = (not verified if snapshot.get('content_gate') else rating is None) and fence['nsfwfallback']
        route = 'nsfw' if rating in ('questionable', 'explicit') or fallback else 'normal'
        if fence['enabled'] and (rating is not None and RATINGS.index(rating) > RATINGS.index(fence['threshold'])
                                 or fallback and not fence['use_rating_model']):
            diagnostic['fenced'] += 1
            continue
        if excluded(tags, snapshot['request']['plan']['exclude']):
            diagnostic['excluded'] += 1
            continue
        key = tuple(sorted(tags))
        if key in seen:
            diagnostic['duplicate'] += 1
            continue
        seen.add(key)
        candidates.append({'entry': entry, 'rating': rating, 'verified': verified, 'route': route, 'source': source})
    return candidates, complete, diagnostic


def solve(groups, targets, bounds, node_limit, stop):
    """Iterative combination search; no Python recursion limit at 10,000 items."""
    names = sorted(groups, key=lambda name: (len(groups[name]) - targets[name], len(groups[name]), name))
    candidates, slots, remaining_slots = [], [], []
    # Try a small quota-scaled lower-bound prefix, then retain the random/source
    # priority. Promoting *every* matching candidate would turn a 15% minimum
    # into an accidental preference for 100% of the batch.
    supply = {tag: sum(tag in row['entry']['tags'] for rows in groups.values() for row in rows) for tag in bounds}
    total = sum(targets.values())
    for name in names:
        start = len(candidates)
        needed = {tag: (lower * targets[name] + total - 1) // total for tag, (lower, _) in bounds.items()}
        ranked = sorted(groups[name], key=lambda row: -sum(bounds[tag][0] / max(1, supply[tag])
                        for tag in bounds if tag in row['entry']['tags']))
        prefix, selected = [], set()
        for row in ranked:
            if any(needed[tag] > 0 and tag in row['entry']['tags'] for tag in needed):
                prefix.append(row)
                selected.add(id(row))
                for tag in needed:
                    needed[tag] -= tag in row['entry']['tags']
        ordered = prefix + [row for row in groups[name] if id(row) not in selected]
        candidates.extend(ordered)
        slots.extend([(start, len(candidates))] * targets[name])
        remaining_slots.extend(range(targets[name], 0, -1))
    tags = list(bounds)
    suffix = [[0] * (len(candidates) + 1) for _ in tags]
    for c, tag in enumerate(tags):
        for i in range(len(candidates) - 1, -1, -1):
            suffix[c][i] = suffix[c][i + 1] + (tag in candidates[i]['entry']['tags'])
    chosen, counts, next_index = [], [0] * len(tags), [slots[0][0] if slots else 0]
    nodes, best = 0, []
    while slots and next_index:
        stop()
        depth = len(chosen)
        start, end = slots[depth]
        index = max(start, next_index[depth])
        # A group must leave enough later candidates for all its remaining slots.
        remaining_group = remaining_slots[depth]
        if index > end - remaining_group:
            next_index.pop()
            if not chosen:
                break
            old = chosen.pop()
            for c, tag in enumerate(tags):
                counts[c] -= tag in candidates[old]['entry']['tags']
            continue
        next_index[depth] = index + 1
        nodes += 1
        if nodes > node_limit:
            return [candidates[i] for i in best], False, nodes
        row_tags = candidates[index]['entry']['tags']
        updated = [counts[c] + (tag in row_tags) for c, tag in enumerate(tags)]
        left = len(slots) - depth - 1
        if any(updated[c] > bounds[tag][1] or updated[c] + min(left, suffix[c][index + 1]) < bounds[tag][0] for c, tag in enumerate(tags)):
            continue
        chosen.append(index)
        counts = updated
        if len(chosen) > len(best):
            best = chosen[:]
        if len(chosen) == len(slots):
            return [candidates[i] for i in chosen], True, nodes
        following = slots[len(chosen)][0]
        next_index.append(index + 1 if following == start else following)
    return [candidates[i] for i in best], True, nodes


def frozen_pool(snapshot, stop=lambda: None):
    rng = random.Random(snapshot['request']['plan']['random_seed'])
    if snapshot['request']['coverage_enabled']:
        quotas(snapshot['request']['plan'], rng)
    rows, complete, diagnostic = candidate_pool(snapshot, rng, 10000, stop)
    return [{**row, 'pool_index': i} for i, row in enumerate(rows)], complete, diagnostic


def sample(snapshot, *, candidate_limit=10000, node_limit=100000, stop=lambda: None, pool=None):
    request = snapshot['request']
    plan = request['plan']
    rng = random.Random(plan['random_seed'])
    targets, feasible = quotas(plan, rng) if request['coverage_enabled'] else ({'random': plan['target_count']}, True)
    bounds = constraint_bounds(plan)
    candidates, complete, diagnostic = candidate_pool(snapshot, rng, candidate_limit, stop) if pool is None else deepcopy(pool)
    groups = {key: [] for key in targets}
    for row in candidates:
        key, issue = classify(set(row['entry']['tags']), plan) if request['coverage_enabled'] else ('random', None)
        if issue or key not in groups:
            diagnostic[issue or 'excluded_cell'] += 1
        else:
            row['cell'] = key
            groups[key].append(row)
    capacity = all(len(groups[key]) >= count for key, count in targets.items())
    valid_bounds = all(lower <= upper for lower, upper in bounds.values())
    if feasible and capacity and valid_bounds:
        chosen, exhausted, nodes = solve(groups, targets, bounds, node_limit, stop)
    else:
        chosen, exhausted, nodes = [], True, 0
    satisfied = feasible and valid_bounds and len(chosen) == plan['target_count']
    impossible = not feasible or not valid_bounds or complete and exhausted
    status = 'satisfied' if satisfied else 'unsatisfiable' if impossible else 'search_exhausted'
    # Stable final output follows candidate priority, independent of solver group order.
    priority = {id(row): index for index, row in enumerate(candidates)}
    chosen.sort(key=lambda row: priority[id(row)])
    actual = Counter(row['cell'] for row in chosen)
    counts = {tag: sum(tag in row['entry']['tags'] for row in chosen) for tag in bounds}
    items = [{'seq': index + 1, 'prompt': ', '.join(row['entry']['tags']) +
              (', ' + row['entry']['natural_language'] if row['entry']['natural_language'] else ''),
              'aspect': row['entry']['aspect']} for index, row in enumerate(chosen)]
    catalog = snapshot['catalog']
    warnings = Counter()
    for row in chosen:
        if not row['verified']:
            warnings['rating_unverified'] += 1
        if any(tag not in catalog['entries'] for tag in row['entry']['tags']):
            warnings['unknown_tag_may_parse_as_nl'] += 1
        if any(normalize_tag(part) in catalog['entries'] for part in (row['entry']['natural_language'] or '').split(',') if part.strip()):
            warnings['nl_fragment_may_parse_as_tag'] += 1
    return {'status': status, 'ready': satisfied, 'requested': plan['target_count'], 'available': len(candidates),
            'count': len(chosen), 'cell_targets': targets, 'cell_counts': dict(actual), 'constraint_counts': counts,
            'constraint_bounds': {tag: list(pair) for tag, pair in bounds.items()},
            'deficits': {key: max(0, count - actual[key]) for key, count in targets.items()},
            'constraint_deficits': {tag: max(0, bounds[tag][0] - counts[tag]) for tag in bounds},
            'diagnostics': dict(diagnostic), 'warnings': dict(warnings), 'complete_pool': complete,
            'search_nodes': nodes, 'source_counts': dict(Counter(row['source'] for row in chosen)),
            'outcomes': [{'index': index, 'status': 'ok', **row} for index, row in enumerate(chosen)], 'items': items}
