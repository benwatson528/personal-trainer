# AI Personal Trainer

A self-hosted, conversational AI personal trainer. The PT manages concurrent
training programmes (J&T 2.0 for lifting, Beginner Pete Plan for rowing),
recommends what to do today, generates Strong-compatible workout CSVs, and
records how each session actually went — a thin intelligent layer over files
that already exist.

## The session lifecycle (the core flow)

This is what the app does. Everything else in this document exists to support
these four steps.

**1. The athlete asks to train.** "I want to lift today", "what should I do
today?", or anything similar.

**2. The PT works out the next session from state, not from history.** It reads
`training_state.yaml` — the `latest_confirmed_workout.programme_position` and
the pending workout — and the programme's `reference.md`. That position is the
only thing that says where in the programme the athlete is. It picks the next
session (the pending session if there is one, otherwise the one after the last
confirmed session, wrapping week to week). History is **never** used to infer
position.

**3. The PT plans and writes the workout.** For a lifting day it reads the
relevant day template CSV, the most recent sessions in `sessions/*.yaml`, the
relevant config (`programmes.yaml`, `equipment.yaml`) and `reference.md` again,
then works out sets, reps, loads and adaptations — drawing on recent sessions
and general training sense. This step is **conversational**: the PT may say
"your last session shows you struggling on squats, so let's hold the load and
focus on form". Once the athlete agrees, the PT generates a Strong-compliant CSV
from the day template and writes it to Google Drive, ready to import into
Strong.

**4. After training, the PT records the session.** The athlete imports the
completed Strong CSV into Google Drive and talks to the PT. The PT asks how the
session went and whether there is anything to note (some notes will already be
in the CSV). It reads the completed CSV and condenses it into a session YAML in
`sessions/`, including the athlete's own words as notes. Those session YAMLs are
the lifting history the next plan reads.

Rowing follows the same lifecycle but is simpler: there is no template and no
generated CSV, so steps 3 and 4 are advice and recording only.

Feedback lives **only** in the session YAML. It cannot be recorded outside a
post-workout conversation, and that is deliberate: a note is always tied to the
workout it is about.

## Design rule

**Python supplies context. The LLM makes the decision.**

The LLM determines the next session by reading the programme reference, the
programme position, and recent history. No programme logic is encoded in
Python: there is no table of what week 3 day 2 contains. Adding a programme
means adding a reference document, not changing code.

History is used for exactly two things — advice, and setting specific weights.
It is never used to infer where you are in the programme. Position lives in
one place (`latest_confirmed_workout.programme_position`) and moves only when
you confirm a session.

**The app export is never parsed in Python.** The training app is yours to
change, and a parser written against its columns today breaks silently the day
you switch. So rows leave the export verbatim as text, and the LLM reads them.
Only one schema is derived in Python — the Strong CSV column list, which we must
emit ourselves — and the same rule holds there: it comes from the day templates
we ship, never from ad hoc parsing.

## Architecture

```
Open WebUI  ──►  FastAPI PT service  ──►  Google Drive (or local backend)
                     │                     ├── programme references
                     ├── OpenRouter LLM    ├── day templates (Strong CSV)
                     └── tool-calling loop ├── session records (YAML)
                                           ├── Concept2 history (CSV)
                                           └── state (training_state.yaml)
```

- **Python** locates files, manages state, enforces hard constraints, validates
  LLM output, and controls all file writes. The **LLM** understands
  Strong/Concept2 formats, programme semantics and history.
- No universal workout schema: Strong and Concept2 stay in their native
  formats. For Strong, the day templates are the format reference for generated
  files, and the condensed session records are the history.
- No database, no Airflow, no n8n. The post-workout conversation is the
  completion trigger.

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/personal-trainer        # or: .venv/bin/uvicorn app.api:create_app --factory
```

The service listens on `http://localhost:8000` and exposes an OpenAI-compatible
endpoint:

```
POST /v1/chat/completions
GET  /v1/models
GET  /health
```

