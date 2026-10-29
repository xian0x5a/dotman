---
status: open
---

# YAML output by text splicing (#167)

## Goal

`dotman transform yaml` writes the output document by editing the target
document's text: unchanged keys keep their bytes, and only keys whose data
changed are cut, replaced or added.

## Intention

YAML writes must keep the user's comments, anchors, merge keys and spelling.
The earlier design edited ruamel's round-trip object graph and dumped it
whole. Three review rounds kept finding comments lost or moved around aliases
and `<<` lines, because ruamel stores a comment on the node *before* it, and
every dump renormalised untouched values (`~`, `TRUE`, `%YAML 1.1`).

## Scope & Constraints

- Branch `feat/yaml-round-trip`, worktree `.worktrees/issue-167`.
- `ruamel.yaml>=0.19.1` stays the only YAML dependency: its parse events give
  every key, value and alias an exact text span, and its round-trip dumper
  renders changed values.
- YAML 1.2 values; duplicate keys and second documents are errors.
- Probes outside pytest isolate HOME and XDG dirs (see `AGENTS.md`).

## Design

1. Parse the target text twice: ruamel round-trip load (values) and ruamel
   parse events (spans). Zip them by walking mappings in text order.
2. Diff the loaded mapping against the desired data, key by key with
   type-strict keys and values:
   - missing key: delete its lines, its head comment and its deeper-indented
     trailing comments;
   - new key: render `key: value` and insert after the mapping's last entry;
   - changed block mapping: recurse;
   - any other change: replace the text after the key's colon with the
     rendered value.
3. Comment ownership: comment lines directly above a key (no blank line,
   indented no deeper than the key) belong to it; an end-of-line comment
   belongs to its line; comment lines after a value indented deeper than its
   key belong to that value. Everything else stays.
4. Re-render the nearest enclosing mapping value instead when the change
   cannot be made by lines: flow collections, sequences, a first key that
   shares its line with a `- ` dash, a mapping left empty, a deleted key a `<<`
   merge provides. At the root this renders the whole document body.
5. Anchors: a removed or replaced span drops the anchors defined in it; each
   alias to them outside the edits gets the desired value written out.
6. Shared values: edit, re-parse, diff again until the diff is empty. An edit
   inside an anchored mapping that changed an alias or `<<` receiver which
   must keep its value is fixed in the next round (alias written out, or an
   own key overriding the merged one). A round that makes no progress, or a
   result whose data differs from the desired data, raises.
7. Rendered fragments come from `tree_copy` (no anchors, merges flattened),
   dumped with the detected layout; trailing comment lines are dropped since
   they introduce the source document's next key.

## Work Plan

1. Replace the graph-editing code in `src/dotman/transforms/yaml.py` with the
   splicer (mappings, anchors, merges). Keep loading, `plain_value`, layout
   detection, representer, compare reuse.
2. Run existing YAML tests; adjust tests that pinned normalisation of
   untouched values.
3. Block sequences: item-level splicing (later layer, same PR if small).
4. Docs (`docs/cli.md`) and skill note.
5. Independent tests, independent review, full suite, PR.

## Validation

- `uv run pytest tests/test_yaml_transform.py tests/test_mapping_transforms.py tests/test_yaml_root_cli.py`
- Full suite once before the PR.

## Progress

- 2026-10-28: design accepted by user (text splicing on ruamel parse events,
  head-comment rule). Implementation started.
- 2026-10-28: splicer for mappings, anchors and merges (6a12b38); block
  sequences item by item (b19b81f); docs and skill note updated. Existing
  164 YAML/mapping tests pass. Next: independent tests and review.

## Surprises & Discoveries

- ruamel parse events: an empty value's span sits at the next token, possibly
  after comment lines; a block scalar's span includes its trailing blank
  lines; a block collection's end event sits at the next token. Spans are
  derived from children and trimmed instead.

## Decisions

- The compare file is the splice target when it holds a mapping: it is the
  file output replaces (repo copy on capture, live file on render), so the
  repo's notes survive a capture. Base, then overlay (merge, no base) follow.
- Sequence items are matched with `difflib.SequenceMatcher` on the `repr` of
  plain values; a non-match only costs a less minimal edit.
- Whole-document loads are cached by text (ruamel's `<<` handling is
  quadratic); nothing mutates a loaded document.

- Text splicing over patching the ruamel graph: weighted 3.95 vs 2.75
  (fidelity 35, robustness 25, cost 20, maintainability 20).
- Keep ruamel over YAMLRocks (4.50 vs 2.65): YAMLRocks 0.7.1 reports no key
  spans, empty alias spans, and its `comment_before` assigns a blank-separated
  file header to the first key.
- Rejected switching to Go `yaml.v3`/`yq`: Go binary dependency, and it
  re-emits the whole document (drops blank lines).

## Outcomes & Retrospective

(pending)
