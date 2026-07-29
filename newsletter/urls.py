from django.urls import path

from . import views

urlpatterns = [
    path("weekly/", views.weekly_news, name="weekly_news"),
]
