from django.contrib import admin
from django.urls import path, include

from . import views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("docs/", views.swagger_ui, name="swagger_ui"),
    path("docs/openapi.yaml", views.openapi_spec, name="openapi_spec"),
    path("avisos/", include("avisos.urls")),
    path("users/", include("users.urls")),
    path("match/", include("match.urls")),
    path("anuncios/", include("anuncios.urls")),
    path("", include("planned_grants.urls")),
    path("news/", include("newsletter.urls")),
]
