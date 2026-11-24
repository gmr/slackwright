"""Block Kit forms that an agent designs and Slack users fill in.

An agent writes a :class:`FormSpec`. :class:`Runtime` posts it as a
message, either with an "Open form" button (``mode='modal'``) or with
the inputs in the message (``mode='inline'``), and handles the Slack
interactions. When a user submits valid answers, the runtime calls the
bot's ``on_submit`` callback with a :class:`Submission`.

The button listener does its work after ``ack()``. The modal listener
reads and claims the form in the store before ``ack()``, because the
``ack()`` response can carry ``response_action``, so those store calls
count against Slack's 3-second limit. The message update and
``on_submit`` run after ``ack()``. Bolt sends the acknowledgement
first when ``process_before_response`` is ``False`` (the default), so
that slow work does not pass the limit.

"""

from __future__ import annotations

import collections.abc
import dataclasses
import logging
import re
import secrets
import time
import typing

import aiohttp
import pydantic
from slack_sdk import errors

from slackwright import blocks as block_specs
from slackwright import inflight, render

LOGGER = logging.getLogger(__name__)

ACTION_OPEN = 'slackwright:open'
ACTION_SUBMIT = 'slackwright:submit'
CALLBACK_ID = 'slackwright:form'
ACTION_PATTERN = re.compile(r'^slackwright:(?:open|submit)$')

DEFAULT_TTL = 7 * 24 * 60 * 60
"""Seconds that a form can be answered."""

MAX_FIELDS = 40
CREATED_BY = 'Form created by the assistant'

_VALUE = 'value'
"""The ``action_id`` of every input element."""

_SECRET_WORDS = re.compile(
    r'\b(?:passwords?|passcodes?|passphrases?|tokens?|api[ _-]?keys?|'
    r'secrets?|credentials?|private[ _-]?keys?|ssn|social security|'
    r'credit[ _-]?cards?|card numbers?|cvv|cvc)\b',
    re.IGNORECASE,
)
_EMAIL = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
_USER_ID = r'^[UW][A-Z0-9]+$'
_CHANNEL_ID = r'^[CDG][A-Z0-9]+$'

_Option = typing.Annotated[str, pydantic.Field(min_length=1, max_length=75)]
_Placeholder = typing.Annotated[
    str | None,
    pydantic.Field(max_length=150, description='Grey text in the input.'),
]


_plain = block_specs.plain_text


def _options(options: list[str]) -> list[dict[str, typing.Any]]:
    return [{'text': _plain(o), 'value': o} for o in options]


class _Field(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra='forbid')

    id: str = pydantic.Field(
        pattern=r'^[a-z][a-z0-9_]{0,49}$',
        description='Unique field ID: lower-case letters, digits, and _.',
    )
    label: str = pydantic.Field(
        min_length=1, max_length=2000, description='The question.'
    )
    required: bool = pydantic.Field(
        default=True, description='True if the user must answer.'
    )
    hint: str | None = pydantic.Field(
        default=None, max_length=2000, description='Help text below it.'
    )

    def texts(self) -> list[str]:
        """Return the texts that the user reads, for the secret check."""
        texts = [self.label, self.hint, getattr(self, 'placeholder', None)]
        return [text for text in texts if text]

    def block(self) -> dict[str, typing.Any]:
        """Return the Block Kit ``input`` block for this field."""
        block: dict[str, typing.Any] = {
            'type': 'input',
            'block_id': self.id,
            'label': _plain(self.label),
            'optional': not self.required,
            'element': {'action_id': _VALUE, **self.element()},
        }
        if self.hint:
            block['hint'] = _plain(self.hint)
        return block

    def element(self) -> dict[str, typing.Any]:
        raise NotImplementedError

    def convert(self, state: dict[str, typing.Any]) -> typing.Any:
        """Return the typed answer from a ``state.values`` entry.

        Raises :class:`ValueError` with a message for the user.

        """
        value = self.read(state)
        if value in (None, '', []):
            if self.required:
                raise ValueError('This field is required.')
            return [] if isinstance(value, list) else None
        return self.check(value)

    def read(self, state: dict[str, typing.Any]) -> typing.Any:
        return state.get('value')

    def check(self, value: typing.Any) -> typing.Any:
        return value


