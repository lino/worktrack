"""Subparser builders and handlers for `wt jira` connector sync and `wt serve` WSGI server."""

from __future__ import annotations

import argparse
import secrets
import threading
import urllib.parse
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Final
from wsgiref.simple_server import WSGIRequestHandler, make_server

from worktrack.cli.context import (
    EXIT_SUCCESS,
    OUTCOME_OK,
    SUBCOMMAND_JIRA,
    SUBCOMMAND_SERVE,
    CliContext,
)
from worktrack.cli.render import write_json, write_line
from worktrack.config import WorktrackSettings

if TYPE_CHECKING:
    from worktrack.web.services import WebServiceContainer
from worktrack.connectors import (
    JIRA_PROVIDER_SLUG,
    JiraCloudConfig,
    JiraConfigurationError,
    JiraHttpTransport,
    JiraIssueSource,
    JiraOAuthClient,
)
from worktrack.secrets import (
    DEFAULT_TOKEN_ACCOUNT,
    OAuthTokenBundle,
    SecretValue,
    UserTokenStore,
    generate_pkce_pair,
)

# region Constants
JIRA_ACTION_STATUS: Final = "status"
JIRA_ACTION_AUTH_URL: Final = "auth-url"
JIRA_ACTION_LOGIN: Final = "login"
JIRA_ACTION_LOGOUT: Final = "logout"
JIRA_ACTION_SYNC: Final = "sync"

ENV_JIRA_CLIENT_ID: Final = "WORKTRACK_JIRA_CLIENT_ID"
ENV_JIRA_REDIRECT_URI: Final = "WORKTRACK_JIRA_REDIRECT_URI"
ENV_JIRA_ACCESS_TOKEN: Final = "WORKTRACK_JIRA_ACCESS_TOKEN"
ENV_JIRA_REFRESH_TOKEN: Final = "WORKTRACK_JIRA_REFRESH_TOKEN"

DEFAULT_ATLASSIAN_AUTHORIZE_URL: Final = "https://auth.atlassian.com/authorize"
DEFAULT_ATLASSIAN_AUDIENCE: Final = "api.atlassian.com"
DEFAULT_JIRA_OAUTH_SCOPES: Final = "read:jira-work write:jira-work offline_access"
OAUTH_STATE_BYTES: Final = 16

DEFAULT_SERVE_HOST: Final = "127.0.0.1"
DEFAULT_SERVE_PORT: Final = 8000
DEFAULT_SERVE_POLL_INTERVAL_SECONDS: Final = 0.05

_JIRA_TRANSPORT_OVERRIDE: ContextVar[JiraHttpTransport | None] = ContextVar(
    "worktrack_cli_jira_transport_override",
    default=None,
)
# endregion


# region Subcommands
@contextmanager
def use_jira_transport(transport: JiraHttpTransport) -> Iterator[None]:
    """Bind an injectable `JiraHttpTransport` for `wt jira sync` within a context."""
    token = _JIRA_TRANSPORT_OVERRIDE.set(transport)
    try:
        yield
    finally:
        _JIRA_TRANSPORT_OVERRIDE.reset(token)


def register_web_subparsers(
    subparsers: argparse._SubParsersAction[Any],
    json_parent: argparse.ArgumentParser,
) -> None:
    """Register `wt jira {status,auth-url,login,logout,sync}` and `wt serve` subcommands."""
    jira_p = subparsers.add_parser(
        SUBCOMMAND_JIRA,
        parents=[json_parent],
        help="Log in, inspect local OAuth status, build PKCE auth URL, or sync Jira issues.",
    )
    jira_p.set_defaults(handler=handle_jira, canonical_subcommand=SUBCOMMAND_JIRA)
    nested = jira_p.add_subparsers(dest="jira_action", required=True)
    _register_jira_auth_parsers(nested, json_parent)
    _register_jira_login_parsers(nested, json_parent)
    _register_jira_sync_parser(nested, json_parent)
    _register_serve_parser(subparsers, json_parent)


