# AI Personal Trainer — Final Build Specification

> **Note:** this is the original build specification, kept for reference. Parts
> of it are now out of date — in particular it describes a raw Strong history
> CSV and YAML feedback sidecars. Neither exists any more: lifting history is the
> condensed `sessions/*.yaml` records and feedback lives inside those records.
> See the "session lifecycle" section of [`README.md`](../README.md) for the
> current flow.

## 1. Project goal

Build a self-hosted, conversational AI personal trainer.

The PT initially manages two concurrent programmes:

* **Jacked & Tan 2.0 (J&T 2.0)** for lifting
* **Beginner Pete Plan** for Concept2 rowing

The architecture must be generic enough to support any combination of training programmes and exercise modalities in the future.

The PT should behave like a persistent human coach:

* it knows what the user has been doing
* it knows what programmes are currently active
* it can recommend what to do today
* it can explain its reasoning
* the user can disagree with it
* it can accept the user's preferred session
* it can make small, explicit adaptations to programmed workouts
* it can construct the next lifting workout as a complete Strong-compatible CSV
* it can validate that CSV before saving it
* it can retry generation with the LLM when validation fails
* it can save the valid Strong CSV to Google Drive for manual import into Strong
* it can receive natural-language feedback after every workout
* it can remember relevant subjective feedback when making future decisions

The user will always talk to the PT after completing a workout, even if there is no useful feedback:

> "It was fine."

That post-workout conversation is the explicit signal that a workout has been completed and should trigger state reconciliation.

---

# 2. Technology

Use:

* **Python**
* **FastAPI** for the PT backend
* **Open WebUI** as the chat interface
* **OpenRouter** as the LLM gateway during development
* **Google Drive** as persistent file storage
* **Google Docs** for human-readable programme/reference documents
* **YAML** for lightweight PT state
* **Strong CSV export** as the authoritative lifting history
* **Concept2 Logbook CSV export** as the authoritative rowing history
* **Pydantic** (or an equivalent strongly typed validation mechanism) for structured validation

Do not use Airflow in the initial system.

Do not introduce n8n.

Do not introduce a database unless a real requirement emerges later.

Do not introduce a separate Strong template file.

---

# 3. Core architectural principle

The application is a thin intelligent layer over the files that already exist.

There are four important categories of information.

## Programme knowledge

What a programme says should happen.

Stored as reference documents in Google Drive.

Examples:

```text
Programmes/
    J&T 2.0/
        reference
    Beginner Pete Plan/
        reference
```

## Objective workout history

What actually happened.

Stored in the native recording formats:

```text
History/
    Strong/
        strong_history.csv

    Concept2/
        concept2_history.csv
```

Do not convert Strong and Concept2 history into a shared workout model.

## Subjective workout feedback

What the user says about the workout that isn't captured in the export.

Examples:

* "felt tired"
* "bench was unusually easy"
* "my back was sore"
* "I stopped squats at 3 reps"
* "legs felt heavy after yesterday's row"

Store this separately from the source CSV.

## PT state

A lightweight YAML file containing current pointers and configuration:

```text
State/
    training_state.yaml
```

This is derived/cache state, not the historical source of truth.

---

# 4. No universal workout schema

Do **not** create a canonical workout schema spanning lifting, rowing and future activities.

Strong and Concept2 have different native representations.

Keep them in those native representations.

```text
Strong CSV     → Strong data
Concept2 CSV   → Concept2 data
```

The LLM is expected to understand each format.

If another activity is added later, it can retain its own native format.

---

# 5. No traditional semantic parser layer initially

Do not build dedicated semantic parsers for Strong or Concept2.

Python is responsible for:

* locating files
* reading/download/upload operations
* supplying files/context to the LLM
* managing YAML state
* storing feedback
* saving generated files
* validating generated outputs

The LLM is responsible for understanding:

* Strong CSV structure
* Strong workout rows
* exercise names
* weights/reps/sets
* Concept2 CSV structure
* rowing splits
* programme semantics
* historical patterns

Only introduce dedicated semantic parsing later if a real limitation appears.

---

# 6. Google Drive model

Google Drive is the storage layer.