class TextField(_Field):
    """Free text, on one line or more."""

    type: typing.Literal['text']
    multiline: bool = pydantic.Field(
        default=False, description='True for a large text box.'
    )
    min_length: int | None = pydantic.Field(
        default=None, ge=0, le=3000, description='Minimum characters.'
    )
    max_length: int | None = pydantic.Field(
        default=None, ge=1, le=3000, description='Maximum characters.'
    )
    placeholder: _Placeholder = None
    initial: str | None = pydantic.Field(
        default=None, max_length=3000, description='Text filled in first.'
    )

    def element(self) -> dict[str, typing.Any]:
        element: dict[str, typing.Any] = {
            'type': 'plain_text_input',
            'multiline': self.multiline,
        }
        if self.min_length is not None:
            element['min_length'] = self.min_length
        if self.max_length is not None:
            element['max_length'] = self.max_length
        if self.placeholder:
            element['placeholder'] = _plain(self.placeholder)
        if self.initial:
            element['initial_value'] = self.initial
        return element

    def check(self, value: typing.Any) -> typing.Any:
        text = str(value)
        if self.min_length is not None and len(text) < self.min_length:
            raise ValueError(f'Use at least {self.min_length} characters.')
        if self.max_length is not None and len(text) > self.max_length:
            raise ValueError(f'Use at most {self.max_length} characters.')
        return text


class NumberField(_Field):
    """A number, with optional limits."""

    type: typing.Literal['number']
    decimal: bool = pydantic.Field(
        default=False, description='True to allow decimals.'
    )
    min: float | None = pydantic.Field(default=None, description='Minimum.')
    max: float | None = pydantic.Field(default=None, description='Maximum.')
    placeholder: _Placeholder = None
    initial: float | None = pydantic.Field(
        default=None, description='Number filled in first.'
    )

    def element(self) -> dict[str, typing.Any]:
        element: dict[str, typing.Any] = {
            'type': 'number_input',
            'is_decimal_allowed': self.decimal,
        }
        for key, value in (
            ('min_value', self.min),
            ('max_value', self.max),
            ('initial_value', self.initial),
        ):
            if value is not None:
                element[key] = _number_text(value)
        if self.placeholder:
            element['placeholder'] = _plain(self.placeholder)
        return element

    def check(self, value: typing.Any) -> typing.Any:
        try:
            number = float(value) if self.decimal else int(value)
        except (TypeError, ValueError):
            kind = 'a number' if self.decimal else 'a whole number'
            raise ValueError(f'Enter {kind}.') from None
        if self.min is not None and number < self.min:
            raise ValueError(f'Enter {_number_text(self.min)} or more.')
        if self.max is not None and number > self.max:
            raise ValueError(f'Enter {_number_text(self.max)} or less.')
        return number


class AddressField(_Field):
    """An email address or a web URL."""

    type: typing.Literal['email', 'url']
    placeholder: _Placeholder = None
    initial: str | None = pydantic.Field(
        default=None, max_length=3000, description='Value filled in first.'
    )

    def element(self) -> dict[str, typing.Any]:
        kind = 'email_text_input' if self.type == 'email' else 'url_text_input'
        element: dict[str, typing.Any] = {'type': kind}
        if self.placeholder:
            element['placeholder'] = _plain(self.placeholder)
        if self.initial:
            element['initial_value'] = self.initial
        return element

    def check(self, value: typing.Any) -> typing.Any:
        text = str(value).strip()
        if self.type == 'email' and not _EMAIL.match(text):
            raise ValueError('Enter an email address.')
        if self.type == 'url' and not text.startswith(('https://', 'http://')):
            raise ValueError('Enter a URL that starts with https://.')
        return text


