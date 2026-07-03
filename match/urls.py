from django.urls import path
from . import views

urlpatterns = [
    path("evaluate-nif/", views.evaluate_nif, name="evaluate_nif"),
    path("promote/<str:nif>/", views.promote_viewer, name="promote_viewer"),
]
