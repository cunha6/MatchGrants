from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path("admin/", admin.site.urls),
    path("avisos/", include("avisos.urls")),
    path("users/", include("users.urls")),
]