Google Docs is simply the format used for human-readable reference documents stored in Drive.

Suggested structure:

```text
Personal Trainer/
│
├── Programmes/
│   ├── J&T 2.0/
│   │   └── reference
│   └── Beginner Pete Plan/
│       └── reference
│
├── History/
│   ├── Strong/
│   │   ├── strong_history.csv
│   │   └── Archive/
│   └── Concept2/
│       ├── concept2_history.csv
│       └── Archive/
│
├── Feedback/
│   ├── ...
│   └── ...
│
├── State/
│   └── training_state.yaml
│
└── Generated/
    └── Strong/
        └── ...
```

The exact folder names should be configurable.

---

# 7. Workout history is the source of truth

The user maintains complete exports.

## Lifting

After every lifting workout:

1. export the complete Strong workout history
2. upload the newest file to the expected Drive location
3. replace the current history file
4. optionally archive the previous version

Example:

```text
History/Strong/strong_history.csv
```

## Rowing

After every rowing workout:

1. export the complete Concept2 season history
2. upload the newest file to the expected Drive location
3. replace the current history file
4. optionally archive the previous version

Example:

```text
History/Concept2/concept2_history.csv
```

The latest complete export is the current objective record.

---

# 8. Strong CSV is both history and format reference

There is **no separate Strong template**.

The latest Strong history CSV serves two purposes:

1. authoritative lifting history
2. example/schema for the exact CSV format to reproduce when creating a new Strong workout

When generating a lifting workout, the system should provide the LLM with enough of the existing Strong CSV to establish:

* headers
* column order
* data types/representations
* quoting/escaping style where relevant
* row structure
* representative workout examples

The system should not assume the Strong CSV format in advance.

The actual user file is the format reference.

---

# 9. Training state YAML

Maintain:

```text
State/training_state.yaml
```

Example:

```yaml
version: 1

training_preferences:
  desired_sessions_per_week:
    lifting: 3
    rowing: 3
  fixed_weekdays: false
  double_sessions_preferred: false

programmes:

  jnt_2:
    active: true
    modality: lifting
    history_file: History/Strong/strong_history.csv
    reference: Programmes/J&T 2.0/reference

    latest_confirmed_workout:
      id: "..."
      date: "2026-09-27"
      label: "Workout D"

  pete_plan_beginner:
    active: true
    modality: rowing
    history_file: History/Concept2/concept2_history.csv
    reference: Programmes/Beginner Pete Plan/reference

    latest_confirmed_workout:
      id: "..."
      date: "2026-09-25"
      label: "Week 9 Session 1"
```

The YAML stores pointers/configuration, not workout history.

---

# 10. State reconciliation

The YAML is a cache.

The underlying history files win if there is a discrepancy.

Example:

```text
YAML:
last J&T workout = Workout C

Strong history:
latest J&T workout = Workout D
```

The system should recognise the discrepancy and repair the YAML.

---

# 11. Generic programme model

The PT must support multiple simultaneous programmes.

Each programme provides:

* identifier
* name
* modality
* reference document
* history source
* optional output behaviour

Examples:

```text
J&T 2.0        → lifting
Beginner Pete  → rowing
```

Future combinations may include:

```text
5/3/1 + Pete Plan
J&T 2.0 + another rowing programme
multiple lifting programmes
lifting + rowing + cycling
```

The core system should not be rewritten for each combination.

---

# 12. Programme references

Programme documents should contain enough information for the PT to understand the programme.

J&T reference:

* workout structure
* exercise selection
* order
* sets
* reps
* progression
* intensity
* rest
* cycles/weeks
* deloads
* programme-specific rules

Pete Plan reference:

* week/session structure
* workout details
* intervals
* recovery
* pace/SPM guidance
* progression

The document is the programme definition.

It is not a mutable workout log.

---

# 13. Interactive conversation

Open WebUI is the user-facing interface.

The user should be able to discuss training naturally.

Examples:

```text
I want to work out today.

I think you should row today because it's been two days.
Is that okay with you?

Actually I'd rather lift.

Why did you choose rowing?

I think that bench load is too high.

How has my training been going?

I've just finished a lifting session.
The new Strong history is in Drive.

It was fine.

I've just finished rowing. It felt much harder than usual.
```