def _register_jira_auth_parsers(
    nested: argparse._SubParsersAction[Any],
    json_parent: argparse.ArgumentParser,
) -> None:
    """Configure `wt jira status` and `wt jira auth-url` subparsers."""
    status_p = nested.add_parser(JIRA_ACTION_STATUS, parents=[json_parent])
    status_p.add_argument("--account", default=DEFAULT_TOKEN_ACCOUNT, help="Local token account.")

    auth_p = nested.add_parser(JIRA_ACTION_AUTH_URL, parents=[json_parent])
    auth_p.add_argument("--client-id", default=None, help="Public OAuth client_id override.")
    auth_p.add_argument("--redirect-uri", default=None, help="OAuth redirect_uri override.")
    auth_p.add_argument("--state", default=None, help="Optional explicit CSRF state token.")
    auth_p.add_argument("--scope", default=DEFAULT_JIRA_OAUTH_SCOPES, help="OAuth scopes.")
    auth_p.add_argument("--authorize-url", default=DEFAULT_ATLASSIAN_AUTHORIZE_URL)
    auth_p.add_argument("--audience", default=DEFAULT_ATLASSIAN_AUDIENCE)


def _register_jira_login_parsers(
    nested: argparse._SubParsersAction[Any],
    json_parent: argparse.ArgumentParser,
) -> None:
    """Configure `wt jira login` and `wt jira logout` subparsers."""
    login_p = nested.add_parser(JIRA_ACTION_LOGIN, parents=[json_parent])
    login_p.add_argument("--access-token", default="", help="Direct Jira OAuth access token.")
    login_p.add_argument("--refresh-token", default="", help="Optional OAuth refresh token.")
    login_p.add_argument("--code", default="", help="OAuth 2.0 PKCE authorization code.")
    login_p.add_argument("--code-verifier", default="", help="PKCE S256 code_verifier.")
    login_p.add_argument("--redirect-uri", default="", help="OAuth redirect_uri override.")
    login_p.add_argument("--scope", default=DEFAULT_JIRA_OAUTH_SCOPES, help="OAuth scopes.")
    login_p.add_argument("--expires-in", type=int, default=None, help="Token TTL in seconds.")
    login_p.add_argument("--account", default=DEFAULT_TOKEN_ACCOUNT, help="Local token account.")

    logout_p = nested.add_parser(JIRA_ACTION_LOGOUT, parents=[json_parent])
    logout_p.add_argument("--account", default=DEFAULT_TOKEN_ACCOUNT, help="Local token account.")


def _register_jira_sync_parser(
    nested: argparse._SubParsersAction[Any],
    json_parent: argparse.ArgumentParser,
) -> None:
    """Configure `wt jira sync` subparser."""
    sync_p = nested.add_parser(JIRA_ACTION_SYNC, parents=[json_parent])
    sync_p.add_argument("--jql", default=None, help="Optional JQL query override.")
    sync_p.add_argument("--cloud-id", default=None, help="Optional Jira Cloud site ID override.")
    sync_p.add_argument("--api-base-url", default=None, help="Optional Jira API base URL override.")
    sync_p.add_argument("--account", default=DEFAULT_TOKEN_ACCOUNT, help="Local token account.")


def _register_serve_parser(
    subparsers: argparse._SubParsersAction[Any],
    json_parent: argparse.ArgumentParser,
) -> None:
    """Configure `wt serve` loopback/container WSGI server subparser."""
    serve_p = subparsers.add_parser(SUBCOMMAND_SERVE, parents=[json_parent])
    serve_p.add_argument("--host", default=DEFAULT_SERVE_HOST, help="Bind host address.")
    serve_p.add_argument("--port", type=int, default=DEFAULT_SERVE_PORT, help="Bind TCP port.")
    serve_p.add_argument("--check", action="store_true", help="Verify Django readiness only.")
    serve_p.set_defaults(handler=handle_serve, canonical_subcommand=SUBCOMMAND_SERVE)


