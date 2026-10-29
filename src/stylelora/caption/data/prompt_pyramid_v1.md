# R4 local sorting contract — pyramid-local-v1

This is an application contract for a deterministic Python adapter, not an LLM prompt.
It implements the order in docs/F09-F10 §8 and docs/03; it does not execute or copy
the upstream TagCleaner cleanup workflow, or R1 composition/refinement rules.

Input is the normalized, deduplicated content-tag sequence from F01. The trigger
is removed before sorting and inserted once at the front by the assembler.
The output must be an exact permutation of the input strings. No additions,
deletions, renaming, alias substitution, deduplication, contradiction resolution,
artist removal, rating inference, subject ownership inference or model calls.
Blocklist filtering remains a separate existing export step. Natural language,
original image bytes, database annotations and their provenance are unchanged.

Stable group order: subject, action_expression, clothing, composition, scene,
lighting, unclassified. Within every group, preserve the input relative order.
For multi-person tags this is a global presentation order, not character binding.

Classification is conservative and exact:

1. A frozen catalog alias may be used only to look up a category. Keep the input
   string in the output, including aliases that coexist with canonical tags.
2. Explicit catalog categories pyramid:subject, pyramid:action_expression,
   pyramid:clothing, pyramid:composition, pyramid:scene and pyramid:lighting
   select that group. Catalog character selects subject.
3. Absent/general category uses the packaged exact-tag fixture. Unknown tags and
   other categories (including artist, copyright, meta and unknown category names)
   remain unclassified. Do not infer categories from substrings or parentheses.
4. Unknown tags are kept, stably placed at the end and counted in preview/report.
   The finite fixture is not an exhaustive Danbooru taxonomy or a semantic model.

Read and validate the packaged fixture and this contract for each export snapshot.
Freeze category/alias mappings. Record version, fixture SHA-256, contract SHA-256,
catalog revision and catalog content digest in the export plan/report. Any changed
input invalidates the previous plan; missing/malformed resources disable this
sorter before output directories are created. Do not silently use layer_first.