The user should not have to understand internal operations.

---

# 14. FastAPI PT backend

Build a Python FastAPI service.

The service should expose an OpenAI-compatible chat endpoint so Open WebUI can use the PT as its backend.

Example:

```text
POST /v1/chat/completions
```

The backend should:

1. receive conversation
2. determine the user's intent
3. load relevant state
4. determine relevant programme(s)
5. retrieve relevant files
6. retrieve relevant feedback
7. call OpenRouter
8. validate structured responses
9. perform requested actions
10. persist state/feedback where required
11. return the conversational response

Open WebUI should remain the UI, not the location of the training logic.

---

# 15. Two kinds of memory

## Conversation memory

Used for immediate conversational context.

Examples:

> "Actually, let's lift instead."

Handled by Open WebUI/chat context.

## Training memory

Used for future coaching.

Stored in:

* workout history files
* feedback files
* state YAML
* persistent training preferences

Do not depend on chat history as the permanent training record.

---

# 16. Today's workout interaction

When the user says:

> "I want to work out today."

the PT should:

1. inspect active programmes
2. inspect training state
3. inspect recent workout history
4. inspect relevant subjective feedback
5. determine next valid session for each active programme
6. consider recent training balance
7. consider recovery/gaps
8. consider preferences
9. recommend a session
10. explain the recommendation
11. allow the user to agree, disagree or request something else

Preferred training pattern:

```text
3 lifting sessions/week
3 rowing sessions/week
```

but without fixed weekdays.

This is a preference, not a hard schedule.

---

# 17. User overrides

The user is allowed to disagree with the PT.

Track:

```yaml
decision:
  pt_recommendation: rowing
  user_preference: lifting
  final_session: lifting
```

The PT can explain its recommendation and then accept a reasonable user preference.

Do not treat the PT's initial recommendation as immutable.

---

# 18. Controlled adaptation

The PT may slightly modify a programmed workout when justified.

Examples:

* modest load adjustment
* rep adjustment
* volume adjustment
* rest adjustment
* rowing pace adjustment
* rowing SPM adjustment
* permitted substitution
* long-gap adaptation
* fatigue/performance adaptation

The PT must explicitly distinguish:

```text
Programme prescription
```

from:

```text
PT adaptation
```

The programme reference itself is never silently rewritten.

---

# 19. Adaptation policy

Maintain configurable boundaries:

```yaml
adaptation_policy:
  allow_load_adjustment: true
  max_load_change_percent: 10

  allow_rep_adjustment: true

  allow_volume_adjustment: true
  max_volume_change_percent: 25

  allow_rest_adjustment: true

  allow_exercise_substitution: true
```

Exact values can be adjusted.

The LLM may propose an adaptation.

Python validates it before output.

---

# 20. Core lifting capability: generate the next Strong workout

This is a **primary V1 capability**.

Whenever the PT determines that the next session is lifting, it must produce a fully populated Strong-compatible CSV and save it to Google Drive.

The generated workout should contain all information required by the Strong format, such as:

* workout name
* exercise names
* exercise order
* sets
* reps
* weights
* rest information where represented by Strong
* any other required columns/metadata

The process is:

```text
J&T reference
        +
Strong complete history
        +
relevant feedback
        +
current state
        +
user conversation/preferences
        +
adaptation policy
        ↓
determine next J&T workout
        ↓
determine exercise prescription
        ↓
determine loads/reps/sets/rest
        ↓
apply permitted adaptation
        ↓
LLM generates Strong CSV
        ↓
Python validates CSV
        ↓
if invalid:
        send validation errors back to LLM
        ↓
        LLM regenerates CSV
        ↓
        Python validates again
        ↓
repeat until valid or retry limit reached
        ↓
save valid CSV to Google Drive
        ↓
tell user it is ready
```

The LLM is responsible for constructing the actual Strong-format output because it has the Strong history available as the format reference.

Python is responsible for deciding whether the output is structurally acceptable.

---

# 21. Strong CSV validation and regeneration loop