# endregion


# region CLI
def handle_jira(ctx: CliContext, args: argparse.Namespace) -> int:
    """Dispatch `wt jira {status,auth-url,login,logout,sync}`."""
    dispatch = {
        JIRA_ACTION_STATUS: _handle_jira_status,
        JIRA_ACTION_AUTH_URL: _handle_jira_auth_url,
        JIRA_ACTION_LOGIN: _handle_jira_login,
        JIRA_ACTION_LOGOUT: _handle_jira_logout,
    }
    handler = dispatch.get(str(args.jira_action), _handle_jira_sync)
    return handler(ctx, args)


def _handle_jira_status(ctx: CliContext, args: argparse.Namespace) -> int:
    """Report workstation-local Jira OAuth token metadata without exposing raw tokens."""
    account = str(args.account)
    bundle = UserTokenStore(environ=ctx.environ).load(JIRA_PROVIDER_SLUG, account=account)
    payload: dict[str, object] = {
        "provider": JIRA_PROVIDER_SLUG,
        "account": account,
        "authenticated": bundle is not None,
        "has_refresh_token": bool(bundle is not None and bundle.refresh_token is not None),
        "token_type": bundle.token_type if bundle is not None else None,
        "scope": bundle.scope if bundle is not None else None,
        "expires_in": bundle.expires_in if bundle is not None else None,
    }
    if ctx.as_json:
        write_json(ctx.stdout, payload)
    else:
        state_label = "authenticated" if bundle is not None else "unauthenticated"
        write_line(ctx.stdout, f"jira ({account}): {state_label}")
    return EXIT_SUCCESS


def _handle_jira_login(ctx: CliContext, args: argparse.Namespace) -> int:
    """Persist a direct Jira token or PKCE-exchanged bundle in local `UserTokenStore`."""
    account = str(args.account)
    bundle = _resolve_login_bundle(ctx, args)
    UserTokenStore(environ=ctx.environ).store(JIRA_PROVIDER_SLUG, bundle, account=account)
    if ctx.as_json:
        write_json(
            ctx.stdout,
            {
                "provider": JIRA_PROVIDER_SLUG,
                "account": account,
                "authenticated": True,
                "has_refresh_token": bundle.refresh_token is not None,
                "scope": bundle.scope,
            },
        )
    else:
        write_line(ctx.stdout, f"jira ({account}): authenticated")
    return EXIT_SUCCESS


def _resolve_login_bundle(ctx: CliContext, args: argparse.Namespace) -> OAuthTokenBundle:
    """Resolve `OAuthTokenBundle` from `--code`/`--code-verifier` or `--access-token`."""
    code = str(args.code).strip()
    verifier = str(args.code_verifier).strip()
    if code:
        from worktrack.web.services import WebServiceContainer

        redirect_uri = str(args.redirect_uri).strip() or ctx.environ.get(ENV_JIRA_REDIRECT_URI, "")
        broker = WebServiceContainer.from_settings(ctx.settings, environ=ctx.environ).oauth_broker
        return broker.exchange_authorization_code(JIRA_PROVIDER_SLUG, code, verifier, redirect_uri)
    raw_access = str(args.access_token).strip() or ctx.environ.get(ENV_JIRA_ACCESS_TOKEN, "")
    if not raw_access.strip():
        raise JiraConfigurationError(
            "Provide --access-token (or --code and --code-verifier) to log in to Jira."
        )
    raw_refresh = str(args.refresh_token).strip() or ctx.environ.get(ENV_JIRA_REFRESH_TOKEN, "")
    return OAuthTokenBundle(
        access_token=SecretValue(raw_access.strip()),
        refresh_token=SecretValue(raw_refresh.strip()) if raw_refresh.strip() else None,
        expires_in=args.expires_in,
        scope=str(args.scope),
    )