class DateTimeField(_Field):
    """A date (YYYY-MM-DD) or a time of day (HH:MM, 24-hour)."""

    type: typing.Literal['date', 'time']
    placeholder: _Placeholder = None
    initial: str | None = pydantic.Field(
        default=None,
        description='YYYY-MM-DD for a date, HH:MM for a time.',
    )

    @pydantic.model_validator(mode='after')
    def _check_initial(self) -> DateTimeField:
        if self.initial and not re.match(self._pattern(), self.initial):
            raise ValueError(f'initial must match {self._format()}')
        return self

    def element(self) -> dict[str, typing.Any]:
        element: dict[str, typing.Any] = {
            'type': 'datepicker' if self.type == 'date' else 'timepicker'
        }
        if self.placeholder:
            element['placeholder'] = _plain(self.placeholder)
        if self.initial:
            key = 'initial_date' if self.type == 'date' else 'initial_time'
            element[key] = self.initial
        return element

    def read(self, state: dict[str, typing.Any]) -> typing.Any:
        key = 'selected_date' if self.type == 'date' else 'selected_time'
        return state.get(key)

    def check(self, value: typing.Any) -> typing.Any:
        if not re.match(self._pattern(), str(value)):
            raise ValueError(f'Use the format {self._format()}.')
        return str(value)

    def _pattern(self) -> str:
        if self.type == 'date':
            return r'^\d{4}-\d{2}-\d{2}$'
        return r'^\d{2}:\d{2}$'

    def _format(self) -> str:
        return 'YYYY-MM-DD' if self.type == 'date' else 'HH:MM'


class ChoiceField(_Field):
    """A choice from a list: one (select, radio) or many (multi_select,
    checkboxes)."""

    type: typing.Literal['select', 'multi_select', 'radio', 'checkboxes']
    options: list[_Option] = pydantic.Field(
        min_length=1,
        max_length=100,
        description=(
            'The choices, max 75 characters each. Max 100 for select and '
            'multi_select, 10 for radio and checkboxes.'
        ),
    )
    max_selected: int | None = pydantic.Field(
        default=None, ge=1, description='multi_select only: most choices.'
    )
    placeholder: _Placeholder = None
    initial: str | list[str] | None = pydantic.Field(
        default=None,
        description=(
            'Choice selected first: one option for select and radio, a '
            'list of options for multi_select and checkboxes.'
        ),
    )

    @property
    def multiple(self) -> bool:
        return self.type in ('multi_select', 'checkboxes')

    @pydantic.model_validator(mode='after')
    def _check_choices(self) -> ChoiceField:
        if len(set(self.options)) != len(self.options):
            raise ValueError('options must be unique')
        if self.type in ('radio', 'checkboxes') and len(self.options) > 10:
            raise ValueError(f'{self.type} allows at most 10 options')
        if self.max_selected is not None and self.type != 'multi_select':
            raise ValueError('max_selected is only for multi_select')
        if self.initial is None:
            return self
        if self.multiple != isinstance(self.initial, list):
            kind = 'a list of options' if self.multiple else 'one option'
            raise ValueError(f'initial must be {kind} for {self.type}')
        chosen = (
            self.initial if isinstance(self.initial, list) else [self.initial]
        )
        if not set(chosen) <= set(self.options):
            raise ValueError('initial must be one of the options')
        return self

    def element(self) -> dict[str, typing.Any]:
        kind = {
            'select': 'static_select',
            'multi_select': 'multi_static_select',
            'radio': 'radio_buttons',
            'checkboxes': 'checkboxes',
        }[self.type]
        element: dict[str, typing.Any] = {
            'type': kind,
            'options': _options(self.options),
        }
        if self.max_selected is not None:
            element['max_selected_items'] = self.max_selected
        if self.placeholder and self.type in ('select', 'multi_select'):
            element['placeholder'] = _plain(self.placeholder)
        if isinstance(self.initial, list) and self.initial:
            element['initial_options'] = _options(self.initial)
        elif isinstance(self.initial, str):
            element['initial_option'] = _options([self.initial])[0]
        return element

    def read(self, state: dict[str, typing.Any]) -> typing.Any:
        if self.multiple:
            return [o['value'] for o in state.get('selected_options') or []]
        return (state.get('selected_option') or {}).get('value')

    def check(self, value: typing.Any) -> typing.Any:
        chosen = value if isinstance(value, list) else [value]
        if not set(chosen) <= set(self.options):
            raise ValueError('Choose from the options.')
        if self.max_selected is not None and len(chosen) > self.max_selected:
            raise ValueError(f'Choose at most {self.max_selected}.')
        return value


