# slackwright design

Status: draft, 2026-11-19.

## 1. Purpose

slackwright is a Python library that gives an AI agent in Slack the
Slack parts it needs: render answers, read threads, show progress,
build forms, and manage canvases. It also exposes these parts as LLM
tools, so that an agent can design and post forms and blocks itself.

Two bots will use it first:

- **imbi-slackbot** (`AWeber-Imbi/imbi`, `apps/slackbot`, open source,
  Slack over Socket Mode).
- A private bot that receives Slack over HTTP events.

Today the two bots hold forked copies of the same code: `slackdwn.py`,
`inflight.py`, the file-to-content-block conversion, and the thread
reconstruction. slackwright replaces the forks.

### 1.1 Goals

1. One renderer for LLM Markdown, based on Slack's native Markdown
   support.
2. One toolkit that exposes render, blocks, forms, and canvases as LLM
   tools, with an Anthropic adapter (MCP later).
3. Forms in two modes: a modal, and inline input blocks in a message.
4. Full canvas management: create, edit by section, read.
5. Shared ingest (thread, mentions, files) and lifecycle (progress,
   in-flight drain) code.

### 1.2 Non-goals

- slackwright does not run an agent loop or call an LLM.
- slackwright does not own event dispatch. Each bot keeps its Bolt app,
  its handlers, and its own logic (for example, imbi's identity and JWT
  minting).
- slackwright ships only the Anthropic tool adapter. A bot that
  uses a different tool framework keeps that adapter in its own code.

## 2. Slack facts that drive the design

Checked against docs.slack.dev on 2026-11-19.

| Fact | Effect on design |
|---|---|
| The `markdown` block and `markdown_text` render standard Markdown: headers, bold, links, lists, code with syntax highlight, tables, quotes, task lists. Limit: 12,000 characters per message in total. | The renderer sends Markdown without conversion. The mrkdwn regex code, the block builder, code-as-file upload, and tables-as-canvas go away. Only text over 12,000 characters needs special handling. |
| `chat.startStream` / `appendStream` / `stopStream` stream a reply. Chunks: `markdown_text`, `task_update`, `plan_update`, `blocks`. A stream cannot mix the `markdown_text` argument with `chunks`. A stream lives for 10 minutes at most. In a channel, `recipient_user_id` and `recipient_team_id` are required. | Streaming uses `chunks` only, so that text and task updates can share one stream. The stream restarts in the same thread when it is near a limit. |
| `task_update` chunks and the plan/task card blocks show agent steps with status `pending`, `in_progress`, `complete`, `error`. | Progress (tool calls) is shown as task cards. This replaces the status edits in both bots. |
| The `markdown` block is supported in messages only, not in modals. | A form intro in a modal uses a `section` block with mrkdwn. |
| `table` block: up to 100 rows, 20 cells per row, and 10,000 characters across all table cells in one message. | The table spec checks all three limits. |
| A modal opens only with a `trigger_id` from a user action. The `trigger_id` expires after 3 seconds. | The agent cannot open a modal directly. It posts a button. The click opens the modal. |
| A button `value` holds 2,000 characters. Modal `private_metadata` holds 3,000 characters. | The form spec cannot travel in the payload. It goes in a store. Only the `form_id` travels. |
| Interaction payloads must be acknowledged within 3 seconds. | The submit handler validates, acknowledges, and then runs the bot's callback as a background task. |
| A message holds 50 blocks. A modal holds 100 blocks. | Form specs are limited by field count before posting. |
| `canvases.getContent` returns a whole canvas as Markdown (scope `canvases:read`). `canvases.edit` with `replace` and no `section_id` replaces the whole canvas. | `canvas_read` uses `canvases.getContent`. The edit tool requires a section for `replace` and `delete`. |
| The Anthropic Messages API accepts image and document blocks only in user turns. | Ingest puts files from the bot's own messages into a user turn, or drops them. |