def _handle_jira_logout(ctx: CliContext, args: argparse.Namespace) -> int:
    """Delete stored Jira OAuth credentials from local `UserTokenStore`."""
    account = str(args.account)
    removed = UserTokenStore(environ=ctx.environ).delete(JIRA_PROVIDER_SLUG, account=account)
    if ctx.as_json:
        write_json(
            ctx.stdout,
            {"provider": JIRA_PROVIDER_SLUG, "account": account, "removed": removed},
        )
    else:
        write_line(ctx.stdout, f"jira ({account}): logged out")
    return EXIT_SUCCESS


def _handle_jira_auth_url(ctx: CliContext, args: argparse.Namespace) -> int:
    """Build an Atlassian 3LO PKCE S256 authorization URL."""
    client_id = (args.client_id or ctx.environ.get(ENV_JIRA_CLIENT_ID, "")).strip()
    if not client_id:
        raise JiraConfigurationError(
            f"Provide --client-id or set {ENV_JIRA_CLIENT_ID} to build a Jira OAuth URL."
        )
    redirect_uri = (args.redirect_uri or ctx.environ.get(ENV_JIRA_REDIRECT_URI, "")).strip()
    if not redirect_uri:
        raise JiraConfigurationError(
            f"Provide --redirect-uri or set {ENV_JIRA_REDIRECT_URI} to build a Jira OAuth URL."
        )

    pkce = generate_pkce_pair()
    state = str(args.state).strip() if args.state else secrets.token_urlsafe(OAUTH_STATE_BYTES)
    query = urllib.parse.urlencode(
        {
            "audience": str(args.audience),
            "client_id": client_id,
            "scope": str(args.scope),
            "redirect_uri": redirect_uri,
            "state": state,
            "response_type": "code",
            "prompt": "consent",
            "code_challenge": pkce.code_challenge,
            "code_challenge_method": pkce.code_challenge_method,
        }
    )
    auth_url = f"{str(args.authorize_url).rstrip('?')}?{query}"
    if ctx.as_json:
        write_json(
            ctx.stdout,
            {
                "authorization_url": auth_url,
                "state": state,
                "code_verifier": pkce.code_verifier,
                "code_challenge": pkce.code_challenge,
                "code_challenge_method": pkce.code_challenge_method,
            },
        )
    else:
        write_line(ctx.stdout, auth_url)
    return EXIT_SUCCESS


def _handle_jira_sync(ctx: CliContext, args: argparse.Namespace) -> int:
    """Sync assigned Jira Cloud issues into the local SQLite `Ledger`."""
    token_store = UserTokenStore(environ=ctx.environ)
    config = JiraCloudConfig(
        cloud_id=args.cloud_id,
        api_base_url=args.api_base_url,
        account=str(args.account),
    )
    client = JiraOAuthClient(
        config=config,
        token_store=token_store,
        transport=_JIRA_TRANSPORT_OVERRIDE.get(),
        environ=ctx.environ,
    )
    source = JiraIssueSource(client=client, telemetry=ctx.telemetry, environ=ctx.environ)
    with ctx.open_ledger() as ledger:
        result = source.sync(ledger, jql=args.jql)

    if ctx.as_json:
        write_json(
            ctx.stdout,
            {
                "jql": result.jql,
                "issues_fetched": result.issues_fetched,
                "activities_created": result.activities_created,
                "activities_updated": result.activities_updated,
                "entities_created": result.entities_created,
                "attributions_upserted": result.attributions_upserted,
            },
        )
    else:
        write_line(
            ctx.stdout,
            f"jira sync: {result.issues_fetched} issue(s) "
            f"(+{result.activities_created} created, {result.activities_updated} updated)",
        )
    return EXIT_SUCCESS


