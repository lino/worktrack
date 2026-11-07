"""Stateless OAuth 2.0 PKCE Confidential Token-Exchange Broker DRF endpoints (`PROB-WT-001`)."""

from __future__ import annotations

from typing import Any, Final

from django.http import HttpRequest, HttpResponseBase
from drf_spectacular.utils import extend_schema
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from worktrack.secrets import SecretValue
from worktrack.web.serializers_outbox import (
    OAuthExchangeRequestSerializer,
    OAuthRefreshRequestSerializer,
    OAuthTokenResponseSerializer,
    token_bundle_to_dict,
)
from worktrack.web.services import get_active_web_services

# region Constants
CACHE_CONTROL_NO_STORE: Final = "no-store"
PRAGMA_NO_CACHE: Final = "no-cache"
# endregion


# region Views
class _NoStoreOAuthAPIView(APIView):  # type: ignore[misc]
    """Base `APIView` enforcing `Cache-Control: no-store` on all OAuth broker responses."""

    def finalize_response(
        self,
        request: HttpRequest,
        response: Response,
        *args: Any,
        **kwargs: Any,
    ) -> HttpResponseBase:
        """Attach `Cache-Control: no-store` and `Pragma: no-cache` to every response."""
        finalized = super().finalize_response(request, response, *args, **kwargs)
        finalized["Cache-Control"] = CACHE_CONTROL_NO_STORE
        finalized["Pragma"] = PRAGMA_NO_CACHE
        return finalized


class OAuthExchangeView(_NoStoreOAuthAPIView):
    """Stateless PKCE `authorization_code` exchange (`/api/v1/oauth/{provider}/exchange/`)."""

    @extend_schema(
        tags=["OAuthBroker"],
        request=OAuthExchangeRequestSerializer,
        responses={200: OAuthTokenResponseSerializer},
    )
    def post(self, request: Request, provider: str) -> Response:
        """Exchange `code` + `code_verifier` via `StatelessOAuthBroker` with zero persistence."""
        serializer = OAuthExchangeRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        broker = get_active_web_services().oauth_broker
        bundle = broker.exchange_authorization_code(
            provider=provider.strip().lower(),
            code=str(data["code"]),
            code_verifier=SecretValue(str(data["code_verifier"])),
            redirect_uri=str(data["redirect_uri"]),
        )
        return Response(token_bundle_to_dict(bundle))


class OAuthRefreshView(_NoStoreOAuthAPIView):
    """Stateless confidential `refresh_token` grant (`/api/v1/oauth/{provider}/refresh/`)."""

    @extend_schema(
        tags=["OAuthBroker"],
        request=OAuthRefreshRequestSerializer,
        responses={200: OAuthTokenResponseSerializer},
    )
    def post(self, request: Request, provider: str) -> Response:
        """Exchange `refresh_token` via `StatelessOAuthBroker` with zero persistence."""
        serializer = OAuthRefreshRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        broker = get_active_web_services().oauth_broker
        bundle = broker.refresh_access_token(
            provider=provider.strip().lower(),
            refresh_token=SecretValue(str(data["refresh_token"])),
        )
        return Response(token_bundle_to_dict(bundle))


# endregion