`src/` is on the path via the editable install, so `app.api` resolves without
setting `PYTHONPATH`. For an unmanaged run:

```bash
PYTHONPATH=src .venv/bin/uvicorn "app.api:create_app" --factory --port 8000
```

Logs go to stdout and to `pt.log`, including per-call token usage and cost.

### Exposure and authentication

The service has **no authentication** and, by default, binds `0.0.0.0:8000`.
Anyone who can reach the port can spend your `OPENROUTER_API_KEY` and, through
the tool loop, read and write your entire Google Drive (the OAuth token carries
full Drive scope). Bind it to localhost with `PT_HOST=127.0.0.1` when nothing
else needs to reach it, and put it behind an authenticating reverse proxy before
exposing it on a network. The default stays open because the bundled
`start-services.sh` runs Open WebUI in Docker, which reaches the host over the
Docker bridge.

### Open WebUI

In Open WebUI, add an OpenAI-compatible API connection pointing at
`http://localhost:8000/v1` with model `personal-trainer`.

### OpenRouter

```bash
export OPENROUTER_API_KEY=sk-or-...
```

The model is configurable via `config/config.yaml` (`llm.model`) — currently
`anthropic/claude-haiku-4.5`. `price_in`/`price_out` are used only to log cost,
never to route.

### Storage backends

`config/config.yaml` selects the backend:

- `local`: filesystem under `data/`, mirroring the Drive folder structure. Only
  for local development and the one-off seeding step below; the committed repo
  ships no `data/` (it is gitignored), so nothing here is public.
- `drive`: Google Drive, authenticated as your own account. Set
  `storage.root_folder_id` and either `storage.oauth_token_file`
  (recommended) or `storage.credentials_file`.

Set up Drive once:

1. Create an OAuth client (Desktop app) in the Google Cloud Console and
   download its JSON — exact steps are in the `scripts/gdrive_auth.py`
   docstring. Never commit the JSON.
2. Run the one-time login to write a token:
   ```bash
   PYTHONPATH=src .venv/bin/python scripts/gdrive_auth.py \
     --client-secrets config/gdrive_client_secret.json \
     --token config/gdrive_token.json
   ```
3. In Drive, create a folder named `Personal Trainer` and copy its ID from
   the address bar (`.../folders/<FOLDER_ID>`) into `storage.root_folder_id`.
