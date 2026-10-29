"""Versioned cache identities from effective parameters, never endpoint secrets."""

from .validation import choice, digest, object_json, project_data, sha256, text

# Version this projection when a field changes whether a result is reusable.
SCHEDULING_ONLY = frozenset({
    "concurrency", "timeout", "retries", "retry_count", "batch_size", "job_id",
    "request_id", "created_at", "rated_at",
})


def make_cache_key(*, source, type, image_sha256, model, protocol, skill_hash,
                   actual_params, route, keyword_hash, evidence_digest=None):
    choice(source, ("wd", "llm"), "cache layer")
    choice(type, ("tags", "nl"), "cache target")
    if source == "wd" and type != "tags":
        from .errors import ValidationError
        raise ValidationError("WD cache only supports tags")
    sha256(image_sha256)
    text(model, "actual model", nonempty=True)
    text(protocol, "protocol", nonempty=True)
    text(skill_hash, "skill hash", nonempty=True)
    text(keyword_hash, "keyword hash", nonempty=True)
    object_json(actual_params)
    project_data(actual_params)
    projected = {key: value for key, value in actual_params.items() if key not in SCHEDULING_ONLY}
    payload = {
        "schema_version": 1, "source": source, "type": type, "image_sha256": image_sha256,
        "actual_model": model, "protocol": protocol, "skill_hash": skill_hash,
        "actual_params": projected, "route": route, "keyword_hash": keyword_hash,
        "evidence_digest": evidence_digest,
    }
    project_data(payload)
    return "annotation-cache-v1:" + digest(payload)