def handle_serve(ctx: CliContext, args: argparse.Namespace) -> int:
    """Configure Django lazily and either check readiness or serve WSGI HTTP traffic."""
    from worktrack.web.services import WebServiceContainer
    from worktrack.web.settings import ensure_django_configured

    host = str(args.host)
    port = int(args.port)
    ensure_django_configured(environ=ctx.environ, worktrack_settings=ctx.settings)
    if args.check:
        if ctx.as_json:
            write_json(
                ctx.stdout,
                {"ready": True, "host": host, "port": port, "mode": ctx.settings.mode},
            )
        else:
            write_line(ctx.stdout, f"wt serve: ready ({ctx.settings.mode}) on {host}:{port}")
        return EXIT_SUCCESS

    services = WebServiceContainer.from_settings(ctx.settings, environ=ctx.environ)
    ctx.record_outcome(SUBCOMMAND_SERVE, OUTCOME_OK)
    if not ctx.as_json:
        write_line(ctx.stdout, f"Serving Worktrack API on http://{host}:{port}")
    try:
        return serve_wsgi_until_stopped(host=host, port=port, services=services)
    except KeyboardInterrupt:
        return EXIT_SUCCESS


# endregion


# region HTTP Server
class _QuietWsgiRequestHandler(WSGIRequestHandler):
    """`WSGIRequestHandler` suppressing default stderr request logging."""

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress raw stderr access log lines in favour of `FritzTraceAndMetricsMiddleware`."""
        _ = (format, args)


def _resolve_serve_services(
    *,
    services: WebServiceContainer | None,
    settings: WorktrackSettings | None,
    environ: Mapping[str, str] | None,
) -> WebServiceContainer | None:
    """Resolve an isolated `WebServiceContainer` when `services`/`settings`/`environ` is passed."""
    if services is not None:
        return services
    if settings is None and environ is None:
        return None
    from worktrack.web.services import WebServiceContainer

    if settings is not None:
        return WebServiceContainer.from_settings(settings, environ=environ)
    return WebServiceContainer.from_env(environ=environ)


def serve_wsgi_until_stopped(
    *,
    host: str = DEFAULT_SERVE_HOST,
    port: int = DEFAULT_SERVE_PORT,
    stop_event: threading.Event | None = None,
    on_bound: Callable[[int], object] | None = None,
    poll_interval: float = DEFAULT_SERVE_POLL_INTERVAL_SECONDS,
    services: WebServiceContainer | None = None,
    settings: WorktrackSettings | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Bind a stdlib WSGI HTTP server for `worktrack.web` and serve until stopped."""
    from worktrack.web.services import use_web_services
    from worktrack.web.settings import ensure_django_configured
    from worktrack.web.wsgi import get_wsgi_application

    ensure_django_configured(environ=environ, worktrack_settings=settings)
    active_services = _resolve_serve_services(
        services=services,
        settings=settings,
        environ=environ,
    )
    raw_wsgi_app: Callable[[dict[str, Any], Callable[..., Any]], Iterable[bytes]] = (
        get_wsgi_application()
    )

    def _scoped_wsgi_app(
        wsgi_environ: dict[str, Any],
        start_response: Callable[..., Any],
    ) -> Iterable[bytes]:
        """Execute WSGI request scoped to `active_services` when provided."""
        if active_services is None:
            return raw_wsgi_app(wsgi_environ, start_response)
        with use_web_services(active_services):
            return raw_wsgi_app(wsgi_environ, start_response)

    with make_server(host, port, _scoped_wsgi_app, handler_class=_QuietWsgiRequestHandler) as httpd:
        if on_bound is not None:
            on_bound(int(httpd.server_port))
        if stop_event is None:
            httpd.serve_forever(poll_interval=poll_interval)
            return EXIT_SUCCESS
        httpd.timeout = poll_interval
        while not stop_event.is_set():
            httpd.handle_request()
    return EXIT_SUCCESS


# endregion
