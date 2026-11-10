# Spicy schema fixture

`seedance_fast_schema_2026-10-22.json` contains only model identity, availability
flags and input schema from an explicitly authorized, read-only authenticated
model lookup on 2026-10-22. Account data, credentials, requests, tasks and prices
are absent. This is regression evidence, never current account readiness or
generation authority. The runtime must still use its current authenticated schema.

The vendor's `contentMediaType` MIME array is nonstandard; JSON Schema 2020-12
defines a string-valued annotation. Compatibility handling is restricted to the
two source image fields and requires coverage of the uploader's PNG/JPEG/WebP
subset. The vendor's additional GIF and automatic duration options do not expand
the application's supported inputs.
