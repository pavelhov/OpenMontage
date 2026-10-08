# Retaining an approved canonical video cut

This workflow retains an authorized local cut from an existing provider clip.
It keeps the original attempt identity and journals, identifies the cut as a
derived output, and requires a fresh review of the cut and its actual outgoing
frame. It creates no generation authority, repair allowance or review waiver.

Use the registered `video_trimmer` for the cut and registered `frame_sampler`
for outgoing sampling. The existing `record_derived_edit` and `record_selection`
utilities retain and validate the evidence; there is no separate workflow tool.
Read the pipeline's current editing/review instructions and the tools' Layer 3
skills before executing production edits.

## Sequence

1. Identify the exact generated parent attempt, shot and story revision. Obtain
   its bound original output from `load_attempt_result`; verify its current SHA.
   Retain the user's existing local-edit authorization for this exact edit.
2. Execute `registry.get("video_trimmer").execute(inputs)` with explicit cut
   start, end, codec, separate in-project output path and reason. Retain the
   actual `ToolResult` through `dataclasses.asdict`. Its `data.cut_receipt`
   records the input/output hashes, actual command, return code, submitted
   inputs and historical adapter version. Never manufacture this return.
3. Retain the typed recipe and execution envelope shown below. Record audio
   evidence for the retained interval. A trimmed AAC stream normally differs
   from the full parent AAC stream; do not claim whole-clip unchanged audio.
4. Compute `trimmed_outgoing_timestamp(output_path)`, then run the registered
   sampler with exactly that timestamp, `strategy="timestamps"` and
   `format="png"`. Retain the actual submitted inputs, sampler result and PNG
   hash. This timestamp comes from the final decoded presentation timestamp,
   normalized to the output seek timeline. `duration - 1 / average_fps` can
   choose an earlier image in a variable-frame-rate clip.
5. Construct the closed derived record. The root's named edit approval must
   bind `_digest(record_without_approval)` and the retained local-edit authority
   evidence. Approval of the original generation or its review alone does not
   approve changed bytes.
6. Call `record_derived_edit(project_dir, record)`. It retains separate preserved
   bytes at `production_derived_edits/<output_sha256>/output.mp4` and the immutable
   `record.json`. Keep native request/result journals unchanged.
7. View the cut and actual outgoing image, listen to its retained audio where
   present, and author a fresh review of the exact derived selection. Then call
   `record_selection(project_dir, shot_id, selection)`. All existing required
   semantic/audio predicates remain in force. The original attempt's review
   is stale for the cut.

## Exact recipe and execution envelope

The example below illustrates the existing Python utility boundary. Supply
real project identifiers, the existing authorization binding, and fresh unique
artifact/output paths. `bind(path)` means `{path: absolute_path, sha256:
file_sha256(path)}`. Save each JSON artifact before binding it; retain the actual
returns unchanged.