class PickerField(_Field):
    """Slack users or channels: one (user, channel) or many (users,
    channels)."""

    type: typing.Literal['user', 'users', 'channel', 'channels']
    placeholder: _Placeholder = None
    initial: str | list[str] | None = pydantic.Field(
        default=None,
        description=(
            'Selected first: a user ID (U...) or channel ID (C...), or a '
            'list of IDs for users and channels.'
        ),
    )

    @property
    def multiple(self) -> bool:
        return self.type in ('users', 'channels')

    @pydantic.model_validator(mode='after')
    def _check_initial(self) -> PickerField:
        if self.initial is None:
            return self
        if self.multiple != isinstance(self.initial, list):
            kind = 'a list of IDs' if self.multiple else 'one ID'
            raise ValueError(f'initial must be {kind} for {self.type}')
        pattern = _USER_ID if self.type.startswith('user') else _CHANNEL_ID
        ids = (
            self.initial if isinstance(self.initial, list) else [self.initial]
        )
        if not all(re.match(pattern, value) for value in ids):
            raise ValueError(f'initial has an ID that is not a {self.type}')
        return self

    def element(self) -> dict[str, typing.Any]:
        kind, initial_key = {
            'user': ('users_select', 'initial_user'),
            'users': ('multi_users_select', 'initial_users'),
            'channel': ('conversations_select', 'initial_conversation'),
            'channels': (
                'multi_conversations_select',
                'initial_conversations',
            ),
        }[self.type]
        element: dict[str, typing.Any] = {'type': kind}
        if self.placeholder:
            element['placeholder'] = _plain(self.placeholder)
        if self.initial:
            element[initial_key] = self.initial
        return element

    def read(self, state: dict[str, typing.Any]) -> typing.Any:
        key = {
            'user': 'selected_user',
            'users': 'selected_users',
            'channel': 'selected_conversation',
            'channels': 'selected_conversations',
        }[self.type]
        return state.get(key) or ([] if self.multiple else None)


FieldSpec = typing.Annotated[
    TextField
    | NumberField
    | AddressField
    | DateTimeField
    | ChoiceField
    | PickerField,
    pydantic.Field(discriminator='type'),
]


class FormSpec(pydantic.BaseModel):
    """A form that an agent designs."""

    model_config = pydantic.ConfigDict(extra='forbid')

    title: str = pydantic.Field(
        min_length=1, max_length=24, description='Short title, max 24.'
    )
    intro: str | None = pydantic.Field(
        default=None,
        max_length=2000,
        description='Markdown above the fields: why you ask.',
    )
    fields: list[FieldSpec] = pydantic.Field(
        min_length=1,
        max_length=MAX_FIELDS,
        description=f'The questions, top to bottom. Max {MAX_FIELDS}.',
    )
    submit_label: str = pydantic.Field(
        default='Submit',
        min_length=1,
        max_length=24,
        description='Text of the submit button.',
    )
    mode: typing.Literal['modal', 'inline'] = pydantic.Field(
        default='modal',
        description=(
            'modal: the message has an "Open form" button that opens a '
            'dialog. inline: the inputs are in the message. Use inline '
            'for 1 to 3 short questions.'
        ),
    )
    respondents: (
        typing.Literal['requester', 'anyone']
        | typing.Annotated[
            list[typing.Annotated[str, pydantic.Field(pattern=_USER_ID)]],
            pydantic.Field(min_length=1, max_length=50),
        ]
    ) = pydantic.Field(
        default='requester',
        description=(
            'Who can answer: requester (the user you act for), anyone '
            'in the channel, or a list of user IDs.'
        ),
    )
    single_submission: bool = pydantic.Field(
        default=True,
        description=(
            'True: the form closes after one answer. False: each '
            'respondent can answer once.'
        ),
    )

    @pydantic.model_validator(mode='after')
    def _check_fields(self) -> FormSpec:
        ids = [field.id for field in self.fields]
        if len(set(ids)) != len(ids):
            raise ValueError('field IDs must be unique')
        texts = [self.title, self.intro or '', self.submit_label] + [
            text for field in self.fields for text in field.texts()
        ]
        for text in texts:
            if match := _SECRET_WORDS.search(text):
                raise ValueError(
                    f'forms cannot ask for secrets ({match.group(0)!r}); '
                    'ask users to never share passwords, tokens, or keys'
                )
        return self