Sources: [markdown block](https://docs.slack.dev/reference/block-kit/blocks/markdown-block/),
[chat.startStream](https://docs.slack.dev/reference/methods/chat.startStream/),
[plan block](https://docs.slack.dev/reference/block-kit/blocks/plan-block/),
[task card block](https://docs.slack.dev/reference/block-kit/blocks/task-card-block/),
[table block](https://docs.slack.dev/reference/block-kit/blocks/table-block/),
[input block](https://docs.slack.dev/reference/block-kit/blocks/input-block/),
[canvases.getContent](https://docs.slack.dev/reference/methods/canvases.getContent),
[canvases.edit](https://docs.slack.dev/reference/methods/canvases.edit/),
[conversations.canvases.create](https://docs.slack.dev/reference/methods/conversations.canvases.create/).

## 3. Architecture

```text
            bot (for example, imbi-slackbot)
   ┌──────────────────────────────────────────┐
   │ Bolt app ─ event handlers ─ agent loop   │
   │     │              │            │         │
   │     │ register()   │ ingest     │ tools   │
   └─────┼──────────────┼────────────┼─────────┘
         ▼              ▼            ▼
   ┌──────────────────────────────────────────┐
   │ slackwright                              │
   │  forms   ingest   toolkit ─► render       │
   │    │               │      ─► blocks       │
   │    ▼               │      ─► forms        │
   │  FormStore (bot)   │      ─► canvas       │
   │                    ▼                      │
   │              progress, inflight           │
   └──────────────────────────────────────────┘
                     │
                     ▼  slack_sdk AsyncWebClient
                   Slack
```

### 3.1 Modules

| Module | Contents | Replaces |
|---|---|---|
| `render.py` | `post_markdown()`, `Stream` (streaming text and task updates), overflow over 12,000 characters | both `slackdwn.py` |
| `blocks.py` | Pydantic specs for a set of blocks, and `to_blocks()` | — |
| `forms.py` | `FormSpec`, field types, modal and inline builders, submission parsing, `FormStore` protocol, `MemoryFormStore`, `register(app, ...)` | — |
| `canvas.py` | create, edit by section, read | canvas code in both `slackdwn.py` |
| `ingest.py` | thread to messages, mention replacement, file to content block, safe file download | imbi `messages.py` and the private bot's copy |
| `progress.py` | task-card progress on a stream, `assistant.threads.setStatus` fallback | imbi `_StatusReporter` and the private bot's status edits |
| `inflight.py` | in-flight counter and drain for graceful shutdown | both `inflight.py` |
| `toolkit.py` | `Toolkit`, tool definitions, `anthropic()`, `call()` | — |

### 3.2 Dependencies

- `slack-sdk`: Web API client and `chat_stream` helper.
- `slack-bolt`: only for `forms.register()`. Both bots use Bolt.
- `pydantic`: specs and tool input schemas.
- `markdown-it-py`: splits text over 12,000 characters at block
  boundaries.

Optional extras for form stores (section 7.2):

- `slackwright[valkey]`: `valkey`.
- `slackwright[postgres]`: `psycopg[pool]` 3.

An MCP adapter (`toolkit.mcp(server)`, extra `slackwright[mcp]`) is
deferred until a bot needs it. `toolkit.anthropic()` gives the tool
list with descriptions, and its schemas are plain JSON Schema, so an
MCP adapter is a small addition later.

slackwright does not depend on the `anthropic` package. Content blocks
and tool definitions are plain dicts with `TypedDict` types.

## 4. Toolkit

### 4.1 API

```python
toolkit = slackwright.Toolkit(
    client,                     # slack_sdk AsyncWebClient
    channel_id='C123',
    thread_ts='1700000000.000100',
    user_id='U123',             # the user the agent acts for
    team_id='T123',
    forms=form_runtime,         # optional; enables slack_create_form
    can_post=None,              # optional access check, see 4.3
    max_posts=5,                # per Toolkit (per turn)
    max_forms=1,
)

toolkit.anthropic()             # list of {'name', 'description', 'input_schema'}
result = await toolkit.call(name, arguments)   # ToolResult
```

A bot makes one `Toolkit` for each agent turn.

`call()` validates `arguments` with the tool's Pydantic model. It never
raises for a bad input or a Slack API error. It returns
`ToolResult(text: str, is_error: bool)`. `text` is JSON:
`{"ok": true, ...}` or `{"ok": false, "error": "..."}`. The error text
tells the agent what to change, so that the agent can correct the spec
and call again. `is_error` maps to the `is_error` flag of an Anthropic
`tool_result`.

### 4.2 Tools

| Tool | Input | Result |
|---|---|---|
| `slack_post_markdown` | `text` | `ts` list |
| `slack_post_blocks` | `blocks: list[BlockSpec]`, `fallback_text` | `ts` |
| `slack_create_form` | `FormSpec` (includes `mode: modal \| inline`) | `form_id`, `ts` |
| `slack_canvas_create` | `title`, `markdown`, `channel_canvas: bool` | `canvas_id` |
| `slack_canvas_edit` | `canvas_id`, `operation`, `markdown`, `section_contains` (required for `replace`, `delete`, `insert_after`, `insert_before`) | `ok` |
| `slack_canvas_read` | `canvas_id` | `markdown` |

The `slack_post_markdown` description tells the agent to use it only
for content that is separate from its final answer, because the bot
posts the final answer itself.

Each description says what the tool does, the limits (characters,
fields, rows), and one small example input. The descriptions and the
Pydantic `Field(description=...)` text are the only documentation the
agent gets, so the tests check that each field has a description.

### 4.3 Scope and access

- By default, every post goes to the bound `channel_id` and
  `thread_ts`. No tool input can change the channel.
- A bot that needs more passes `can_post(channel_id, user_id) -> bool`
  (async). The tools then accept an optional `channel_id`, for example
  to post to any channel the bot and the user are both members of.
- `slack_canvas_read` and `slack_canvas_edit` check that the canvas is
  shared with the bound channel, unless `can_post` allows more.
- Thread binding stops posts to other channels. It does not stop spam
  or phishing in the bound thread. So:
  - `max_posts` and `max_forms` limit each `Toolkit`. A call over the
    limit returns an error.
  - Each form shows a context line: "Form created by the assistant".
  - Form validation rejects a field whose label or hint asks for a
    password, token, API key, or secret (a small word list).
  - The tools neutralize broadcast mentions (`<!channel>`, `<!here>`,
    `<!everyone>`, user groups) in agent text, unless the bot sets
    `allow_broadcasts`.

## 5. Rendering

### 5.1 `post_markdown(client, channel_id, thread_ts, text)`

1. If the text is empty, return an empty list.
2. If the text has 12,000 characters or fewer, post one message with
   `markdown_text`. Slack does not allow `text` or `blocks` with
   `markdown_text` (`markdown_text_conflict`).
3. If the text is longer, split it at top-level block boundaries
   (markdown-it tokens) into parts of 12,000 characters or fewer. Never
   split inside a code block or a table. Post each part as one message.
4. If one block alone is longer than 12,000 characters (for example, a
   very long code block), upload it as a file snippet.

### 5.2 `Stream`

`Stream` always uses `chunks`. It uses `slack_sdk`'s `chat_stream`
helper if the helper accepts `chunks`. If not, it calls the three
methods directly (check this first in phase 1).

- `await stream.text(markdown)`: append text.
- `await stream.task(id, title, status, details=None, output=None,
  sources=None)`: add or update a task card.
- `await stream.close()`: stop the stream.

A stream does not restart itself. When a stream is near 10 minutes,
12,000 characters, or 50 tasks, `Stream` marks open tasks complete or
error, stops the stream, and sends the remaining text with
`post_markdown()`. Restarts would split task cards across messages and
need state to carry over, so they are not worth it in the first
version.

### 5.3 Progress

`progress.Reporter` gives the bot one interface for tool-call progress:
`start(tool_name, summary)`, `finish(tool_name, ok, output)`. When the
bot uses a `Stream`, the reporter sends `task_update` chunks. When it
does not, the reporter calls `assistant.threads.setStatus`.

## 6. Blocks

`BlockSpec` is a Pydantic discriminated union of a small set of blocks.
The agent writes specs. slackwright writes Block Kit.

Set: `markdown`, `header`, `divider`, `context`, `image`, `table`.

The Alert, Card, and Carousel blocks (April 2026) are deferred until a
bot needs them.

No free buttons or other interactive elements in the first version.
Interactive elements need routing, and forms are the only routed
interaction in this design.

## 7. Forms

### 7.1 Spec

```python
class FormSpec(pydantic.BaseModel):
    title: str                    # max 24 characters (modal title limit)
    intro: str | None             # Markdown above the fields (see below)
    fields: list[Field]           # 1 to 40 fields
    submit_label: str = 'Submit'
    mode: Literal['modal', 'inline'] = 'modal'
    respondents: Literal['requester', 'anyone'] | list[str] = 'requester'
    single_submission: bool = True
```

`Field` is a discriminated union. Each type has `id`, `label`,
`required`, optional `hint`, `placeholder`, and `initial`:

| Type | Block Kit element | Extra |
|---|---|---|
| `text` | `plain_text_input` | `multiline`, `min_length`, `max_length` |
| `number` | `number_input` | `decimal`, `min`, `max` |
| `email` | `email_text_input` | — |
| `url` | `url_text_input` | — |
| `date` | `datepicker` | — |
| `time` | `timepicker` | — |
| `select` | `static_select` | `options` (max 100) |
| `multi_select` | `multi_static_select` | `options`, `max_selected` |
| `radio` | `radio_buttons` | `options` (max 10) |
| `checkboxes` | `checkboxes` | `options` (max 10) |
| `user` / `users` | `users_select` / `multi_users_select` | — |
| `channel` / `channels` | `conversations_select` / `multi_conversations_select` | — |

Validation rules: field `id` values are unique, the block count fits
the mode (50 for inline, 100 for modal), labels and options fit
Slack's limits, and no label or hint asks for a secret (4.3). A
failure returns an error to the agent before anything is posted.

`intro` rendering: in a message, a `markdown` block. In a modal, the
`markdown` block is not supported, so slackwright converts the intro
to mrkdwn (bold, italic, links, lists only) in a `section` block.

### 7.2 Runtime

```python
form_runtime = slackwright.forms.Runtime(
    store=valkey_store,           # FormStore
    on_submit=handle_submission,  # async (Submission) -> None
)
form_runtime.register(bolt_app)   # adds action and view handlers
```

`FormStore` protocol:

```python
class FormStore(Protocol):
    async def put(self, record: FormRecord, ttl: int) -> None: ...
    async def get(self, form_id: str) -> FormRecord | None: ...
    async def claim(self, form_id: str, user_id: str) -> bool: ...
```

`claim()` is atomic (for example, Redis `SET NX`). It returns `False`
if the key is already claimed. The key is `form_id` when
`single_submission` is set, and `(form_id, user_id)` when it is not.
The second key stops a double-click from making two submissions. slackwright ships
slackwright ships three stores:

| Store | Install | Backend |
|---|---|---|
| `MemoryFormStore` | core | dict; tests and single-process bots |
| `ValkeyFormStore` | `slackwright[valkey]` | `valkey` asyncio client |
| `PostgresFormStore` | `slackwright[postgres]` | `psycopg` 3 async pool |

Each store takes a client or pool that the bot already has. It does
not open or close connections itself.

- **Valkey:** `put` is `SET key value EX ttl`. `claim` is
  `SET claim_key 1 NX EX ttl`. Expiry is automatic. Keys use a
  configurable prefix (default `slackwright:form:`).
- **PostgreSQL:** two tables, `slackwright_forms (form_id PK, record
  JSONB, expires_at)` and `slackwright_form_claims (claim_key PK,
  form_id, claimed_at)`. `claim` is `INSERT ... ON CONFLICT DO NOTHING`
  and checks the row count. `get` ignores rows past `expires_at`.
  `await store.create_schema()` runs `CREATE TABLE IF NOT EXISTS`, and
  `await store.purge_expired()` deletes old rows; the bot calls it on a
  schedule. Table names are configurable.

A bot with a different backend implements the protocol itself.

`FormRecord` holds the spec, `channel_id`, `thread_ts`, `message_ts`,
the requester, and the creation time. The default TTL is 7 days.

All action IDs start with `slackwright:`, so `register()` matches only
slackwright interactions.

### 7.3 Modal flow

1. The agent calls `slack_create_form` with `mode='modal'`.
2. slackwright stores the record and posts a message with the intro and
   an "Open form" button (`action_id=slackwright:open`,
   `value=<form_id>`).
3. A user clicks. The action handler acknowledges first. Then it loads
   the record, checks `respondents`, and calls `views.open` with the
   `trigger_id` (it expires 3 seconds after the click).
   `private_metadata` holds the `form_id`.
4. The user submits. The view handler, in this order:
   1. Loads the record and checks `respondents` again.
   2. Validates the values against the spec. On an error, it returns
      `response_action: errors`, and Slack shows the error on the
      field.
   3. Calls `claim()`. If it returns `False`, it returns
      `response_action: update` with an "Already submitted" view.
   4. Acknowledges.
5. In a tracked background task (`inflight`), it updates the original
   message to "Submitted by @user" and runs `on_submit`. If the update
   fails (for example, the message was deleted), it still runs
   `on_submit`.

### 7.4 Inline flow

1. The agent calls `slack_create_form` with `mode='inline'`.
2. slackwright stores the record and posts a message with the input
   blocks and a submit button (`action_id=slackwright:submit`).
3. The user clicks submit. The `block_actions` payload holds
   `state.values`. The handler checks `respondents` and validates.
   Slack does not enforce `optional: false` in a message, so
   slackwright checks required fields itself.
4. On an error, Slack cannot show field errors in a message. The
   handler posts an ephemeral message to that user with the errors.
5. On success: `claim()`, replace the inputs with a read-only summary,
   and run `on_submit`.

### 7.5 Expired forms

When `get()` returns `None` (the TTL ended):

- Open click: acknowledge, send the user an ephemeral "This form has
  expired" message, and change the form message to show that it
  expired.
- Modal submit: return `response_action: errors` on the first field
  with "This form has expired". A modal can be open when the TTL ends.
- Inline submit: same as the open click.

### 7.6 Submission

```python
@dataclasses.dataclass
class Submission:
    form_id: str
    spec: FormSpec
    values: dict[str, Any]   # field id -> typed value
    user_id: str
    channel_id: str
    thread_ts: str
```

The bot decides what to do. The expected use: start a new agent turn
in the thread with the values as input. The values are untrusted
data, and with `respondents='anyone'` they come from people other
than the requester. The bot must mark them as data, not as
instructions, in that turn.

## 8. Canvases

- `create(title, markdown, channel_canvas=False)`: by default, call
  `canvases.create` with `channel_id` set to the bound channel. With
  `channel_canvas=True`, call `conversations.canvases.create`. A
  channel has only one channel canvas, so return a clear error on
  `channel_canvas_already_exists`. Some Slack plans cannot create
  canvases; return Slack's error text as is.
- `edit(canvas_id, operation, markdown, section_contains=None)`:
  `operation` is one of `insert_at_start`, `insert_at_end`,
  `insert_after`, `insert_before`, `replace`, `delete`, `rename`. For a
  section-based operation, find the section with
  `canvases.sections.lookup` and `section_contains`. Return an error
  unless exactly one section matches. `replace` and `delete` always
  need a section, so the tool cannot erase a whole canvas.
- `read(canvas_id)`: `canvases.getContent` with
  `content_type=markdown`.

Required scopes: `canvases:read`, `canvases:write`.

The Markdown that goes into a canvas is standard Markdown. No mrkdwn
conversion is necessary.

## 9. Ingest

### 9.1 Thread to messages

`ingest.thread(client, channel_id, thread_ts, bot_user_id)` returns
Anthropic-format messages:

- Reads all pages of `conversations.replies`.
- Gives the bot's messages the `assistant` role and all other messages
  the `user` role. It merges turns with the same role that follow each
  other.
- Files in the bot's own messages are not put in an `assistant` turn,
  because the API accepts image and document blocks only in user
  turns. Ingest replaces them with a short text note.
- Replaces `<@U123>` with display names (cached).
- Downloads files and turns them into content blocks.

A bot that needs extra steps calls the lower-level functions
(`file_blocks()`, `replace_mentions()`) and keeps its own loop.

### 9.2 Files

`file_to_block(mimetype, content)` is the shared logic from both bots:
PDF to `document`, supported images to `image`, text and text-like
types to `text`, otherwise an error text block.

`download(url)` follows these rules:

- Sends the bot token only to Slack file hosts. A request hook checks
  each request and each redirect. This fixes the gap in imbi-slackbot,
  which sends the token to any `url_private`.
- Stops at a maximum size (default 10 MB, as in both bots today) and
  returns an error block.

## 10. Lifecycle

`inflight.track()` and `inflight.wait_for_drain()` are the shared code
from both bots. `forms.Runtime` uses `track()` for `on_submit` tasks.

## 11. Testing

- `unittest`, as in the bots. Coverage target: 90%.
- No mocks of `slack_sdk`. A small fake Slack HTTP server (stdlib
  `http.server` in a thread, or `aiohttp` test server) records calls
  and returns fixture responses. Tests point `AsyncWebClient(base_url=)`
  at it.
- Golden tests: each `FormSpec` and `BlockSpec` fixture has the
  expected Block Kit JSON in `tests/data/`.
- A test checks that every tool input field has a description.

## 12. Phases

| # | Work | Size | Done when |
|---|---|---|---|
| 1 | `render`, `blocks`, `toolkit` with `slack_post_markdown` and `slack_post_blocks`, `ingest`, `progress`, `inflight` | M | Tests pass; `post_markdown` posts a 30,000-character answer with a table and code in correct parts against a real test workspace. Test the table limits in a `markdown` block first, because they are not documented |
| 2 | `forms`: specs, modal and inline flows, the three stores, `register()`, `slack_create_form` | L | An agent in a test workspace designs, posts, and receives a 5-field form in both modes; the same store test suite passes for all three stores against real Valkey and PostgreSQL |
| 3 | `canvas` and the three canvas tools | S | Create, edit a section, and read back match |
| 4 | Move imbi-slackbot | S | `slackdwn.py`, `inflight.py`, and file code deleted from imbi; imbi tests pass; token host check active |
| 5 | Move the private bot, with its own tool adapter and shared `FormStore` | L | Its `slackdwn.py` and `inflight.py` deleted; its tests pass |

Release 0.1.0 to PyPI after phase 1, so that phase 4 can start early
if necessary.

## 13. Decisions and open questions

Decided:

1. Default `respondents` is `requester`. `anyone` is an explicit
   choice in the spec.
2. Text over 12,000 characters is split into messages, not put in a
   canvas.
3. Streaming is opt-in for each bot in phase 1. It can become the
   default after both bots use it without problems.
4. Alert, Card, and Carousel blocks are deferred (section 6).
5. slackwright ships optional Valkey and PostgreSQL form stores
   (section 7.2). Each bot picks one in its migration phase.