```python
from dataclasses import asdict
from lib.production_execution import _digest, load_attempt_result, record_derived_edit
from lib.production_provenance import _aac_sha256, trimmed_outgoing_timestamp
from lib.shot_contract import file_sha256
from tools.tool_registry import registry

registry.discover()
parent = load_attempt_result(root, parent_attempt_id)["output"]
inputs = {
    "operation": "cut", "input_path": parent["path"],
    "output_path": str(output_path), "start_seconds": 0, "end_seconds": 3.5,
    "codec": "libx264", "reason": "Retain reviewed completed action; remove late defect.",
}
cut = registry.get("video_trimmer").execute(inputs)
assert cut.success, cut.error
output = {"path": str(output_path), "sha256": file_sha256(output_path)}
recipe = {
    "version": "1.0", "operation": "canonical_video_trimmer_cut",
    "input": parent, "output_path": str(output_path),
    "submitted_inputs": inputs, "reason": inputs["reason"],
}
# Save recipe, then bind(recipe_path).
audio = {
    "mode": "retained_interval_aac_reencode",  # copy: retained_interval_stream_copy
    "input_sha256": _aac_sha256(parent["path"]),
    "output_sha256": _aac_sha256(output_path),
    "retained_interval_seconds": [inputs["start_seconds"], inputs["end_seconds"]],
}
execution_receipt = {
    "tool": "video_trimmer", "provider": "ffmpeg", "canonical_registry_used": True,
    "success": True, "generation": False, "input": parent,
    "recipe": bind(recipe_path), "output": output,
    "audio": audio, "tool_result": asdict(cut),
}
# Save execution_receipt, then bind(execution_receipt_path).
sample_inputs = {
    "input_path": str(output_path), "strategy": "timestamps",
    "timestamps": [trimmed_outgoing_timestamp(output_path)],
    "format": "png", "output_dir": str(outgoing_directory),
}
sample = registry.get("frame_sampler").execute(sample_inputs)
assert sample.success and len(sample.data["frames"]) == 1, sample.error
outgoing = bind(sample.data["frames"][0]["path"])
sampling_receipt = {
    "tool": "frame_sampler", "provider": "ffmpeg", "input": output,
    "submitted_inputs": sample_inputs, "outgoing_frame": outgoing,
    "tool_result": asdict(sample),
}
# Save sampling_receipt, then bind(sampling_receipt_path).
record = {
    "version": "1.0", "project_id": project_id, "story_revision": story_revision,
    "shot_id": shot_id, "parent_attempt_id": parent_attempt_id, "parent_output": parent,
    "recipe": bind(recipe_path), "execution_receipt": bind(execution_receipt_path),
    "output": output,
    "preserved_output": {
        "path": str(root / "production_derived_edits" / output["sha256"] / "output.mp4"),
        "sha256": output["sha256"],
    },
    "outgoing_frame": outgoing, "outgoing_receipt": bind(sampling_receipt_path),
}
root_approval = {
    "status": "approved", "approved_by": named_root_reviewer,
    "derived_edit_sha256": _digest(record), "authorization": existing_local_edit_authority,
}
# Root supplies approval; save it, then bind(root_approval_path).
record["approval"] = {"approved_by": named_root_reviewer, "evidence": bind(root_approval_path)}
checked = record_derived_edit(root, record)
```

`checked["selected_output"]` and `checked["selected_outgoing_frame"]` identify
the derived bytes. `checked["derived_timing"]` contains actual video
`duration_seconds`, `fps` and `final_frame_timestamp_seconds`. The retained
native result still describes the full original clip. For strict selection,
construct `{attempt_id: parent_attempt_id, output: output, outgoing_frame:
outgoing}` and add a fresh reviewer-authored review bound to
`selection_digest(selection)` and the current story revision before calling
`record_selection`. The example supplies no automatic passing review.

## Audio and cut boundaries

AAC parents use the elementary AAC hashes above, even when the retained output
was re-encoded. For `codec="copy"`, use `retained_interval_stream_copy`; the
canonical command copies the retained interval's audio. For other codecs, the
canonical tool re-encodes audio to AAC and the mode is
`retained_interval_aac_reencode`.

If probes establish that both parent and cut have no audio track, the exact
audio evidence is:

```json
{
  "mode": "no_audio",
  "input_has_audio": false,
  "output_has_audio": false,
  "retained_interval_seconds": [0, 3.5]
}
```

The validator independently probes both files. Dropping existing source audio
or adding audio to a silent parent fails. Do not convert a failed AAC hash probe
into a no-audio claim: non-AAC source audio is outside this adapter's current
boundary and fails explicitly. Obtain appropriate authorization and a supported
workflow rather than inventing receipts or removing required dialogue.

Both nonzero starts and variable frame rate are supported. Stream-copy cuts
can have keyframe limitations; validation checks the observed video duration
against the requested interval within one output frame. If a copy cut is
inaccurate, report that fact and use an authorized re-encoding cut when suitable.
Review the actual visual/audio endpoints; byte provenance does not judge story
completion or dialogue completeness.

## Verification cost and limits

Every canonical-cut validation, including disk-loaded selection, replays the
exact local cut and outgoing sampling and compares current bytes. An agent can
write a self-consistent on-disk record directly, so registration history alone
cannot prove derivation. Re-encoded cuts incur another encode per validation;
copy cuts incur a local remux. Final-PTS probing also decodes the selected video.
This narrow workflow has no persistent proof cache or signing system.

Exact-byte replay uses the installed FFmpeg environment and may fail closed
after an encoder/environment change. A historical tool adapter version remains
a retained fact and does not need to equal today's tool version. Missing,
changed, imprecise or unsupported evidence remains a reported failure. Do not
rewrite native journals, authority or an immutable derived record to force a
passing selection.

The parent must pass the existing native-parent provenance adapter. This change
adds no new OpenArt/MCP parent adapter or non-AAC audio adapter.