class FormRecord(pydantic.BaseModel):
    """A posted form, as a :class:`FormStore` keeps it."""

    form_id: str
    spec: FormSpec
    channel_id: str
    thread_ts: str | None
    message_ts: str | None = None
    requester: str
    created_at: float
    metadata: dict[str, typing.Any] = pydantic.Field(default_factory=dict)
    """Data of the bot, given back on each :class:`Submission`. Slack
    users never see it."""

    def allows(self, user_id: str) -> bool:
        """Return ``True`` if ``user_id`` can answer this form."""
        respondents = self.spec.respondents
        if respondents == 'anyone':
            return True
        if respondents == 'requester':
            return user_id == self.requester
        return user_id in respondents

    def claim_key(self, user_id: str) -> str:
        if self.spec.single_submission:
            return self.form_id
        return f'{self.form_id}:{user_id}'


class FormStore(typing.Protocol):
    """Keeps posted forms between the post and the answers.

    ``claim`` must be atomic: of two calls with the same key, only one
    returns ``True`` until ``ttl`` seconds pass, or until ``release``
    removes the claim.

    """

    async def put(self, record: FormRecord, ttl: int) -> None: ...

    async def get(self, form_id: str) -> FormRecord | None: ...

    async def claim(self, key: str, ttl: int) -> bool: ...

    async def release(self, key: str) -> None: ...


class MemoryFormStore:
    """A :class:`FormStore` in process memory.

    For tests and bots with one process. Forms are lost when the
    process stops.

    """

    def __init__(self) -> None:
        self._records: dict[str, tuple[str, float]] = {}
        self._claims: dict[str, float] = {}

    async def put(self, record: FormRecord, ttl: int) -> None:
        self._records[record.form_id] = (
            record.model_dump_json(),
            time.monotonic() + ttl,
        )

    async def get(self, form_id: str) -> FormRecord | None:
        data, expires = self._records.get(form_id, ('', 0.0))
        if expires <= time.monotonic():
            self._records.pop(form_id, None)
            return None
        return FormRecord.model_validate_json(data)

    async def claim(self, key: str, ttl: int) -> bool:
        now = time.monotonic()
        if self._claims.get(key, 0.0) > now:
            return False
        self._claims[key] = now + ttl
        return True

    async def release(self, key: str) -> None:
        self._claims.pop(key, None)


@dataclasses.dataclass(frozen=True)
class Submission:
    """Valid answers to a form.

    ``values`` maps each field ID to its answer: ``str`` for text,
    email, url, date (YYYY-MM-DD), time (HH:MM), select, radio, user,
    and channel; ``int`` or ``float`` for number; ``list[str]`` for
    multi_select, checkboxes, users, and channels; ``None`` (or an
    empty list) for an optional field with no answer.

    The values are untrusted user input. Give them to an agent as
    data, not as instructions.

    """

    form_id: str
    spec: FormSpec
    values: dict[str, typing.Any]
    user_id: str
    channel_id: str
    thread_ts: str | None
    metadata: dict[str, typing.Any] = dataclasses.field(default_factory=dict)


class Rejected(Exception):  # noqa: N818 - reads as forms.Rejected
    """Raise in ``on_submit`` to refuse a submission.

    The user who submitted gets ``message`` as an ephemeral message.
    With ``retry`` (the default), the runtime releases the claim and
    shows the form again, so someone can submit it again. Without
    ``retry``, the form closes, and its message shows ``message``.

    """

    def __init__(self, message: str, *, retry: bool = True) -> None:
        super().__init__(message)
        self.message = message
        self.retry = retry


