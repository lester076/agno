"""Slack senders of an agent, team or workflow get a user in the AgentOS user directory."""

from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("slack_sdk", reason="slack_sdk not installed")

from agno.agent import Agent
from agno.db.sqlite import SqliteDb
from agno.os import AgentOS, UserDirectory
from agno.os.interfaces.slack.handlers import handle_message

from .conftest import make_async_client_mock, make_ctx, make_event_body

_EXECUTOR = "agno.os.interfaces.slack.handlers.executor"


def _app(tmp_path, **directory_kwargs):
    db = SqliteDb(db_file=str(tmp_path / "os.db"))
    directory = UserDirectory(**directory_kwargs) if directory_kwargs else True
    os = AgentOS(id="slack-directory", db=db, agents=[Agent(id="agent-1")], user_directory=directory, telemetry=False)
    return os.get_app()


async def _send(ctx, **body_kwargs):
    client = make_async_client_mock()
    with patch(f"{_EXECUTOR}.run_event", new_callable=AsyncMock) as run_event:
        await handle_message(make_event_body(**body_kwargs), client, ctx)
    return run_event, client


@pytest.mark.asyncio
async def test_sender_is_created_once_and_reused(tmp_path):
    app = _app(tmp_path)
    ctx = make_ctx().bind(app)
    run_event, _ = await _send(ctx)
    assert run_event.called
    user = await app.state.user_store.aget("U123")
    assert (user["name"], user["disabled"]) == ("Test User", False)
    await _send(ctx, ts="1700000000.000200", event_id="Ev2")
    assert await app.state.user_store.acount() == 1


@pytest.mark.asyncio
async def test_resolved_email_is_the_directory_user(tmp_path):
    app = _app(tmp_path)
    await _send(make_ctx(resolve_user_identity=True).bind(app))
    user = await app.state.user_store.aget("test@example.com")
    assert user["email"] == "test@example.com"


@pytest.mark.asyncio
async def test_disabled_sender_is_refused(tmp_path):
    app = _app(tmp_path)
    await app.state.user_store.aset_disabled("U123", True)
    run_event, client = await _send(make_ctx().bind(app))
    assert not run_event.called
    assert client.chat_postEphemeral.call_args.kwargs["text"] == "Not run: User is disabled"


@pytest.mark.asyncio
async def test_without_auto_provisioning_no_user_is_created_and_disabled_still_applies(tmp_path):
    app = _app(tmp_path, auto_provision=False)
    ctx = make_ctx().bind(app)
    run_event, _ = await _send(ctx)
    assert run_event.called
    assert await app.state.user_store.aget("U123") is None
    await app.state.user_store.aset_disabled("U123", True)
    run_event, _ = await _send(ctx, ts="1700000000.000200", event_id="Ev2")
    assert not run_event.called


@pytest.mark.asyncio
async def test_another_apps_post_creates_no_user(tmp_path):
    app = _app(tmp_path)
    ctx = make_ctx(respond_to_other_apps=True).bind(app)
    ctx.bot_id, ctx.bot_user_id = "B_SELF", "U_SELF"
    run_event, _ = await _send(ctx, user="", bot_id="B_OTHER")
    assert run_event.called
    assert await app.state.user_store.acount() == 0


@pytest.mark.asyncio
async def test_super_agent_sender_is_not_provisioned(tmp_path):
    app = _app(tmp_path)
    ctx = make_ctx().bind(app)
    ctx.company = AsyncMock()
    run_event, _ = await _send(ctx)
    assert run_event.called
    assert await app.state.user_store.acount() == 0


@pytest.mark.asyncio
async def test_without_a_directory_nothing_changes():
    run_event, client = await _send(make_ctx())
    assert run_event.called and run_event.call_args.args[0].authority is None
    assert not client.chat_postEphemeral.called


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_closed", [True, False])
async def test_unreachable_directory_follows_the_configured_policy(tmp_path, fail_closed):
    app = _app(tmp_path, fail_closed=fail_closed)
    with patch.object(app.state.user_store, "aget", AsyncMock(side_effect=RuntimeError("db down"))):
        run_event, client = await _send(make_ctx().bind(app))
    assert run_event.called is not fail_closed
    if fail_closed:
        assert client.chat_postEphemeral.call_args.kwargs["text"] == "Not run: User directory unavailable"


@pytest.mark.asyncio
async def test_default_role_sender_is_provisioned_and_still_checked(tmp_path):
    app = _app(tmp_path)
    ctx = make_ctx(default_role="slack-member").bind(app)
    authority = object()
    authorize = AsyncMock(return_value=authority)
    with patch("agno.os.interfaces.slack.security.resolve_principal_authorization", authorize):
        run_event, _ = await _send(ctx)
    assert await app.state.user_store.aget("U123") is not None
    authorize.assert_awaited_once_with(app, "U123", "agents", "agent-1", ["slack-member"])
    assert run_event.call_args.args[0].authority is authority
