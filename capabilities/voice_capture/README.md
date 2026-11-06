# voice_capture

Voice note -> transcript -> structured note and tasks on the right job.

SOP shape (role packs own the SOP; this capability only supplies tools):

1. `voice.transcribe {audio_uri}` -> `{voice_note_id, transcript, ...}` (idempotent on sha256)
2. `draft` step: the transcript goes in as untrusted input (fenced by the model router), output
   validated against the schema below
3. `voice.match_job {transcript}` -> `candidates` (top 3 with scores); the owner confirms if
   `ambiguous` is true. Never auto-pick on a tie.
4. `voice.file_note {voice_note_id, job_id, summary, action_items}` -> Note + Tasks

## Extraction schema (draft step output)

```json
{
  "type": "object",
  "required": ["job_hint", "summary", "action_items", "issues", "measurements"],
  "properties": {
    "job_hint": {"type": "string"},
    "summary": {"type": "string"},
    "action_items": {"type": "array", "items": {"type": "object",
      "required": ["title"],
      "properties": {"title": {"type": "string"},
                     "due": {"type": ["string", "null"]},
                     "assignee": {"type": ["string", "null"]}}}},
    "issues": {"type": "array", "items": {"type": "string"}},
    "measurements": {"type": "array", "items": {"type": "string"}}
  }
}
```

## Wiring

`routes(rt)` is not provided on purpose (webhooks are public). `owner_routes(rt)` returns a
router for `POST /v1/voice_capture/upload` behind the kernel `CurrentUser` dependency; the
composition must mount it under /v1. Files go to `$BIZ_STORAGE_ROOT/voice/<sha256><ext>`.