OnSubmit = collections.abc.Callable[
    [Submission], collections.abc.Awaitable[None]
]
Ack = collections.abc.Callable[..., collections.abc.Awaitable[typing.Any]]


class Runtime:
    """Post forms and handle their Slack interactions.

    Args:
        store: Where posted forms are kept until they are answered.
        on_submit: ``async on_submit(submission)``, called once for
            each valid submission.
        ttl: Seconds that a form can be answered.

    """

    def __init__(
        self,
        store: FormStore,
        on_submit: OnSubmit,
        *,
        ttl: int = DEFAULT_TTL,
    ) -> None:
        self._store = store
        self._on_submit = on_submit
        self._ttl = ttl

    def register(self, app: typing.Any) -> None:
        """Add the form listeners to a Bolt ``AsyncApp``."""
        app.action(ACTION_PATTERN)(self.handle_action)
        app.view(CALLBACK_ID)(self.handle_view)

    async def post(
        self,
        slack: typing.Any,
        spec: FormSpec,
        *,
        channel_id: str,
        thread_ts: str | None,
        requester: str,
        metadata: dict[str, typing.Any] | None = None,
    ) -> FormRecord:
        """Post ``spec`` as a message and store it. Return the record.

        ``metadata`` is data of the bot that comes back on each
        :class:`Submission`. It must be JSON-serializable.

        """
        record = FormRecord(
            form_id=secrets.token_urlsafe(12),
            spec=spec,
            channel_id=channel_id,
            thread_ts=thread_ts,
            requester=requester,
            created_at=time.time(),
            metadata=metadata or {},
        )
        # Store before posting, so that a fast click finds the form.
        await self._store.put(record, self._ttl)
        response = await slack.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            blocks=_message_blocks(record),
            text=f'Form: {spec.title}',
        )
        record.message_ts = response['ts']
        await self._store.put(record, self._ttl)
        return record

    async def handle_action(
        self, ack: Ack, body: dict[str, typing.Any], client: typing.Any
    ) -> None:
        """Bolt listener for the open and submit buttons."""
        await ack()
        action = body['actions'][0]
        user_id = body['user']['id']
        record = await self._store.get(action.get('value', ''))
        if record is None:
            await _expire_message(client, body, user_id)
            return
        if not record.allows(user_id):
            await _ephemeral(
                client, record, user_id, 'You cannot answer this form.'
            )
            return
        if action['action_id'] == ACTION_OPEN:
            if record.spec.mode == 'modal':
                await client.views_open(
                    trigger_id=body['trigger_id'], view=_modal(record)
                )
            return
        if record.spec.mode != 'inline':
            return
        values, problems = _parse(record.spec, body.get('state') or {})
        if problems:
            lines = [
                f'• {_label(record.spec, field_id)}: {message}'
                for field_id, message in problems.items()
            ]
            await _ephemeral(
                client,
                record,
                user_id,
                'Your answers were not sent:\n' + '\n'.join(lines),
            )
            return
        if not await self._store.claim(record.claim_key(user_id), self._ttl):
            await _ephemeral(
                client, record, user_id, 'This form was already answered.'
            )
            return
        await self._finish(client, record, user_id, values)

    async def handle_view(
        self, ack: Ack, body: dict[str, typing.Any], client: typing.Any
    ) -> None:
        """Bolt listener for the modal submission."""
        view = body['view']
        user_id = body['user']['id']
        first = _first_input(view)
        record = await self._store.get(view.get('private_metadata', ''))
        if record is None:
            await ack(
                response_action='errors',
                errors={first: 'This form has expired.'},
            )
            return
        if not record.allows(user_id):
            await ack(
                response_action='errors',
                errors={first: 'You cannot answer this form.'},
            )
            return
        values, problems = _parse(record.spec, view.get('state') or {})
        if problems:
            await ack(response_action='errors', errors=problems)
            return
        if not await self._store.claim(record.claim_key(user_id), self._ttl):
            await ack(
                response_action='update',
                view=_notice(record.spec, 'This form was already answered.'),
            )
            return
        await ack()
        await self._finish(client, record, user_id, values)

    async def _finish(
        self,
        client: typing.Any,
        record: FormRecord,
        user_id: str,
        values: dict[str, typing.Any],
    ) -> None:
        async with inflight.track():
            if record.spec.single_submission:
                await _update(
                    client,
                    record,
                    _answered_blocks(record, user_id, values),
                    f'Form: {record.spec.title} (answered)',
                )
            elif record.spec.mode == 'inline':
                await _ephemeral(
                    client, record, user_id, 'Thanks. Your answers were sent.'
                )
            submission = Submission(
                form_id=record.form_id,
                spec=record.spec,
                values=values,
                user_id=user_id,
                channel_id=record.channel_id,
                thread_ts=record.thread_ts,
                metadata=record.metadata,
            )
            # ponytail: the claim stays held after an unexpected error,
            # because on_submit may have done part of its work. Raise
            # Rejected to let the user submit again.
            try:
                await self._on_submit(submission)
            except Rejected as rejected:
                await self._reject(client, record, user_id, rejected)
            except Exception:
                LOGGER.exception(
                    'on_submit failed for form %s', record.form_id
                )
                await _ephemeral(
                    client,
                    record,
                    user_id,
                    'Your answers were received, but processing failed.',
                )

    async def _reject(
        self,
        client: typing.Any,
        record: FormRecord,
        user_id: str,
        rejected: Rejected,
    ) -> None:
        """Open the form again, or close it, and tell the user why."""
        if rejected.retry:
            await self._store.release(record.claim_key(user_id))
        if record.spec.single_submission:
            if rejected.retry:
                blocks = _message_blocks(record)
            else:
                blocks = [
                    {'type': 'header', 'text': _plain(record.spec.title)},
                    # plain_text, so the reason (often text from an
                    # API) makes no links, formatting, or mentions.
                    {
                        'type': 'context',
                        'elements': [
                            _plain(
                                f'Not sent: {rejected.message}'[
                                    : block_specs.TEXT_OBJECT_LIMIT
                                ]
                            )
                        ],
                    },
                ]
            await _update(client, record, blocks, f'Form: {record.spec.title}')
        await _ephemeral(client, record, user_id, rejected.message)


