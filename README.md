# slackwright

Building blocks for AI agents that live in Slack.

slackwright is a library, not a framework. Your bot keeps its own event
loop and agent; slackwright gives it the Slack parts:

- **Render**: post LLM Markdown answers with Slack's native Markdown
  support, split long answers, and stream replies with task cards.
- **Tools**: expose the parts below to an LLM agent as tools, with
  Anthropic tool definitions and validated inputs.
- **Forms**: forms that an agent designs, in a modal or inline in a
  message, with validated submissions.
- **Canvases**: create, edit, and read canvas documents.
- **Ingest**: turn a Slack thread, its mentions, and its files into LLM
  messages.
- **Lifecycle**: progress updates and an in-flight drain for graceful
  shutdown.

> Status: early development. The API will change.

## Installation

```bash
pip install slackwright
pip install 'slackwright[valkey]'    # ValkeyFormStore
pip install 'slackwright[postgres]'  # PostgresFormStore
```

## Example

```python
import slackwright
from slackwright import forms, stores

runtime = forms.Runtime(stores.ValkeyFormStore(valkey), on_submit)
runtime.register(bolt_app)  # handles form buttons and modals

toolkit = slackwright.Toolkit(
    slack_client,
    channel_id=channel_id,
    thread_ts=thread_ts,
    user_id=user_id,
    forms=runtime,
    canvases=True,
)
tools = toolkit.anthropic()                     # give these to Claude
result = await toolkit.call(name, tool_input)   # run a tool_use block
```

See [docs/design.md](docs/design.md) for the design.

## Development

```bash
uv sync --all-groups
docker compose up -d --wait   # Valkey and PostgreSQL for integration tests
uv run pre-commit run --all-files
uv run coverage run
uv run coverage report
```

Without the compose services, the Valkey and PostgreSQL tests are
skipped. When `VALKEY_URL` or `POSTGRES_URL` is set (CI sets both),
those tests always run against that URL.

## License

BSD 3-Clause. See [LICENSE](LICENSE).
