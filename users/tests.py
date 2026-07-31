import json
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.core.cache import cache
from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from . import service
from .models import UserProfile


class UserCreateSecurityTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.base_url = "/users/create/"

        # 1. Admin
        self.admin_user = User.objects.create_user(username="admin_creator", password="123", email="admin@mail.com")
        UserProfile.objects.filter(user=self.admin_user).update(role=UserProfile.ADMIN)

        # 2. Commercial
        self.commercial_user = User.objects.create_user(username="commercial_creator", password="123", email="com@mail.com")
        UserProfile.objects.filter(user=self.commercial_user).update(role=UserProfile.COMMERCIAL_GRANTS)

        # 3. Client
        self.client_user = User.objects.create_user(username="normal_client", password="123", email="client@mail.com")
        UserProfile.objects.filter(user=self.client_user).update(role=UserProfile.CLIENT)

        # Perfect base payload to create a CLIENT
        self.valid_client_payload = {
            "username": "new_client",
            "password": "strongPassword123",
            "email": "new@client.com",
            "role": UserProfile.CLIENT,
            "entity_type": UserProfile.EMPRESA,
            "entity_size": UserProfile.MEDIA,
            "nif": "999999999",
            "main_cae": "62010",
            "address": "Main Street",
            "region": "Norte",
            "nuts_ii": True,
            "nuts_iii": False,
        }

    # --- TEST 1: Client Rule (Blocked) ---
    def test_client_cannot_create_users(self):
        """Ensures a Client cannot create accounts (HTTP 403)."""
        self.client.login(username="normal_client", password="123")

        response = self.client.post(
            self.base_url,
            data=json.dumps(self.valid_client_payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403)
        self.assertIn("You do not have permission to create users.", response.json()["error"])

    # --- TEST 2: Commercial Rule (Creates Client) ---
    def test_commercial_can_create_client(self):
        """Ensures a Commercial user can successfully create a new Client."""
        self.client.login(username="commercial_creator", password="123")

        response = self.client.post(
            self.base_url,
            data=json.dumps(self.valid_client_payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 201)

    # --- TEST 3: Commercial Rule (Attempts to create Admin) ---
    def test_commercial_cannot_create_admin(self):
        """Ensures a Commercial user gets an error if attempting to create an Admin."""
        self.client.login(username="commercial_creator", password="123")

        attacker_payload = self.valid_client_payload.copy()
        attacker_payload["role"] = UserProfile.ADMIN

        response = self.client.post(
            self.base_url,
            data=json.dumps(attacker_payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403)
        self.assertIn("You can only create 'client' users.", response.json()["error"])

    # --- TEST 4: Admin Rule (Creates anyone) ---
    def test_admin_can_create_any_role(self):
        """Ensures the Admin can create a Commercial user without restrictions."""
        self.client.login(username="admin_creator", password="123")

        payload_admin = {
            "username": "new_commercial",
            "password": "strongPassword123",  # Fixed: Needs > 8 characters
            "email": "commercial@mail.com",
            "role": UserProfile.COMMERCIAL_GRANTS,
        }

        response = self.client.post(
            self.base_url,
            data=json.dumps(payload_admin),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 201)

        new_user = User.objects.get(username="new_commercial")
        self.assertEqual(new_user.profile.role, UserProfile.COMMERCIAL_GRANTS)

    # --- TEST 5: Public Rule (No Login -> Forces Client) ---
    def test_public_user_is_forced_to_client_role(self):
        """Ensures public registration ignores attempts to inject 'role':'admin'."""
        attacker_payload = self.valid_client_payload.copy()
        attacker_payload["username"] = "hacker_visitor"
        attacker_payload["role"] = UserProfile.ADMIN

        response = self.client.post(
            self.base_url,
            data=json.dumps(attacker_payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 201)

        new_user = User.objects.get(username="hacker_visitor")
        self.assertEqual(new_user.profile.role, UserProfile.CLIENT)

    # --- TEST 6: Fails due to missing required data (Error 400) ---
    def test_create_fails_if_missing_credentials(self):
        """Ensures creating without username, email, or password returns a 400 error."""
        incomplete_payload = {
            "username": "no_password",
            "email": "test@mail.com",
            # Intentionally missing the password
        }
        response = self.client.post(
            self.base_url,
            data=json.dumps(incomplete_payload),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("password", response.json()["error"].lower())

    # --- TEST 7: Fails due to duplicated Email/Username (Error 400) ---
    def test_create_fails_if_username_or_email_exists(self):
        """Ensures accounts cannot be created with already existing credentials."""
        duplicate_payload = self.valid_client_payload.copy()
        duplicate_payload["email"] = "admin@mail.com"

        response = self.client.post(
            self.base_url,
            data=json.dumps(duplicate_payload),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("already registered", response.json()["error"])

    # --- TEST 8: Fails due to missing Entity data (Error 400) ---
    def test_create_client_fails_if_missing_entity_data(self):
        """Ensures creating a Client requires NIF, address, etc."""
        missing_nif_payload = self.valid_client_payload.copy()
        del missing_nif_payload["nif"]

        response = self.client.post(
            self.base_url,
            data=json.dumps(missing_nif_payload),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("required", response.json()["error"].lower())


class UserUpdateSecurityTests(TestCase):
    def setUp(self):
        self.client = Client()

        # 1. Admin
        self.admin_user = User.objects.create_user(username="admin", password="password123", email="admin@admin.com")
        UserProfile.objects.filter(user=self.admin_user).update(role=UserProfile.ADMIN)

        # 2. Target
        self.target_user = User.objects.create_user(username="target", password="password123", email="target@test.com")
        UserProfile.objects.filter(user=self.target_user).update(
            role=UserProfile.CLIENT,
            nif="111111111",
            entity_type=UserProfile.EMPRESA,
        )

        # 3. Attacker
        self.attacker_user = User.objects.create_user(username="hacker", password="password123", email="hacker@test.com")
        UserProfile.objects.filter(user=self.attacker_user).update(
            role=UserProfile.CLIENT,
            nif="222222222",
        )

        self.base_url = "/users/"

    def _update_url(self, uid):
        # Fixed: Re-added the /update/ endpoint to avoid 405 Method Not Allowed on PUT
        return f"{self.base_url}{uid}/update/"

    # --- TEST 1: IDOR Prevention ---
    def test_client_cannot_update_other_user(self):
        """Ensures a Client receives a 403 error if trying to edit another person's ID."""
        self.client.login(username="hacker", password="password123")

        payload = {"address": "Hacker Street"}
        response = self.client.put(
            self._update_url(self.target_user.id),
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403)
        self.assertIn("You do not have permission to edit other users.", response.json()["error"])

    # --- TEST 2: Success on Own Account ---
    def test_client_can_update_own_profile(self):
        """Ensures a user can edit their own profile."""
        self.client.login(username="target", password="password123")

        payload = {"address": "My New Address"}
        response = self.client.put(
            self._update_url(self.target_user.id),
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)

        self.target_user.profile.refresh_from_db()
        self.assertEqual(self.target_user.profile.address, "My New Address")

    # --- TEST 3: Role Escalation Prevention ---
    def test_client_cannot_change_own_role(self):
        """Ensures if a Client tries to send "role": "admin", the system ignores it."""
        self.client.login(username="target", password="password123")

        payload = {
            "address": "Legit Address",
            "role": UserProfile.ADMIN,
        }
        response = self.client.put(
            self._update_url(self.target_user.id),
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)

        self.target_user.profile.refresh_from_db()
        self.assertEqual(self.target_user.profile.address, "Legit Address")
        self.assertEqual(self.target_user.profile.role, UserProfile.CLIENT)

    # --- TEST 4: Admin Success ---
    def test_admin_can_update_other_user_and_change_role(self):
        """Ensures the Admin can edit others and change roles."""
        self.client.login(username="admin", password="password123")

        payload = {
            "address": "Address Edited By Admin",
            "role": UserProfile.COMMERCIAL_PUBLIC,
        }
        response = self.client.put(
            self._update_url(self.target_user.id),
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)

        self.target_user.profile.refresh_from_db()
        self.assertEqual(self.target_user.profile.address, "Address Edited By Admin")
        self.assertEqual(self.target_user.profile.role, UserProfile.COMMERCIAL_PUBLIC)

    # --- TEST 5: Unauthenticated User ---
    def test_unauthenticated_user_cannot_update(self):
        """Ensures a user without login is blocked by the decorator."""
        payload = {"address": "Hacked"}
        response = self.client.put(
            self._update_url(self.target_user.id),
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertNotEqual(response.status_code, 200)

    # --- TEST 6: User Not Found (Error 404) ---
    def test_admin_gets_404_if_user_does_not_exist(self):
        """Ensures trying to update a ghost ID returns 404."""
        self.client.login(username="admin", password="password123")

        payload = {"address": "Address"}
        response = self.client.put(
            self._update_url(9999),
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 404)
        self.assertIn("User not found", response.json()["error"])

    # --- TEST 7: Duplicated Username (Error 400) ---
    def test_update_fails_if_username_already_exists(self):
        """Ensures the service layer blocks repeated usernames."""
        self.client.login(username="target", password="password123")

        payload = {"username": "hacker"}
        response = self.client.put(
            self._update_url(self.target_user.id),
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("username already exists", response.json()["error"])

    # --- TEST 8: Blocked HTTP Method (Error 405) ---
    def test_endpoint_rejects_post_method(self):
        """Ensures @require_http_methods(['PUT']) is working."""
        self.client.login(username="target", password="password123")

        payload = {"address": "New Address"}
        response = self.client.post(
            self._update_url(self.target_user.id),
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 405)

    def test_update_fails_if_email_already_exists(self):
        """Ensures the service layer blocks repeated emails on update."""
        self.client.login(username="target", password="password123")

        payload = {"email": "hacker@test.com"}
        response = self.client.put(
            self._update_url(self.target_user.id),
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("email already exists", response.json()["error"])


class UserDeleteSecurityTests(TestCase):
    def setUp(self):
        self.client = Client()

        # 1. Admin
        self.admin_user = User.objects.create_user(username="admin_delete", password="123")
        UserProfile.objects.filter(user=self.admin_user).update(role=UserProfile.ADMIN)

        # 2. Target
        self.target_user = User.objects.create_user(username="target_delete", password="123")
        UserProfile.objects.filter(user=self.target_user).update(role=UserProfile.CLIENT)

        # 3. Commercial
        self.commercial_user = User.objects.create_user(username="commercial_delete", password="123")
        UserProfile.objects.filter(user=self.commercial_user).update(role=UserProfile.COMMERCIAL_GRANTS)

        self.base_url = "/users/"

    def _delete_url(self, uid):
        return f"{self.base_url}{uid}/"

    # --- TEST 1: Admin Success (soft-delete) ---
    def test_admin_can_delete_user(self):
        """Soft-delete: Admin deactivates the user (204); the record remains but is inactive."""
        self.client.login(username="admin_delete", password="123")

        response = self.client.delete(self._delete_url(self.target_user.id))

        self.assertEqual(response.status_code, 204)
        # Record still exists, but inactive
        self.target_user.refresh_from_db()
        self.assertTrue(User.objects.filter(id=self.target_user.id).exists())
        self.assertFalse(self.target_user.is_active)

    # --- TEST 2: Admin attempts to delete ghost ID (Error 404) ---
    def test_admin_gets_404_when_deleting_ghost_user(self):
        """Ensures deleting a nonexistent ID returns 404 Not Found."""
        self.client.login(username="admin_delete", password="123")

        response = self.client.delete(self._delete_url(99999))

        self.assertEqual(response.status_code, 404)
        self.assertIn("User not found", response.json()["error"])

    # --- TEST 3: Commercial attempts to delete user (Blocked) ---
    def test_commercial_cannot_delete_users(self):
        """Ensures non-Admin profiles are blocked by @require_role."""
        self.client.login(username="commercial_delete", password="123")

        response = self.client.delete(self._delete_url(self.target_user.id))

        self.assertEqual(response.status_code, 403)
        # Fixed: The @require_role decorator blocks before the view and sends its own message
        self.assertIn("You do not have permission to perform this action.", response.json()["error"])
        self.target_user.refresh_from_db()
        self.assertTrue(self.target_user.is_active)  # Remains active

    # --- TEST 4: Client attempts to delete themselves (Blocked) ---
    def test_client_cannot_delete_himself(self):
        """Ensures a Client cannot delete their own account."""
        self.client.login(username="target_delete", password="123")

        response = self.client.delete(self._delete_url(self.target_user.id))

        self.assertEqual(response.status_code, 403)
        # Fixed: The @require_role decorator blocks before the view and sends its own message
        self.assertIn("You do not have permission to perform this action.", response.json()["error"])
        self.target_user.refresh_from_db()
        self.assertTrue(self.target_user.is_active)  # Remains active
        self.assertTrue(User.objects.filter(id=self.target_user.id).exists())

    # --- TEST 5: Incorrect HTTP Method (Error 405) ---
    def test_endpoint_rejects_post_method(self):
        """Ensures @require_http_methods(['DELETE']) is working."""
        self.client.login(username="admin_delete", password="123")

        response = self.client.post(self._delete_url(self.target_user.id))

        self.assertEqual(response.status_code, 405)


class UserPasswordSecurityTests(TestCase):
    def setUp(self):
        self.client = Client()

        self.admin_user = User.objects.create_user(username="admin_pw", password="adminpass", email="admin@pw.com")
        UserProfile.objects.filter(user=self.admin_user).update(role=UserProfile.ADMIN)

        self.target_user = User.objects.create_user(username="target_pw", password="oldpass", email="target@pw.com")
        UserProfile.objects.filter(user=self.target_user).update(role=UserProfile.CLIENT)

        self.attacker_user = User.objects.create_user(username="hacker_pw", password="hackpass", email="hacker@pw.com")
        UserProfile.objects.filter(user=self.attacker_user).update(role=UserProfile.CLIENT)

        self.base_url = "/users/"

    def _pw_url(self, uid):
        return f"{self.base_url}{uid}/password/"

    # --- TEST 1: Other user (neither self nor admin) → 403 ---
    def test_other_user_cannot_change_password(self):
        """A user who is neither self nor admin gets 403 when changing another's password."""
        self.client.login(username="hacker_pw", password="hackpass")

        # Fixed: Added >8 characters to bypass validation
        payload = {"password": "newpassword123"}
        response = self.client.post(
            self._pw_url(self.target_user.id),
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403)
        self.assertIn("You do not have permission to change another user's password.", response.json()["error"])

    # --- TEST 2: Own password with correct current password → 200 ---
    def test_user_can_change_own_password(self):
        self.client.login(username="target_pw", password="oldpass")

        # Fixed: Added >8 characters to bypass validation
        payload = {"current_password": "oldpass", "password": "newpassword123"}
        response = self.client.post(
            self._pw_url(self.target_user.id),
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.target_user.refresh_from_db()
        self.assertTrue(self.target_user.check_password("newpassword123"))

    # --- TEST 3: Own password with incorrect current password → 400 ---
    def test_user_change_own_password_wrong_current(self):
        self.client.login(username="target_pw", password="oldpass")

        # Fixed: Added >8 characters to bypass validation
        payload = {"current_password": "wrongpassword", "password": "newpassword123"}
        response = self.client.post(
            self._pw_url(self.target_user.id),
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)

    # --- TEST 4: Admin resets another user's password (without current) → 200 ---
    def test_admin_can_reset_other_password(self):
        self.client.login(username="admin_pw", password="adminpass")

        # Fixed: Added >8 characters to bypass validation
        payload = {"password": "resetpassword123"}
        response = self.client.post(
            self._pw_url(self.target_user.id),
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.target_user.refresh_from_db()
        self.assertTrue(self.target_user.check_password("resetpassword123"))


class UserActivateAndMeTests(TestCase):
    def setUp(self):
        self.client = Client()

        self.admin_user = User.objects.create_user(username="admin_act", password="123", email="a@act.com")
        UserProfile.objects.filter(user=self.admin_user).update(role=UserProfile.ADMIN)

        self.commercial_user = User.objects.create_user(username="comm_act", password="123", email="c@act.com")
        UserProfile.objects.filter(user=self.commercial_user).update(role=UserProfile.COMMERCIAL_GRANTS)

        # User already deactivated (soft-deleted) — role=client, o que o commercial pode gerir.
        self.inactive_user = User.objects.create_user(username="inactive_usr", password="123", email="i@act.com", is_active=False)
        UserProfile.objects.filter(user=self.inactive_user).update(role=UserProfile.CLIENT)

        # Outro admin, também inativo — fora do alcance do commercial (só viewer/client).
        self.inactive_admin = User.objects.create_user(username="inactive_admin", password="123", email="ia@act.com", is_active=False)
        UserProfile.objects.filter(user=self.inactive_admin).update(role=UserProfile.ADMIN)

        self.base_url = "/users/"

    # --- /users/me/ ---
    def test_me_returns_own_profile(self):
        """GET /users/me/ returns the authenticated user's profile."""
        self.client.login(username="comm_act", password="123")
        response = self.client.get(f"{self.base_url}me/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["username"], "comm_act")
        self.assertEqual(response.json()["role"], UserProfile.COMMERCIAL_GRANTS)

    def test_me_requires_authentication(self):
        response = self.client.get(f"{self.base_url}me/")
        self.assertEqual(response.status_code, 401)

    # --- Listing inactive users (admin only) ---
    def test_admin_can_list_inactive_users(self):
        """Admin sees inactive users with ?active=false; they do not appear in the normal list."""
        self.client.login(username="admin_act", password="123")

        normal = self.client.get(f"{self.base_url}").json()
        inactive = self.client.get(f"{self.base_url}?active=false").json()

        normal_usernames = [u["username"] for u in normal["users"]]
        inactive_usernames = [u["username"] for u in inactive["users"]]
        self.assertNotIn("inactive_usr", normal_usernames)        # Hidden in normal list
        self.assertIn("inactive_usr", inactive_usernames)         # Visible with active=false

    # --- Reactivate (admin only) ---
    def test_admin_can_reactivate_user(self):
        self.client.login(username="admin_act", password="123")
        response = self.client.post(f"{self.base_url}{self.inactive_user.id}/activate/")
        self.assertEqual(response.status_code, 200)
        self.inactive_user.refresh_from_db()
        self.assertTrue(self.inactive_user.is_active)

    def test_commercial_can_reactivate_client(self):
        # Novo: "editar... Utilizadores do tipo Viewer e Client" inclui reativar.
        self.client.login(username="comm_act", password="123")
        response = self.client.post(f"{self.base_url}{self.inactive_user.id}/activate/")
        self.assertEqual(response.status_code, 200)
        self.inactive_user.refresh_from_db()
        self.assertTrue(self.inactive_user.is_active)

    def test_commercial_cannot_reactivate_admin(self):
        self.client.login(username="comm_act", password="123")
        response = self.client.post(f"{self.base_url}{self.inactive_admin.id}/activate/")
        self.assertEqual(response.status_code, 403)
        self.inactive_admin.refresh_from_db()
        self.assertFalse(self.inactive_admin.is_active)  # Remains inactive

    def test_client_cannot_reactivate_anyone(self):
        User.objects.create_user(username="cli_act", password="123", email="cl@act.com")
        self.client.login(username="cli_act", password="123")
        response = self.client.post(f"{self.base_url}{self.inactive_user.id}/activate/")
        self.assertEqual(response.status_code, 403)
        self.inactive_user.refresh_from_db()
        self.assertFalse(self.inactive_user.is_active)


class UserListFilterTests(TestCase):
    def setUp(self):
        self.client = Client()

        self.admin = User.objects.create_user(username="adm_f", password="123", email="a@f.com")
        UserProfile.objects.filter(user=self.admin).update(role=UserProfile.ADMIN)
        self.commercial = User.objects.create_user(username="comm_f", password="123", email="c@f.com")
        UserProfile.objects.filter(user=self.commercial).update(role=UserProfile.COMMERCIAL_GRANTS)

        def make_user(username, role, main_cae, region, address):
            u = User.objects.create_user(username=username, password="123", email=f"{username}@f.com")
            UserProfile.objects.filter(user=u).update(
                role=role, main_cae=main_cae, region=region, address=address,
            )
            return u

        # viewer (lead do match sem login) e client — os dois tipos que o commercial gere.
        self.ca = make_user("client_a", UserProfile.VIEWER, "62010", "Norte", "Street A")
        self.cb = make_user("client_b", UserProfile.CLIENT, "62020", "Sul", "Street B")
        self.cc = make_user("client_c", UserProfile.VIEWER, "47110", "Norte", "Street C")

        self.base_url = "/users/"

    def _usernames(self, response):
        return {u["username"] for u in response.json()["users"]}

    # --- main_cae by prefix (2 chars) ---
    def test_commercial_filter_main_cae_prefix(self):
        self.client.login(username="comm_f", password="123")
        resp = self.client.get(f"{self.base_url}?main_cae=62")
        self.assertEqual(self._usernames(resp), {"client_a", "client_b"})  # 62* but not 47*

    def test_commercial_filter_main_cae_full(self):
        self.client.login(username="comm_f", password="123")
        resp = self.client.get(f"{self.base_url}?main_cae=62010")
        self.assertEqual(self._usernames(resp), {"client_a"})

    # --- commercial sees viewer AND client (never admin/commercial/superuser) ---
    def test_commercial_sees_viewers_and_clients(self):
        self.client.login(username="comm_f", password="123")
        names = self._usernames(self.client.get(f"{self.base_url}"))
        self.assertNotIn("adm_f", names)
        self.assertNotIn("comm_f", names)
        # client_a/client_c são viewer, client_b é client — os três aparecem.
        self.assertEqual(names, {"client_a", "client_b", "client_c"})

    def test_commercial_sees_inactive_viewers(self):
        # Os viewers criados pelo match sem login ficam is_active=False — o commercial tem de
        # os ver mesmo assim (é precisamente a lista de leads a converter).
        self.ca.is_active = False
        self.ca.save(update_fields=["is_active"])
        self.client.login(username="comm_f", password="123")
        names = self._usernames(self.client.get(f"{self.base_url}"))
        self.assertIn("client_a", names)

    def test_commercial_never_sees_superuser_even_with_matching_role(self):
        # Um superuser tem profile.role='client' por omissão (o signal não o promove) — sem a
        # exclusão explícita, calharia dentro de qualquer filtro por role. Aqui simula-se o caso
        # mais direto: um superuser cujo profile.role foi posto a 'viewer'.
        su = User.objects.create_superuser(username="root_f", password="123", email="r@f.com")
        UserProfile.objects.filter(user=su).update(role=UserProfile.VIEWER)
        self.client.login(username="comm_f", password="123")
        names = self._usernames(self.client.get(f"{self.base_url}"))
        self.assertNotIn("root_f", names)

    def test_admin_sees_superuser(self):
        User.objects.create_superuser(username="root_f2", password="123", email="r2@f.com")
        self.client.login(username="adm_f", password="123")
        names = self._usernames(self.client.get(f"{self.base_url}"))
        self.assertIn("root_f2", names)

    # --- commercial CANNOT filter by address (param ignored) ---
    def test_commercial_cannot_filter_by_address(self):
        self.client.login(username="comm_f", password="123")
        resp = self.client.get(f"{self.base_url}?address=Street A")
        # address is ignored → returns all clients, not just the one on "Street A"
        self.assertEqual(self._usernames(resp), {"client_a", "client_b", "client_c"})

    # --- admin CAN filter by address ---
    def test_admin_can_filter_by_address(self):
        self.client.login(username="adm_f", password="123")
        resp = self.client.get(f"{self.base_url}?address=Street A")
        self.assertEqual(self._usernames(resp), {"client_a"})

    # --- region filter (both) ---
    def test_commercial_filter_region(self):
        self.client.login(username="comm_f", password="123")
        resp = self.client.get(f"{self.base_url}?region=Norte")
        self.assertEqual(self._usernames(resp), {"client_a", "client_c"})

    # --- detail endpoint: mesma regra (commercial só vê viewer, nunca superuser) ---
    def test_commercial_can_view_viewer_detail(self):
        self.client.login(username="comm_f", password="123")
        resp = self.client.get(f"{self.base_url}{self.ca.id}/")
        self.assertEqual(resp.status_code, 200)

    def test_commercial_cannot_view_admin_detail(self):
        self.client.login(username="comm_f", password="123")
        resp = self.client.get(f"{self.base_url}{self.admin.id}/")
        self.assertEqual(resp.status_code, 403)

    def test_commercial_cannot_view_superuser_detail_even_as_viewer_role(self):
        su = User.objects.create_superuser(username="root_f3", password="123", email="r3@f.com")
        UserProfile.objects.filter(user=su).update(role=UserProfile.VIEWER)
        self.client.login(username="comm_f", password="123")
        resp = self.client.get(f"{self.base_url}{su.id}/")
        self.assertEqual(resp.status_code, 403)

    def test_commercial_can_view_client_detail(self):
        self.client.login(username="comm_f", password="123")
        resp = self.client.get(f"{self.base_url}{self.cb.id}/")
        self.assertEqual(resp.status_code, 200)

    # --- update endpoint: commercial edita viewer/client de outrem, mas não o próprio role ---
    def test_commercial_can_update_client_profile(self):
        self.client.login(username="comm_f", password="123")
        resp = self.client.put(
            f"{self.base_url}{self.cb.id}/update/",
            data=json.dumps({"address": "Rua Nova"}), content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.cb.profile.refresh_from_db()
        self.assertEqual(self.cb.profile.address, "Rua Nova")

    def test_commercial_cannot_change_role_via_update(self):
        self.client.login(username="comm_f", password="123")
        self.client.put(
            f"{self.base_url}{self.cb.id}/update/",
            data=json.dumps({"role": UserProfile.ADMIN}), content_type="application/json",
        )
        self.cb.profile.refresh_from_db()
        self.assertEqual(self.cb.profile.role, UserProfile.CLIENT)  # ignorado

    def test_commercial_cannot_update_admin_profile(self):
        self.client.login(username="comm_f", password="123")
        resp = self.client.put(
            f"{self.base_url}{self.admin.id}/update/",
            data=json.dumps({"address": "Invasão"}), content_type="application/json",
        )
        self.assertEqual(resp.status_code, 403)

    # --- password reset: commercial reseta a de viewer/client sem saber a atual ---
    def test_commercial_can_reset_client_password(self):
        self.client.login(username="comm_f", password="123")
        resp = self.client.post(
            f"{self.base_url}{self.cb.id}/password/",
            data=json.dumps({"password": "novaPassword123"}), content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.cb.refresh_from_db()
        self.assertTrue(self.cb.check_password("novaPassword123"))

    def test_commercial_cannot_reset_admin_password(self):
        self.client.login(username="comm_f", password="123")
        resp = self.client.post(
            f"{self.base_url}{self.admin.id}/password/",
            data=json.dumps({"password": "novaPassword123"}), content_type="application/json",
        )
        self.assertEqual(resp.status_code, 403)


class UserFieldValidationTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.base_url = "/users/create/"

        # Usa o admin para ignorar bloqueios de permissão na criação
        self.admin_user = User.objects.create_user(username="admin_val", password="123")
        UserProfile.objects.filter(user=self.admin_user).update(role=UserProfile.ADMIN)
        self.client.login(username="admin_val", password="123")

        self.valid_payload = {
            "username": "test_validation",
            "password": "strongPassword123",
            "email": "val@mail.com",
            "role": UserProfile.CLIENT,
            "entity_type": UserProfile.EMPRESA,
            "entity_size": UserProfile.MEDIA,
            "nif": "123456789",
            "main_cae": "62010",
            "address": "Main Street",
            "region": "Norte",
            "nuts_ii": True,
            "nuts_iii": False,
        }

    # --- REGRA 1: ENUMS ---
    def test_enums_validation(self):
        """Garante que role, entity_type e entity_size só aceitam os valores definidos."""
        fields_to_test = {
            "role": "INVALID_ROLE",
            "entity_type": "INVALID_TYPE",
            "entity_size": "INVALID_SIZE"
        }

        for field, invalid_value in fields_to_test.items():
            payload = self.valid_payload.copy()
            payload[field] = invalid_value

            response = self.client.post(
                self.base_url,
                data=json.dumps(payload),
                content_type="application/json",
            )
            self.assertEqual(
                response.status_code, 400,
                f"O campo {field} aceitou um valor inválido ({invalid_value})."
            )

    # --- REGRA 2: NIF (Exatamente 9 caracteres) ---
    def test_nif_exact_length(self):
        """Garante que o NIF é rejeitado se não tiver exatamente 9 caracteres."""
        invalid_nifs = ["12345678", "1234567890"]  # 8 e 10 caracteres

        for nif in invalid_nifs:
            payload = self.valid_payload.copy()
            payload["nif"] = nif

            response = self.client.post(
                self.base_url,
                data=json.dumps(payload),
                content_type="application/json",
            )
            self.assertEqual(
                response.status_code, 400,
                f"O NIF com tamanho {len(nif)} não foi rejeitado."
            )

    # --- REGRA 3: CAE (Exatamente 5 caracteres) ---
    def test_cae_exact_length(self):
        """Garante que o main_cae e secondary_cae têm exatamente 5 caracteres."""
        # Testar main_cae
        payload_main = self.valid_payload.copy()
        payload_main["main_cae"] = "6201" # 4 caracteres
        response = self.client.post(self.base_url, data=json.dumps(payload_main), content_type="application/json")
        self.assertEqual(response.status_code, 400, "main_cae aceitou tamanho inválido.")

        # Testar secondary_cae (já validado diretamente no service.py)
        payload_sec = self.valid_payload.copy()
        payload_sec["secondary_cae"] = ["62010", "1234"] # Um válido, outro inválido
        response = self.client.post(self.base_url, data=json.dumps(payload_sec), content_type="application/json")
        self.assertEqual(response.status_code, 400, "secondary_cae aceitou tamanho inválido.")

    # --- REGRA 4 e 5: Limites Máximos (Address e Region) ---
    def test_max_lengths(self):
        """Garante que address (255 chars) e region (100 chars) respeitam os limites."""
        payload_address = self.valid_payload.copy()
        payload_address["address"] = "A" * 256
        response_add = self.client.post(self.base_url, data=json.dumps(payload_address), content_type="application/json")
        self.assertEqual(response_add.status_code, 400, "address ultrapassou o limite máximo (255).")

        payload_region = self.valid_payload.copy()
        payload_region["region"] = "R" * 101
        response_reg = self.client.post(self.base_url, data=json.dumps(payload_region), content_type="application/json")
        self.assertEqual(response_reg.status_code, 400, "region ultrapassou o limite máximo (100).")

    # --- REGRA 6: Campos obrigatórios vazios (Strings Vazias) ---
    def test_client_empty_required_fields(self):
        """Garante que se enviar o campo mas com string vazia (''), dá erro."""
        required_fields = ["nif", "main_cae", "address", "region"]

        for field in required_fields:
            payload = self.valid_payload.copy()
            payload[field] = ""  # String vazia em vez de eliminar a chave

            response = self.client.post(
                self.base_url,
                data=json.dumps(payload),
                content_type="application/json",
            )
            self.assertEqual(
                response.status_code, 400,
                f"O campo obrigatório '{field}' aceitou uma string vazia."
            )


class PasswordResetTests(TestCase):
    """Pedido + confirmação de reset de password por email (nunca revela se a conta existe)."""

    def setUp(self):
        self.client = Client()
        cache.clear()  # isola o throttle entre testes
        self.user = User.objects.create_user(
            "com_reset", email="reset@x.pt", password="Xk93!vTq21mZ")

        # Viewer — sem password utilizável e inativo, como create_or_update_viewer.
        self.viewer = User.objects.create_user(
            "999888777", email="lead@x.pt", is_active=False)
        self.viewer.set_unusable_password()
        self.viewer.save()

    def _token_for(self, user):
        uidb64 = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user)
        return uidb64, token

    # --- service layer ---
    def test_request_reset_sends_email_for_valid_user(self):
        service.request_password_reset("reset@x.pt")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["reset@x.pt"])
        body = mail.outbox[0].alternatives[0][0]  # versão HTML
        self.assertNotIn("https://example.com", body)  # placeholder substituído
        self.assertIn("uid=", body)
        self.assertIn("token=", body)

    def test_request_reset_silent_for_unknown_email(self):
        service.request_password_reset("ninguem@x.pt")
        self.assertEqual(len(mail.outbox), 0)

    def test_request_reset_works_for_viewer_without_usable_password(self):
        # Serve também para DEFINIR a 1ª password (não só repor uma esquecida) — o viewer
        # nunca teve password nenhuma, mas continua inativo (is_active=False) até promovido,
        # por isso não há risco em deixá-lo passar por aqui.
        service.request_password_reset("lead@x.pt")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["lead@x.pt"])

    def test_reset_confirm_sets_first_password_for_viewer(self):
        uidb64, token = self._token_for(self.viewer)
        service.reset_password_with_token(uidb64, token, "primeiraPasswordForte123")
        self.viewer.refresh_from_db()
        self.assertTrue(self.viewer.check_password("primeiraPasswordForte123"))
        # Continua inativo — ter password não é o mesmo que poder entrar.
        self.assertFalse(self.viewer.is_active)

    def test_reset_confirm_with_valid_token_changes_password(self):
        uidb64, token = self._token_for(self.user)
        service.reset_password_with_token(uidb64, token, "novaPasswordForte123")
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("novaPasswordForte123"))

    def test_reset_confirm_with_invalid_token_raises(self):
        uidb64, _ = self._token_for(self.user)
        with self.assertRaises(ValueError):
            service.reset_password_with_token(uidb64, "token-invalido", "novaPasswordForte123")

    def test_reset_confirm_with_garbage_uid_raises(self):
        with self.assertRaises(ValueError):
            service.reset_password_with_token("lixo-nao-base64", "qualquer", "novaPasswordForte123")

    def test_reset_confirm_with_weak_password_raises(self):
        uidb64, token = self._token_for(self.user)
        with self.assertRaises(ValueError):
            service.reset_password_with_token(uidb64, token, "123")

    def test_token_invalidated_after_password_already_changed(self):
        # O token está ligado ao hash da password atual — usar depois de já ter mudado falha.
        uidb64, token = self._token_for(self.user)
        self.user.set_password("outraPasswordForte456")
        self.user.save()
        with self.assertRaises(ValueError):
            service.reset_password_with_token(uidb64, token, "maisUmaPasswordForte789")

    # --- view layer ---
    def test_reset_request_view_same_response_known_and_unknown_email(self):
        r1 = self.client.post("/users/password-reset/",
                              data=json.dumps({"email": "reset@x.pt"}), content_type="application/json")
        cache.clear()  # não deixar o throttle do 1º pedido interferir na comparação
        r2 = self.client.post("/users/password-reset/",
                              data=json.dumps({"email": "ninguem@x.pt"}), content_type="application/json")
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r1.json(), r2.json())

    def test_reset_request_view_is_throttled(self):
        for _ in range(3):
            self.client.post("/users/password-reset/",
                             data=json.dumps({"email": "reset@x.pt"}), content_type="application/json")
        self.assertEqual(len(mail.outbox), 3)
        # 4º pedido dentro da janela: mesma resposta, mas não dispara mais um email.
        resp = self.client.post("/users/password-reset/",
                                data=json.dumps({"email": "reset@x.pt"}), content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(mail.outbox), 3)  # continua 3 — não enviou o 4º

    def test_reset_confirm_view_success_and_can_login_with_new_password(self):
        uidb64, token = self._token_for(self.user)
        resp = self.client.post(
            "/users/password-reset/confirm/",
            data=json.dumps({"uid": uidb64, "token": token, "password": "novaPasswordForte123"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(
            self.client.login(username="com_reset", password="novaPasswordForte123"))

    def test_reset_confirm_view_invalid_token_returns_400(self):
        uidb64, _ = self._token_for(self.user)
        resp = self.client.post(
            "/users/password-reset/confirm/",
            data=json.dumps({"uid": uidb64, "token": "lixo", "password": "novaPasswordForte123"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_reset_request_view_rejects_get(self):
        self.assertEqual(self.client.get("/users/password-reset/").status_code, 405)