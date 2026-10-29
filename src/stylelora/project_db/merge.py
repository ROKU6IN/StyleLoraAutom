"""Read-only F01 merge semantics, including evidence-age removal boundaries."""

from dataclasses import dataclass

from .errors import UnsupportedSorter, SorterContractViolation, ValidationError
from .types import ExportSchemeInput
from .validation import LAYERS, confidence, normalize_tag
from .repositories.annotations import get_current
from .repositories.schemes import require_executable_export


@dataclass(frozen=True)
class MergedCaption:
    tags: tuple[str, ...]
    natural_language: str | None


def evidence_view(db, image_id, config=None):
    config = config or ExportSchemeInput("internal").caption_config_json
    layers = config.get("tag_layers", list(LAYERS))
    priorities = config.get("layer_priority", ["manual", "rule", "llm", "wd", "original"])
    if len(set(layers)) != len(layers) or any(layer not in LAYERS for layer in layers):
        raise ValidationError("Invalid tag layer selection")
    if len(set(priorities)) != len(priorities) or set(priorities) != set(LAYERS):
        raise ValidationError("Layer priority must contain every layer once")
    threshold = config.get("wd_min_confidence", 0.0)
    confidence(threshold)
    if threshold is None:
        raise ValidationError("WD threshold cannot be null")
    null_mode = config.get("wd_null_confidence", "exclude")
    if null_mode not in ("include", "exclude"):
        raise ValidationError("Invalid WD null confidence policy")
    current = {layer: get_current(db, image_id, layer, "tags") for layer in layers}
    cutoffs = {}
    for layer in ("manual", "rule"):
        row = current.get(layer)
        if row:
            for removal in row.params_json["merge_meta"]["removals"]:
                key = normalize_tag(removal["tag"])
                cutoffs[key] = max(cutoffs.get(key, 0), removal["cutoff_annotation_id"])
    order, evidence = [], {}
    for layer in priorities:
        row = current.get(layer)
        if not row:
            continue
        for tag in row.content:
            key = normalize_tag(tag.tag)
            origins = row.params_json["merge_meta"]["tag_origins"].get(key, []) if layer in ("manual", "rule") else [
                {"layer": layer, "annotation_id": row.annotation_id, "conf": tag.conf}
            ]
            valid = []
            for origin in origins:
                if origin["annotation_id"] <= cutoffs.get(key, 0):
                    continue
                if origin["layer"] == "wd":
                    conf = origin["conf"]
                    if conf is None and null_mode != "include":
                        continue
                    if conf is not None and conf < threshold:
                        continue
                if origin not in valid:
                    valid.append(origin)
            if valid:
                if key not in evidence:
                    order.append(key)
                    evidence[key] = []
                for origin in valid:
                    if origin not in evidence[key]:
                        evidence[key].append(origin)
    for origins in evidence.values():
        origins.sort(key=lambda x: (x["annotation_id"], x["layer"], -1 if x["conf"] is None else x["conf"]))
    return tuple(order), evidence


def merge_caption(db, image_id, *, config=None, trigger_word="", sorter=None):
    config = config or ExportSchemeInput("default").caption_config_json
    order, _ = evidence_view(db, image_id, config)
    content = list(order)
    trigger = normalize_tag(trigger_word) if trigger_word.strip() else None
    if trigger:
        content = [tag for tag in content if tag != trigger]
    tag_sort = config.get("tag_sort", "layer_first")
    if tag_sort == "prompt_pyramid":
        if sorter is None:
            raise UnsupportedSorter("prompt_pyramid adapter is not available")
        try:
            result = sorter(tuple(content))
            if isinstance(result, (str, bytes)):
                raise TypeError('A sorter must return a sequence of tags')
            sorted_tags = tuple(result)
        except (TypeError, ValueError) as exc:
            raise SorterContractViolation('Invalid sorter result') from exc
        if (any(not isinstance(tag, str) for tag in sorted_tags)
                or len(sorted_tags) != len(content) or set(sorted_tags) != set(content)):
            raise SorterContractViolation("A sorter must only reorder existing normalized tags")
        content = list(sorted_tags)
    elif tag_sort != "layer_first":
        raise UnsupportedSorter("Unknown tag sorter")
    if trigger:
        content.insert(0, trigger)
    nl = None
    if config.get("include_nl", False):
        if config.get("nl_priority", ["manual", "llm", "original"]) != ["manual", "llm", "original"]:
            raise ValidationError("Natural language priority is manual, llm, original")
        for layer in ("manual", "llm", "original"):
            row = get_current(db, image_id, layer, "nl")
            if row and row.content.strip():
                nl = row.content.strip()
                break
    return MergedCaption(tuple(content), nl)


def preview_caption(db, image_id, scheme, *, trigger_word="", sorter=None):
    require_executable_export(scheme)
    return merge_caption(db, image_id, config=scheme.caption_config_json, trigger_word=trigger_word, sorter=sorter)