Implement a dedicated validation stage between LLM generation and Google Drive output.

Conceptually:

```text
LLM
 ↓
candidate Strong CSV
 ↓
CSV reader
 ↓
Pydantic models
 ↓
validation
```

Use the latest real Strong history to establish the expected structure.

### Validation should check at minimum:

* valid CSV syntax
* expected headers
* expected column count
* expected column ordering where appropriate
* valid quoting/escaping
* valid data types/representations
* required fields populated
* sensible row structure
* expected workout/exercise structure
* no obvious malformed records

Do not build a complete semantic Strong parser.

The validator exists primarily to prevent malformed output.

### Pydantic

A suitable approach is:

```python
from pydantic import BaseModel

class StrongRow(BaseModel):
    # fields should be derived from the actual Strong CSV format
    ...
```

Parse CSV rows into dictionaries and validate them through Pydantic models.

The exact model must be based on the actual Strong history export.

Do not invent the fields in advance.

---

# 22. LLM correction loop

If validation fails, do not immediately give up.

Send the LLM the candidate output plus concise machine-generated validation errors.

Example:

```text
The generated Strong CSV failed validation.

Errors:
- Row 6 has 7 columns; expected 8.
- "Weight" contains non-numeric value "82.5kg".
- Required "Workout Name" field is empty.

Regenerate the complete CSV.
Do not change the workout prescription unless necessary to fix formatting.
Return only the corrected CSV.
```

Then validate again.

Use a bounded retry count, for example:

```yaml
strong_generation:
  max_validation_retries: 3
```

Do not allow an infinite loop.

If all retries fail:

* do not save the invalid file to the final Generated folder
* preserve the failed candidate for debugging if useful
* tell the user that generation failed and why
* leave programme state unchanged

---

# 23. Separate prescription from serialisation

The system should conceptually maintain:

```text
Workout prescription
```

and:

```text
Strong CSV representation
```

The LLM can generate both as part of the process, but the application should distinguish the two.

The desired workflow is:

```text
Programme/history reasoning
        ↓
structured workout prescription
        ↓
Strong CSV generation
        ↓
Strong CSV validation
        ↓
Google Drive
```

If the CSV is invalid because of formatting, retry the **serialisation**, not the training decision.

Avoid changing loads/reps/exercises merely because the CSV formatting was wrong.

---

# 24. Strong output destination

Valid generated files go to:

```text
Generated/Strong/
```

Example:

```text
Generated/Strong/JT2_Workout_D_2026-09-27.csv
```

The user should receive a response such as:

> J&T Workout D is ready. I've generated and validated the Strong import CSV and saved it to the Generated/Strong folder in Google Drive.

The user manually imports it into Strong.

---

# 25. Rowing workflow

When rowing is selected:

1. identify active rowing programme
2. determine next programme session
3. inspect Concept2 history
4. inspect relevant similar sessions
5. inspect relevant feedback
6. provide pace/SPM guidance
7. consider permitted adaptations
8. provide final rowing prescription

For Pete Plan:

```text
Pete Plan defines what the workout is.

Concept2 history informs how the PT coaches it.
```

---

# 26. Concept2 history

The Concept2 season export remains in native Logbook CSV format.

Preserve access to detailed data including:

* date
* duration
* distance
* average pace
* SPM
* watts
* intervals
* split data
* other available metadata

No universal rowing schema is required.

---

# 27. Post-workout interaction

The user always talks to the PT after training.

Examples:

> Finished. It was fine.

> Finished. Felt a bit tired.

> Finished. My back was sore during squats.

> The row was much harder than expected.

This conversation is the workout-completion trigger.

No background file watcher is required.

No Airflow is required.

No scheduled ingestion job is required.

---

# 28. Post-workout reconciliation

When the user indicates the workout is complete:

1. read the current relevant history file
2. identify the newest completed workout
3. compare against the expected/prescribed workout
4. reconcile programme position
5. update the state YAML
6. interpret any feedback
7. save relevant feedback
8. acknowledge completion

Example:

```text
Expected:
J&T Workout D

Latest Strong history:
J&T Workout D exists

User:
"It was fine."

Result:
Workout D confirmed.
No notable issues recorded.
```