def _parse(
    spec: FormSpec, state: dict[str, typing.Any]
) -> tuple[dict[str, typing.Any], dict[str, str]]:
    """Return ``(values, problems)`` from a payload's ``state``."""
    entries = state.get('values') or {}
    values: dict[str, typing.Any] = {}
    problems: dict[str, str] = {}
    for field in spec.fields:
        entry = (entries.get(field.id) or {}).get(_VALUE) or {}
        try:
            values[field.id] = field.convert(entry)
        except ValueError as error:
            problems[field.id] = str(error)
    return values, problems


def _message_blocks(record: FormRecord) -> list[dict[str, typing.Any]]:
    spec = record.spec
    blocks: list[dict[str, typing.Any]] = [
        {'type': 'header', 'text': _plain(spec.title)}
    ]
    if spec.intro:
        blocks.append({'type': 'markdown', 'text': spec.intro})
    if spec.mode == 'inline':
        blocks += [field.block() for field in spec.fields]
    blocks.append(_context(CREATED_BY))
    action, label = (
        (ACTION_SUBMIT, spec.submit_label)
        if spec.mode == 'inline'
        else (ACTION_OPEN, 'Open form')
    )
    blocks.append(
        {
            'type': 'actions',
            'elements': [
                {
                    'type': 'button',
                    'action_id': action,
                    'text': _plain(label),
                    'style': 'primary',
                    'value': record.form_id,
                }
            ],
        }
    )
    return blocks


def _modal(record: FormRecord) -> dict[str, typing.Any]:
    spec = record.spec
    blocks: list[dict[str, typing.Any]] = []
    if spec.intro:
        blocks.append(
            {
                'type': 'section',
                'text': {
                    'type': 'mrkdwn',
                    'text': render.to_mrkdwn(spec.intro)[
                        : block_specs.TEXT_OBJECT_LIMIT
                    ],
                },
            }
        )
    blocks.append(_context(CREATED_BY))
    blocks += [field.block() for field in spec.fields]
    return {
        'type': 'modal',
        'callback_id': CALLBACK_ID,
        'private_metadata': record.form_id,
        'title': _plain(spec.title),
        'submit': _plain(spec.submit_label),
        'close': _plain('Cancel'),
        'blocks': blocks,
    }


