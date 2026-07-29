from django.urls import path

from . import views

urlpatterns = [
    path("planned-grants/", views.list_planned_grants, name="planned_grants_list"),
    path("planned-grants/sync/", views.sync_planned_grants, name="planned_grants_sync"),
]