---

# 29. Do not advance on unsupported evidence

If the user says:

> "Finished. It was fine."

but the relevant history does not contain the expected workout:

Do not silently advance programme state.

Respond clearly:

> I can't see the expected workout in the current Strong history, so I haven't advanced the J&T position yet. Check that the latest export is in Drive.

---

# 30. Feedback storage

Never modify the Strong or Concept2 CSV to add feedback.

Store a sidecar feedback record.

Example:

```yaml
workout:
  source: strong
  date: 2026-09-27
  id: "..."

feedback:
  raw: >
    I felt a bit tired and only did 3 reps on squats because my back was sore.

  summary:
    fatigue: moderate

    soreness:
      area: lower_back
      severity: mild

    deviations:
      - exercise: squat
        completed_reps: 3
        reason: lower_back_soreness

  future_relevance:
    - "Consider recent fatigue when evaluating subsequent sessions."
    - "Consider reported back soreness when evaluating subsequent lower-body training."

metadata:
  source: user
  recorded_at: "2026-09-27T18:20:00+01:00"
```

Requirements:

* preserve raw feedback
* store a useful summary
* associate it with an exact workout
* make it retrievable later

---

# 31. "It was fine"

A simple completion statement should still establish completion.

Example:

```yaml
workout:
  id: "..."

feedback:
  raw: "It was fine."
  summary:
    notable_issues: false
```

Future context should not become bloated with trivial "fine" records.

---

# 32. Natural-language feedback

No special syntax is required.

Examples:

> Bench was really easy today.

> My legs were still tired from the row.

> I skipped the final set because I was running late.

> I couldn't hit the planned reps on overhead press.

> The rowing pace felt too aggressive.

The PT should identify and associate the feedback with the most likely recent workout.

Ask for clarification only when the target workout is genuinely ambiguous.

---

# 33. Persistent coaching memory

Maintain a small amount of durable training memory.

Examples:

```yaml
preferences:
  - "User prefers flexible training days rather than fixed weekdays."
  - "User prefers explicit targets for rowing."
```

Only preserve information that is useful for future training decisions.

Do not store every conversation permanently.

---

# 34. Objective vs subjective information

Future decisions should consider both.

### Objective

What the recording system says happened.

Example:

```text
Squat:
100 kg × 5
100 kg × 5
100 kg × 3
```

### Subjective

What the user said happened.

Example:

```text
"I stopped at 3 because my back was sore."
```

The PT should use both pieces of information.

Do not assume that one subjective comment automatically invalidates the objective record.

---

# 35. LLM context selection

Do not blindly send every file into every LLM call.

For a lifting decision, likely context includes:

```text
J&T reference
relevant Strong history
relevant feedback
current state
user request
```

For rowing:

```text
Pete Plan reference
relevant Concept2 history
relevant feedback
current state
user request
```

Start with simple context selection.

Do not introduce vector search, embeddings or a RAG framework unless history volume later makes it necessary.

---

# 36. OpenRouter integration

Wrap OpenRouter behind a small client abstraction.

For example:

```python
class LLMClient:
    def complete(...)
```

Configuration:

```text
OPENROUTER_API_KEY
OPENROUTER_MODEL
```

Never hard-code credentials.

The model should be replaceable without changing the PT architecture.

Use structured output where appropriate.

---

# 37. LLM guardrails

The LLM may reason and propose actions, but Python validates important outputs.

Validate:

* response schema
* valid programme/session
* permitted adaptation
* required workout information
* Strong CSV structure
* allowed file operations

The LLM must not:

* invent historical workouts
* invent programme rules
* silently change programme definitions
* exceed adaptation limits
* create malformed final files
* cause an infinite generation/validation loop

---

# 38. No hidden autonomous behaviour

Keep the PT's capabilities explicit.

Useful operations include:

```text
read_state
read_programme
read_history
read_feedback
recommend_session
adapt_workout
generate_strong_workout
validate_strong_csv
save_strong_workout
record_feedback
update_state
```

The LLM should operate through those known capabilities rather than having unrestricted filesystem access.

---

# 39. No background workflow dependency

