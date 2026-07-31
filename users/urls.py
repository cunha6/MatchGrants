from django.urls import path
from . import views

urlpatterns = [
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("password-reset/", views.password_reset_request_view, name="password_reset_request"),
    path("password-reset/confirm/", views.password_reset_confirm_view, name="password_reset_confirm"),
    path("", views.users_all, name="users_all"),
    path("me/", views.users_me, name="users_me"),
    path("create/", views.users_create, name="users_create"),
    path("<int:user_id>/", views.user_by_id, name="users_detail"),  # GET detalhe, DELETE apaga
    path("<int:user_id>/update/", views.users_update, name="users_update"),
    path("<int:user_id>/password/", views.users_change_password, name="users_password"),
    path("<int:user_id>/activate/", views.users_activate, name="users_activate"),
]
