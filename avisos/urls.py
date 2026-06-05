from django.urls import path
from . import views

urlpatterns = [
    path("", views.avisos_todos, name="avisos_todos"),
    path("compete/", views.avisos_compete, name="avisos_compete"),
    path("portugal/", views.avisos_portugal, name="avisos_portugal"),
    path("prr/", views.avisos_prr, name="avisos_prr"),
]