The user has committed to always talking to the PT after a workout.

Therefore:

```text
Workout
   ↓
export latest complete history
   ↓
upload to expected Drive location
   ↓
talk to PT
   ↓
PT reconciles completion
```

No background job is required to notice that a file changed.

---

# 40. Application structure

Suggested repository:

```text
personal-trainer/
│
├── app/
│   ├── api.py
│   └── chat.py
│
├── src/
│   └── personal_trainer/
│       ├── pt/
│       │   ├── context.py
│       │   ├── decisions.py
│       │   ├── coaching.py
│       │   └── memory.py
│       │
│       ├── state/
│       │   └── manager.py
│       │
│       ├── feedback/
│       │   └── manager.py
│       │
│       ├── programmes/
│       │   └── manager.py
│       │
│       ├── strong/
│       │   ├── generation.py
│       │   └── validation.py
│       │
│       ├── integrations/
│       │   ├── google_drive.py
│       │   └── openrouter.py
│       │
│       └── validation/
│           └── actions.py
│
├── tests/
├── config/
├── pyproject.toml
└── README.md
```

This structure is illustrative.

Avoid unnecessary abstraction.

---

# 41. Actual files must be inspected first

Before implementing file-specific functionality, inspect the real user files:

* actual Strong history export
* actual J&T reference document
* actual Pete Plan reference document
* actual Concept2 history export

Do not guess:

* Strong CSV columns
* Strong CSV row structure
* Strong output format
* Concept2 CSV columns
* J&T sequence
* J&T progression rules
* Pete Plan sequence

The Strong history itself is the format reference for generated Strong files.

---

# 42. Development sequence

## Phase 1 — conversational prototype

Build:

* FastAPI
* Open WebUI connection
* OpenRouter
* basic PT prompt/personality
* persistent conversation

Goal:

Be able to talk naturally to the PT.

---

## Phase 2 — Google Drive

Add:

* Drive authentication
* file reading
* file writing
* programme reference loading
* Strong history loading
* Concept2 history loading
* YAML state
* feedback storage

Goal:

The PT can operate over its persistent files.

---

## Phase 3 — today's workout

Implement:

> "I want to work out today."

Goal:

The PT can recommend row/lift based on real training context.

---

## Phase 4 — J&T + Strong generation

Add:

* J&T reference
* Strong history
* progression reasoning
* controlled adaptations
* Strong CSV generation
* Strong CSV validation
* LLM correction/retry loop
* Google Drive output

Goal:

The PT can:

1. determine the next J&T workout
2. fully populate it
3. generate the Strong-format CSV
4. validate it programmatically
5. send validation errors back to the LLM when necessary
6. retry generation until it passes validation or a retry limit is reached
7. save only a valid CSV to Google Drive
8. tell the user the file is ready for manual import into Strong

This is a core V1 capability.

---

## Phase 5 — Pete Plan

Add:

* Pete Plan reference
* Concept2 history
* rowing analysis
* pace/SPM guidance
* controlled adaptations

---

## Phase 6 — post-workout completion

Support:

> "Finished. It was fine."

Reconcile the latest history and update state.

---

## Phase 7 — subjective feedback

Support:

> "Finished. I felt tired and my back was sore during squats."

Store and retrieve workout-specific feedback.

---

## Phase 8 — conversational overrides

Support:

> "I think we should row today instead."

Allow the user to challenge the PT and reach a final session decision.

---

## Phase 9 — additional programmes

Demonstrate that adding another programme requires new reference material and configuration rather than a core architectural rewrite.

---

# 43. End-to-end lifting workflow