def _notice(spec: FormSpec, text: str) -> dict[str, typing.Any]:
    return {
        'type': 'modal',
        'title': _plain(spec.title),
        'close': _plain('Close'),
        'blocks': [{'type': 'section', 'text': _plain(text)}],
    }


def _answered_blocks(
    record: FormRecord, user_id: str, values: dict[str, typing.Any]
) -> list[dict[str, typing.Any]]:
    blocks: list[dict[str, typing.Any]] = [
        {'type': 'header', 'text': _plain(record.spec.title)}
    ]
    if record.spec.mode == 'inline':
        # plain_text shows no links, formatting, or mentions, so user
        # answers cannot speak in the voice of the bot.
        blocks.extend(
            {
                'type': 'section',
                'text': _plain(
                    f'{field.label}\n{_display(values.get(field.id))}'[
                        : block_specs.TEXT_OBJECT_LIMIT
                    ]
                ),
            }
            for field in record.spec.fields
        )
    blocks.append(_context(f'Answered by <@{user_id}>'))
    return blocks


def _display(value: typing.Any) -> str:
    if value in (None, '', []):
        return '-'
    if isinstance(value, list):
        return ', '.join(str(item) for item in value)
    return str(value)


def _context(text: str) -> dict[str, typing.Any]:
    return {'type': 'context', 'elements': [{'type': 'mrkdwn', 'text': text}]}


def _label(spec: FormSpec, field_id: str) -> str:
    return next(f.label for f in spec.fields if f.id == field_id)


def _first_input(view: dict[str, typing.Any]) -> str:
    return next(
        (
            block['block_id']
            for block in view.get('blocks') or []
            if block.get('type') == 'input'
        ),
        '',
    )


def _number_text(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


async def _update(
    client: typing.Any,
    record: FormRecord,
    blocks: list[dict[str, typing.Any]],
    text: str,
) -> None:
    """Replace the blocks of the form message. Log a failure: the
    update is cosmetic."""
    if not record.message_ts:
        return
    try:
        await client.chat_update(
            channel=record.channel_id,
            ts=record.message_ts,
            blocks=blocks,
            text=text,
        )
    except (errors.SlackApiError, aiohttp.ClientError, TimeoutError) as error:
        LOGGER.warning('Could not update form %s: %r', record.form_id, error)


async def _ephemeral(
    client: typing.Any, record: FormRecord, user_id: str, text: str
) -> None:
    await _tell(client, record.channel_id, record.thread_ts, user_id, text)


async def _tell(
    client: typing.Any,
    channel_id: str,
    thread_ts: str | None,
    user_id: str,
    text: str,
) -> None:
    """Send ``user_id`` an ephemeral message. Log a Slack error."""
    try:
        await client.chat_postEphemeral(
            channel=channel_id, user=user_id, thread_ts=thread_ts, text=text
        )
    except errors.SlackApiError as error:
        LOGGER.warning(
            'Could not tell %s about a form: %s',
            user_id,
            error.response.get('error'),
        )


async def _expire_message(
    client: typing.Any, body: dict[str, typing.Any], user_id: str
) -> None:
    """Tell the user that the form expired and mark the message."""
    channel_id = (body.get('channel') or {}).get('id')
    message = body.get('message') or {}
    if not channel_id:
        return
    expired = 'This form has expired.'
    await _tell(client, channel_id, message.get('thread_ts'), user_id, expired)
    if not message.get('ts'):
        return
    kept = [
        block
        for block in message.get('blocks') or []
        if block.get('type') not in ('actions', 'input')
    ]
    try:
        await client.chat_update(
            channel=channel_id,
            ts=message['ts'],
            blocks=[*kept, _context(expired)],
            text=expired,
        )
    except errors.SlackApiError as error:
        LOGGER.warning(
            'Could not mark an expired form: %s', error.response.get('error')
        )