4. Confirm `storage.backend: drive` and point `storage.oauth_token_file` at
   the token file. (The `root_folder_id` committed in `config/config.yaml` is
   the maintainer's — replace it with your own from step 3.)
5. If you have an existing local `data/` mirror to upload, seed Drive from it
   (idempotent — safe to re-run). A fresh clone has no local `data/`: the live
   files already live in Drive, and the test suite uses `tests/fixtures/`.
   ```bash
   PYTHONPATH=src .venv/bin/python scripts/gdrive_migrate.py
   ```
6. Restart the server.

Why OAuth rather than a service account? Service accounts have **no Drive
storage quota and cannot own files**, so they only work on Workspace shared
drives. On a personal Gmail (read: your Drive) uploads fail with
`storageQuotaExceeded` — noted in Google's own Drive API docs. Running OAuth
as yourself means files are yours and count against your normal quota.

## Configuration

| File | Purpose |
|---|---|
| `config/config.yaml` | storage, LLM, generation and folder settings |
| `config/programmes.yaml` | programme registry — reference document per programme |
| `config/equipment.yaml` | **the kit you own** — bars, plates, dumbbells |

Environment overrides: `PT_CONFIG`, `PT_PROGRAMMES_CONFIG`,
`PT_EQUIPMENT_CONFIG`.

`config/equipment.yaml` is the one to get right. It is not advisory — Python
validates every generated weight against it, and a missing or malformed file
means **no equipment enforcement at all**, so unloadable weights reach Strong
silently. See [Equipment enforcement](#equipment-enforcement).

## Tools

Thirteen, all defined in `src/personal_trainer/pt/tools.py`. The conversation
loop is capped at **6** tool rounds; a normal turn needs 2–4.

| Tool | Required args | Purpose |
|---|---|---|
| `read_state` | — | current programme position and pending workout |
| `read_programme` | `programme_id` | registry entry |
| `read_history` | `programme_id` | recent sessions as a markdown table |
| `read_reference` | `programme_id` | raw programme reference document |
| `recommend_session` | `recommended_modality` | **the main entry point** (below) |
| `adapt_workout` | `programme_id`, `prescription`, `adaptations`, `reason` | propose a deviation |
| `generate_strong_workout` | `programme_id` | generate without saving |
| `validate_strong_csv` | `csv` | structural check on candidate text |
| `save_strong_workout` | `programme_id`, `csv`, `label`, `date` | write to `generated/strong/` |
| `reconcile_workout` | `programme_id` | post-session state update |
| `summarise_session` | `programme_id`, `date` | record a finished session, incl. `feedback` (below) |
| `set_programme_position` | `programme_id` | correct a stale pointer |
| `update_state` | `programme_id` | low-level state write |

### `recommend_session` generates and saves

`recommend_session` is not just advice. When the modality is `lifting` it runs
the full generation pipeline and **writes the CSV to disk itself**, returning
`saved_path`. The model does not have to remember to call a second tool, which
was a real gap — it previously answered from history and never generated
anything.

The payload also carries `adaptations` and `reasoning` from the prescription, so
the assistant can tell you what it changed and why rather than presenting an
adapted workout as programmed verbatim.

### Choosing the modality

"I want to do a workout" is a **state** question, not a history question. The
programme pointer in `training_state.yaml` already says which programme is
active, its modality, and where it sits — that is enough, and the persona tells
the model not to reach for `read_history` to decide.

That matters because the source export mixes rowing sessions in with the
lifting ones, so raw history is a genuinely bad signal for modality: it invites
planning the wrong session. `recommend_session` is a required `recommended_modality`
argument, and it returns history **only** for a lifting request. A rowing turn
therefore sends no lifting history and spends no LLM calls generating anything.

The reference and history are pulled in once a specific session is being planned
— not before.

### Progression and off-plan training

If every core session in a week is done, the reference's own progression rule
applies: recommend an optional session that week, or the first core session of
the next week.

Training outside the PT does **not** advance programme position. After a gap,
say so and the assistant calls `set_programme_position` — it returns the
reference so your words ("I'm at week 3") map to a real week and session. Pass
`out_of_cycle=true` to leave position untouched.

## The Strong generation loop

When the next session is lifting, the PT:

1. loads the programme reference, recent session history, state, the adaptation
   policy and the equipment inventory;
2. asks the LLM for a structured **prescription** — the training decision;
3. asks the LLM to serialise it as a **Strong CSV**;
4. validates it through **four gates**;
5. on failure, replays the model's own previous CSV alongside the errors and
   asks for a targeted repair;
6. saves only a validated CSV to `generated/strong/` for import into Strong.

Prescription and serialisation are separated so a formatting failure retries the
serialisation, not the training decision.

### Four validation gates

| Gate | Owner | Checks |
|---|---|---|
| 1. Structural | Python | column count, types, required fields, against a schema taken from the day template; the date must match the supplied timestamp exactly |
| 2. Equipment | Python | every prescribed weight is loadable with your own kit |
| 3. Prescription fidelity | Python | the CSV still contains the prescription: each exercise has the working sets the decision named |
| 4. Semantic | LLM | weights, reps and total volume are reasonable against programme and history |

Gates 1–3 are deterministic; gate 4 is a judgement call and is deliberately
last, because judgement-based validation is what previously failed to
converge. Bounded by `strong_generation.max_validation_retries` (default 3).

Gate 3 exists because the CSV is a *copy* of the prescription, and a copy can
silently drop sets. A live Week 1 Day 1 prescribed 4 working sets of T2a deadlift
and emitted 1: the file was well-formed, every weight was loadable, and the LLM
validator approved it. Gates 1, 2 and 4 all check the file; none checked that the
file still contained the workout. This gate never looks at the programme — it
only compares the copy against the thing it is a copy of.


### Targeted repair, not regeneration

A retry does **not** start the session again. The prompt replays the previous
CSV with the specific errors and instructs the model to fix only what is named,
keeping the exercises, sets and reps that are not mentioned. Where an error
carries a suggested weight, it must use exactly that one.

### The date is supplied, never invented

Python computes the session timestamp once per `generate()` and passes it to
both the generation prompt and the validator, which requires exact string
equality. The model must not copy a date out of the history sample or make one
up — that regression stamped planned workouts weeks in the past.

### Condensed session records

Recent history has to reach the model, and the raw export is expensive to send.
One Strong row is a 15-column CSV line, mostly empty cells, and a session averages
~17 rows — about **430 tokens**. Sending the last 24 rows is only **1.4 sessions**
of context, and rendering rows as a markdown table costs nearly three times the
raw CSV again in padding.

So after each workout `summarise_session` has the model condense that session
into a few lines, and generation reads these instead. The export itself is no
longer kept: these records are the lifting history.

```yaml
date: 2026-06-23
workout: 'J&T 2.0 - 4'
workout_notes: 'bar speed slow on the last set'
Squat (Barbell): ['60x5', '80x3', '97.5x10', '91x6', '91x6']
Deadlift (Barbell): ['60x3', '92.5x10', '92.5x10', '92.5x10', '92.5x10']
notes:
  Squat (Barbell): 'knees caved, brace harder'
```

A session costs ~74 tokens instead of ~430, so the same budget buys ~20 sessions
of history instead of 1.4. The trade is deliberate: more context for slightly
more tokens, because a single recent session does not tell the model what weight
to put on a T Bar Row, and guessing is the failure that matters.

Three details matter:

- **Sets are ordered and unlabelled.** The reader infers the ramp, top set and
  backoff from the order, so the extractor never has to guess which of those a
  set was and cannot label it wrongly.
- **Notes are carried verbatim.** Strong records a note on the workout and on
  individual exercises — cues, grip, pain, what to fix. A condensed record that
  dropped them would be worse than the raw rows it replaces, so `workout_notes`
  and a per-exercise `notes` mapping are part of the format, and the generation
  prompt tells the reader to act on them.
- **Sets are quoted strings.** Unquoted, YAML reads `0x8` as the hex integer `8`,
  which would silently destroy every bodyweight set — and bodyweight is exactly
  what Strong records for pull-ups and push-ups.

Two properties are load-bearing:

- **Python does not parse the export.** The raw rows go out to the model as
  verbatim text and the summary comes back as text. A missing or renamed column
  changes nothing about what the model receives — see
  [The app export is never parsed in Python](#design-rule).
- **These records are the history, not a cache.** Once a session is summarised the
  app export is not retained. `src/personal_trainer/sessions.py` validates only
  that the shape is right and that every set is a `weight x reps` pair, then
  passes the text through untouched.
- **The Strong format comes from the day template.** The condensed records
  deliberately drop the 15-column layout, so generation injects the current day's
  fill-in template, whose header is the exact Strong layout. A workout is only
  importable if it matches the athlete's file, so the format wins over the token
  saving.

## Equipment enforcement

Python, not the LLM. Semantic validation failed earlier because it is a
judgement call whose verdicts oscillated between attempts; equipment
feasibility is arithmetic. A dumbbell weight is either in your inventory or it
is not. Deterministic checks converge.

- **Barbell** loads are checked by bounded subset-sum over your plates, in
  exact rational arithmetic. The unit is the **GCD** of your plate sizes, not
  the smallest plate — with both 0.5kg and 1.25kg plates the smallest is not a
  divisor of the other, and using it silently rejects buildable loads.
- **Dumbbell** weights must exist in your inventory exactly.
- **Unknown implements are skipped, never guessed.** Strong encodes the
  implement in the name (`Hammer Curl (Dumbbell)`) but not reliably (`T Bar
  Row` has no suffix). Coverage is deliberately traded for never false-rejecting
  a good workout.
- **Warmups are exempt** — they are ramp-up weights, not prescribed loads.

Errors name the available alternative and are fed straight back as the repair
prompt:

```
Squat (Barbell): 110.5kg cannot be loaded on a 20kg bar from the plates the
user owns. Plates available per side: 25kg x2, 20kg x6, ...  Use 110kg instead.
```

## Post-workout reconciliation

You always talk to the PT after training ("Finished. It was fine."). Two things
happen:

- `summarise_session` condenses the completed export into a session YAML,
  carrying the athlete's spoken feedback and any notes the export already had.
  This is the history future sessions are planned from.
- `reconcile_workout` reads the newest workout, compares it against the expected
  one, and moves programme position in the state YAML only on supported evidence.
  If the expected workout is missing, it says so and does **not** advance
  position.

## Feedback

Feedback lives in the session records — nowhere else. It is never written into
the Strong or Concept2 CSVs, and there is no separate feedback store.

In step 4 the athlete talks to the PT about the session. Whatever they say (and
whatever notes the Strong export already carried) is passed to
`summarise_session` as `feedback` and folded verbatim into the session YAML's
`workout_notes` / per-exercise `notes`. Generation reads those notes back out of
the recent sessions, so telling the PT how a session went is what lets it adjust
what it prescribes next time.

Because feedback is part of the session record, it can only be recorded in a
post-workout interaction. There is deliberately no way to log feedback outside a
workout: it would have no session to attach to.

The file name is taken from the Strong export's own `Workout` column — that
column is the record. (If an entry was named "Rowing A" in the app but contains
lifting exercises, the session file reflects the app's name, not its contents.)

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

The suite runs entirely against committed fixtures under `tests/fixtures/`:
the `data_dir` fixture copies `tests/fixtures/data` per test, giving a synthetic
Concept2 export, the J&T reference document, the day templates and a fixed
baseline training state. No test reads the repo-root `data/` mirror or Google
Drive, so a fresh clone runs green with no personal data present.

Worth knowing about two of them:

- `test_barbell_math_matches_brute_force` exhaustively enumerates every valid
  plate combination and asserts the validator agrees at all 200 weights in
  0.25kg steps. It exists because an in-place knapsack silently over-used a
  plate size, making unbuildable weights look reachable.
- The `data_dir` fixture copies `tests/fixtures/data` (never the repo-root
  `data/` or Drive), and the session timestamp is pinned globally. Without those
  the wall clock would leak into the suite and cause spurious failures.

Some tests still read the shipped `config/programmes.yaml` and
`config/equipment.yaml`: those are application configuration (the programme
registry and the athlete's kit), not personal training data, and asserting the
shipped files parse is deliberate.

## Repository layout

```
config/
  config.yaml              storage, LLM, generation, folders
  programmes.yaml          programme registry
  equipment.yaml           the kit you own
data/                      local scratch mirror of the live Drive root (gitignored)
  programmes/<name>/reference.md
  programmes/<name>/templates/day_N.csv   per-day fill-in CSV skeletons (J&T 2.0)
  history/strong/          landing folder for a completed export to be summarised
  history/concept2/*.csv
  sessions/*.yaml          condensed session records, written by the LLM (the history)
  state/training_state.yaml
  generated/strong/*.csv   validated output, ready to import
logs/
src/
  app/
    api.py                 FastAPI app factory, logging setup
    chat.py                OpenAI-compatible endpoint, tool loop
  personal_trainer/
    config.py              settings from YAML + env
    equipment.py           inventory loading and prompt rendering
    models.py              pydantic models
    pt/                    coaching persona, context, decisions, memory, tools
    state/                 training_state.yaml manager
    sessions.py            condensed session records
    programmes/            programme registry
    strong/                schema (from templates), structural + equipment +
                           fidelity validation, generation and retry
    integrations/          OpenRouter LLM client
    validation/            action guardrails (adaptation policy, prescriptions)
    storage/               local + Google Drive backends
tests/                     pytest suite
  fixtures/data/           self-contained example tree the tests run against
```