```text
User:
"I want to work out today."

        ↓

PT reads:
- training_state.yaml
- J&T reference
- Strong history
- relevant feedback
- user preferences

        ↓

PT determines:
J&T Workout D

        ↓

PT analyses:
- previous relevant sessions
- exercise progression
- recent fatigue/performance
- relevant feedback

        ↓

PT optionally adapts workout

        ↓

LLM generates:
structured workout prescription

        ↓

LLM generates:
Strong-format CSV

        ↓

Python validation
using schema derived from
actual Strong history

        ↓
     ┌───────────────┐
     │ VALID?        │
     └───────┬───────┘
             │
        NO   │   YES
             │
             ▼
   send validation errors
       back to LLM
             │
             ▼
      regenerate CSV
             │
             └───────→ validate again

        ↓

Valid Strong CSV

        ↓

Save to:
Generated/Strong/

        ↓

PT tells user:
"The Strong workout is ready in Drive."

        ↓

USER:
Imports file into Strong
        ↓
Trains
        ↓
Exports complete Strong history
        ↓
Uploads newest history to Drive
        ↓
Tells PT:
"Finished. It was fine."
        ↓
PT reconciles completion
```

---

# 44. End-to-end rowing workflow

```text
User:
"I want to work out today."

        ↓

PT reads:
- training_state.yaml
- Pete Plan reference
- Concept2 history
- relevant feedback
- user preferences

        ↓

PT determines:
next Pete Plan session

        ↓

PT analyses:
recent rowing performance

        ↓

PT provides:
pace/SPM guidance
+ optional small adaptation

        ↓

USER:
Rows

        ↓

Exports complete Concept2 history
        ↓
Uploads newest history to Drive
        ↓
Tells PT:
"Finished. It was fine."
        ↓
PT reconciles completion
and updates state
```

---

# 45. Final architecture

```text
                         OPEN WEBUI
                       conversation UI
                              |
                              v
                     FASTAPI PT SERVICE
                              |
        +---------------------+----------------------+
        |                     |                      |
        v                     v                      v
   Google Drive          OpenRouter             PT state/memory
        |                     |                      |
        |              LLM reasoning           training_state.yaml
        |              and coaching              feedback files
        |
   +----+-------------+----------------+
   |                  |                |
   v                  v                v
Programme          Strong          Concept2
references         history         history
                                     
Strong history
also serves as
Strong CSV format reference
                              |
                              v
                       PT decision engine
                              |
               +--------------+--------------+
               |                             |
               v                             v
             ROW                           LIFT
               |                             |
        Pete Plan prescription       J&T workout generation
               |                             |
               |                     Strong CSV generation
               |                             |
               |                    Python validation
               |                             |
               |                    LLM retry if invalid
               |                             |
               |                    Google Drive output
               |
               |
               v
           USER TRAINS
               |
               v
       Updated native history
               |
               v
          Google Drive
               |
               v
         USER TALKS TO PT
               |
               v
      completion + feedback
               |
               v
        updated PT state
```

---

# 46. Non-goals for V1

Do not build:

* Airflow
* n8n
* relational workout database
* universal workout schema
* Strong semantic parser
* Concept2 semantic parser
* separate Strong template
* vector database
* embeddings/RAG infrastructure
* complicated ETL pipeline
* manually maintained workout-history spreadsheet
* autonomous background decisions
* automatic Strong import

The following **are core V1 requirements**:

* conversational PT
* Google Drive integration
* native Strong history
* native Concept2 history
* programme reference documents
* YAML PT state
* workout-specific feedback
* controlled workout adaptations
* recommendation between active training options
* **generation of the next fully populated Strong CSV**
* **programmatic validation of the generated Strong CSV**
* **LLM regeneration when validation fails**
* **saving only a validated Strong CSV to Google Drive**
* **manual import of that generated file into Strong**

---

# 47. Overall philosophy

The finished system should feel like **one persistent personal trainer**, not a collection of automations.

The programme documents describe what should normally happen.

The native workout exports describe what actually happened.

The feedback records describe how the user experienced it.

The YAML records the PT's current derived state.

The conversation lets the user challenge, modify or override the PT's suggestions.

The LLM provides interpretation, coaching and workout generation.

Python provides application logic, state management, file operations, **Strong CSV validation**, **retry orchestration**, and **final output control**.

The user remains responsible for manually importing generated Strong workouts and for supplying the newest workout-history exports.

The central lifting loop is:

> **Ask what to do → PT constructs the exact next J&T workout → LLM produces the Strong CSV → Python validates it → invalid output goes back to the LLM for correction → valid file is saved to Google Drive → user imports it into Strong → trains → exports updated history → tells the PT the workout is finished.**
