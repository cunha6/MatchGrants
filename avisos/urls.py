from django.urls import path
from . import views

urlpatterns = [
    path("", views.grants_all, name="grants_all"),
    path("compete/", views.grants_compete, name="grants_compete"),
    path("portugal/", views.grants_portugal, name="grants_portugal"),
    path("prr/", views.grants_prr, name="grants_prr"),
]
