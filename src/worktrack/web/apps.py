"""Django `AppConfig` declaration for `worktrack.web`."""

from __future__ import annotations

import sys

from django.apps import AppConfig, apps


def neutralize_lazy_admin_site() -> None:
    """Bind a plain `AdminSite` to `django.contrib.admin.sites.site` when `admin` is uninstalled."""
    sites_mod = sys.modules.get("django.contrib.admin.sites")
    if sites_mod is None or apps.is_installed("admin"):
        return
    lazy_site = getattr(sites_mod, "site", None)
    admin_site_cls = getattr(sites_mod, "AdminSite", None)
    if lazy_site is not None and callable(admin_site_cls):
        object.__setattr__(lazy_site, "_wrapped", admin_site_cls())


class WorktrackWebConfig(AppConfig):  # type: ignore[misc]
    """Django application configuration for the Worktrack dual-mode web API."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "worktrack.web"
    label = "worktrack_web"
    verbose_name = "Worktrack Web API & Stateless OAuth Broker"

    def ready(self) -> None:
        """Disable unused `ObtainAuthTokenView` schema extension and neutralize lazy admin proxy."""
        neutralize_lazy_admin_site()
        if not apps.is_installed("rest_framework.authtoken"):
            from drf_spectacular.extensions import OpenApiViewExtension

            registry = getattr(OpenApiViewExtension, "_registry", None)
            if isinstance(registry, list):
                registry[:] = [
                    ext
                    for ext in registry
                    if getattr(ext, "target_class", "")
                    != "rest_framework.authtoken.views.ObtainAuthToken"
                ]
