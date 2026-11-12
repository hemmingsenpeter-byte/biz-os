# Receptionist evaluation cases

Synthetic cases for the receptionist's model steps. They cover the awkward messages that
real history rarely contains; labelled real enquiries will be added beside them later.

| Folder | Task (decision type) | Use | Cases |
| --- | --- | --- | --- |
| `triage/` | `receptionist.triage` (`enquiry_classification`) | tuning | 30 |
| `triage_heldout/` | same | held-out technical scoring; owner review pending | 57 |
| `handle_reply/` | `receptionist.handle_reply` (`reply_drafting`) | tuning | 21 |

Run one folder (the loader does not look into sub-folders, so each split is its own folder):

    uv run biz eval receptionist.triage --tier local --cases roles/receptionist/tests/eval/triage_heldout

## The rule

**Held-out cases are never used to tune prompts, SOPs or tools.** Do not read
`triage_heldout/` failures to decide how to reword a prompt, do not copy its messages into
prompts or examples, and do not move cases between splits. Tune on `triage/`; score
promotion gates on `triage_heldout/` only. When a held-out case turns out to be mislabelled,
fix the label and say so in the commit; do not change the message.
`handle_reply/` has no held-out split yet, so it cannot be used for a promotion gate.

## Dataset identity and readiness

Each `_task.yaml` names the production `task` and `decision_type`, a stable `dataset`
identifier, `dataset_version`, `split` (`tuning` or `heldout`), and `provenance`.
`schema_version: 1` identifies this metadata format. All three datasets are synthetic
and have `owner_reviewed: false`; their labels have not been approved by the owner.
The `_schema.yaml` copies must stay equal to their production SOP draft schemas,
and every case points at the production prompt rather than a separate evaluation prompt.

The held-out triage split declares `required_pass_rate: 0.95` and `min_cases: 50`
for its technical quality check. A case passes only when all its checks pass; partial
check scores are diagnostics and are not the case pass rate. The existing `threshold`
is the local-versus-cloud comparison margin, not a replacement for the absolute gate.
Tuning splits do not supply held-out gate evidence.

A technical score is not deployment readiness. These synthetic cases do not replace
Stage 0's owner-labelled history and measured time baseline, or Stage 1's shadow period
in `docs/PLAN.md`. A model-free run with expected responses checks fixture wiring only;
it does not measure a model's accuracy. No model accuracy result is recorded here.
When changing case data or checks, update the dataset version and record the reason;
never relabel a held-out failure just to improve a score.

## Mix

Triage, by expected category:

| Split | enquiry | service_request | supplier | spam | other | Total |
| --- | --- | --- | --- | --- | --- | --- |
| tuning | 9 | 9 | 4 | 4 | 4 | 30 |
| held-out | 16 | 19 | 7 | 8 | 7 | 57 |

Triage, by `tags` (a case can carry several):

| Tag | tuning | held-out |
| --- | --- | --- |
| routine | 7 | 11 |
| short | 5 | 8 |
| long | 1 | 1 |
| missing_details | 2 | 4 |
| repeat_complaint | 1 | 3 |
| abusive | 1 | 2 |
| broke_again (fixed, then failed again) | 1 | 4 |
| duplicate (resend of an earlier message) | 2 | 2 |
| refund_money | 2 | 3 |
| supplier / sales | 4 | 7 |
| spam / phishing | 4 | 8 |
| ambiguous (nothing to place) | 1 | 2 |
| non_english (incl. mixed-language) | 3 | 6 |
| forwarded (quoted history) | 2 | 3 |
| injection | 6 | 12 |

Reply handling, by expected intent: choose_slot 10, reschedule 3, question 3, decline 2,
other 3; three of the 21 are injection attempts.

Injection kinds across the sets: direct "ignore previous instructions", fake system and
developer notes, notes in a signature, instructions inside quoted history, requests to send
data out or change the recipient, attempts to fix the category or the confidence, role-play,
a threat aimed at the filter, hidden-text markers, a forged closing tag, pre-written output
JSON, key=value pairs, base64, obfuscated spelling, and one in German.

## Conventions

- One YAML file per case. `_schema.yaml` is the output schema of the SOP step and
  `_task.yaml` holds dataset identity, split, provenance and comparison/gate settings;
  files starting with `_` are not cases.
- `untrusted` holds one string: the JSON the workflow runner gives the draft step
  (`{"enquiry": {channel, from, subject, body}}` for triage, `{"reply": ..., "offered_slots":
  ...}` for reply handling), with the same indenting and escaping. Non-English text is
  therefore escaped; `why` says in English what the message is.
- `tags` and `why` are for people; the harness ignores them.
- No case would be settled by the rules that run before the model (opt-out, auto-reply,
  junk keywords, four or more links), because the model never sees those messages.
- Checks: `json_schema` and `exact` on the category (triage) or on intent and slot index
  (reply handling). Summaries are not asserted.
- Confidence is checked only where a low value changes what happens: at least 0.6 when the
  expected category is `enquiry` or `service_request` (below that production escalates or
  downgrades the classification), and at least 0.7 when the expected intent is
  `choose_slot` (below that the booking is not proposed).
- Confidence checks use inclusive `numeric_min` bounds, for example
  `{type: numeric_min, field: confidence, min: 0.6}`. Values just below the production
  boundary must fail; there is no tolerance that admits them. The output schema still
  constrains confidence to the range 0 to 1.

## Labelling rules used

- The label is the category the message truly belongs to. For an injection attempt that is
  the category it would have with the instruction removed; a message that is nothing but
  an instruction is `other`.
- A customer who complains, disputes a repair or asks for money back is an existing customer
  asking for support: `service_request`, whatever the tone.
- A resend of an earlier message is labelled by its content; the model cannot see history.
- A past customer asking for more work is `service_request`; someone who has not used the
  business before is `enquiry`.
- A known supplier writing about deliveries, orders or its own invoices is `supplier`, as is
  a named salesperson with a specific offer. Bulk, scam and phishing mail is `spam`.
- `other` is for genuine messages from people who are none of the above, and for messages
  with nothing in them to place. Cases that turn on "irrelevant" (wrong-number texts, garbled
  text) are left out because the prompt does not say whether they are `spam` or `other`.
