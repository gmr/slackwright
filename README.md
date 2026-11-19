# slackwright

Building blocks for AI agents that live in Slack.

slackwright is a library, not a framework. Your bot keeps its own event
loop and agent; slackwright gives it the Slack parts:

- **Render**: post LLM Markdown answers to Slack, with code snippets as
  files and large tables as canvases.
- **Ingest**: turn a Slack thread, its mentions, and its files into LLM
  messages.
- **Forms**: Block Kit input forms, in modals or inline in a message,
  with validated submissions.
- **Canvases**: create, edit, and read canvas documents.
- **Lifecycle**: assistant status updates and in-flight drain for
  graceful shutdown.

> Status: early development. The API will change.

## Installation

```bash
pip install slackwright
```

## License

BSD 3-Clause. See [LICENSE](LICENSE).
