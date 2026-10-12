# Overlapping preparation in manifest pipelines

This note is for generic manifest pipelines whose current plan and authority permit
independent preparation to overlap. It does not add a step to the one-shot
creator-command factory, which uses its own concise contract in
[`docs/production-factory.md`](../../docs/production-factory.md).

In a pipeline that allows overlap, later-shot materials may be prepared while an
eligible shot proceeds only when the current request and its declared dependencies
remain unchanged. Keep unreviewed candidates outside canonical inputs. Actual
upstream footage or outgoing-frame dependencies still wait for the selected original;
a planned image or candidate never substitutes for observed footage.

Follow the selected manifest and current owner for any project that uses this
sequencing. Keep history and existing project bytes readable without rewriting
records, replaying retired execution workflows or changing authority. Existing
derived edits and their provenance remain under the local editing owner.

This note creates no producer handoff, approval, generation, revision, correction,
replacement, purchase, publishing or scheduling authority.
