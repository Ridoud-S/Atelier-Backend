"""HTTP-level security: identity only from the verified token; users cannot reach each other's data."""
from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.api.deps import get_token_verifier
from app.core.security import StaticKeyProvider, TokenVerifier
from app.db.session import get_session
from app.main import create_app
from app.models import Habit, HabitLog, Profile, Task, UserGameProfile, XpTransaction
from tests import auth_helpers as ah
from tests.factories import make_project, make_task


@pytest.fixture(scope="module")
def signing_key():
    return ah.new_ec_key()


@pytest.fixture
def make_client(session_factory, signing_key):
    verifier = TokenVerifier(StaticKeyProvider(signing_key.public_key(), ["ES256"]), issuer=ah.ISSUER)

    def build() -> TestClient:
        app = create_app()

        def _session():
            with session_factory() as s:
                try:
                    yield s
                except Exception:
                    s.rollback()
                    raise

        app.dependency_overrides[get_session] = _session
        app.dependency_overrides[get_token_verifier] = lambda: verifier
        return TestClient(app)

    return build


@pytest.fixture
def client(make_client):
    return make_client()


@pytest.fixture
def alice():
    return uuid.uuid4()


@pytest.fixture
def bob():
    return uuid.uuid4()


@pytest.fixture
def as_user(signing_key):
    def headers(user_id) -> dict:
        return {"Authorization": f"Bearer {ah.sign_es256(signing_key, ah.claims(user_id))}"}

    return headers


@pytest.fixture
def seeded(reference_data):
    return None


def new_habit(client, headers, name="Read"):
    r = client.post("/api/v1/habits", json={"name": name, "category": "focus", "start_date": "2026-10-01"}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def new_task(client, headers, **extra):
    r = client.post("/api/v1/tasks", json={"title": "T", **extra}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["id"]


# --- authentication -----------------------------------------------------------------------------
@pytest.mark.parametrize("path", ["/api/v1/me", f"/api/v1/habits/{uuid.uuid4()}", f"/api/v1/tasks/{uuid.uuid4()}"])
def test_every_endpoint_requires_a_token(client, path):
    r = client.get(path)
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("header", ["Bearer", "Bearer not-a-jwt", "Basic dXNlcjpwYXNz", "Token abc"])
def test_malformed_credentials_are_rejected(client, header):
    assert client.get("/api/v1/me", headers={"Authorization": header}).status_code == 401


def test_a_token_signed_with_another_key_is_rejected_by_the_api(client, alice):
    forged = ah.sign_es256(ah.new_ec_key(), ah.claims(alice))
    assert client.get("/api/v1/me", headers={"Authorization": f"Bearer {forged}"}).status_code == 401


def test_tampering_with_the_subject_cannot_impersonate_another_user(client, as_user, alice, bob):
    token = as_user(alice)["Authorization"].split()[1]
    forged = ah.tamper_subject(token, bob)
    assert client.get("/api/v1/me", headers={"Authorization": f"Bearer {forged}"}).status_code == 401


def test_health_is_public(client):
    assert client.get("/health").json()["status"] == "ok"


# --- provisioning from the token ---------------------------------------------------------------------
def test_first_authenticated_request_provisions_the_user_from_the_token(client, session, as_user, alice):
    r = client.get("/api/v1/me", headers=as_user(alice))
    assert r.status_code == 200
    assert r.json() == {"id": str(alice), "timezone": "UTC", "total_xp": 0, "level": 1}
    assert client.get("/api/v1/me", headers=as_user(alice)).status_code == 200  # idempotent
    assert session.execute(select(func.count()).select_from(Profile)).scalar_one() == 1


def test_the_client_cannot_choose_the_user_id(client, as_user, alice, bob):
    for body in (
        {"name": "x", "category": "focus", "start_date": "2026-10-01", "user_id": str(bob)},
    ):
        assert client.post("/api/v1/habits", json=body, headers=as_user(alice)).status_code == 422
    assert client.post("/api/v1/tasks", json={"title": "x", "user_id": str(bob)}, headers=as_user(alice)).status_code == 422
    assert client.post(f"/api/v1/habits/{uuid.uuid4()}/logs", json={"occurrence_date": "2026-10-20", "user_id": str(bob)}, headers=as_user(alice)).status_code == 422


def test_created_rows_belong_to_the_token_user(client, session, as_user, alice, bob):
    habit_id = new_habit(client, as_user(alice))
    task_id = new_task(client, as_user(bob))
    session.expire_all()
    assert str(session.get(Habit, uuid.UUID(habit_id)).user_id) == str(alice)
    assert str(session.get(Task, uuid.UUID(task_id)).user_id) == str(bob)


# --- cross-user access --------------------------------------------------------------------------------
def test_reading_another_users_habit_is_indistinguishable_from_not_found(client, as_user, alice, bob):
    habit_id = new_habit(client, as_user(alice))
    foreign = client.get(f"/api/v1/habits/{habit_id}", headers=as_user(bob))
    missing = client.get(f"/api/v1/habits/{uuid.uuid4()}", headers=as_user(bob))
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json()["error"] == missing.json()["error"]  # no existence oracle
    assert client.get(f"/api/v1/habits/{habit_id}", headers=as_user(alice)).status_code == 200


def test_another_user_cannot_log_or_archive_my_habit(client, session, seeded, as_user, alice, bob):
    habit_id = new_habit(client, as_user(alice))
    body = {"occurrence_date": (date.today() - timedelta(days=1)).isoformat()}
    assert client.post(f"/api/v1/habits/{habit_id}/logs", json=body, headers=as_user(bob)).status_code == 404
    assert client.post(f"/api/v1/habits/{habit_id}/archive", headers=as_user(bob)).status_code == 404
    session.expire_all()
    assert session.get(Habit, uuid.UUID(habit_id)).status == "active"
    assert session.execute(select(func.count()).select_from(HabitLog)).scalar_one() == 0


def test_another_user_cannot_read_or_complete_my_task_nor_earn_xp_from_it(client, session, seeded, as_user, alice, bob):
    task_id = new_task(client, as_user(alice))
    assert client.get(f"/api/v1/tasks/{task_id}", headers=as_user(bob)).status_code == 404
    assert client.post(f"/api/v1/tasks/{task_id}/status", json={"status": "done"}, headers=as_user(bob)).status_code == 404
    session.expire_all()
    assert session.get(Task, uuid.UUID(task_id)).status == "todo"
    assert session.execute(select(func.count()).select_from(XpTransaction)).scalar_one() == 0


def test_cannot_attach_a_task_to_another_users_project(client, session, as_user, alice, bob):
    # Alice's project exists in the database (created directly); Bob tries to use it.
    from tests.factories import make_user
    from app.models import Project

    client.get("/api/v1/me", headers=as_user(alice))  # provisions Alice
    project = Project(user_id=alice, name="Private")
    session.add(project)
    session.commit()
    r = client.post("/api/v1/tasks", json={"title": "sneaky", "project_id": str(project.id)}, headers=as_user(bob))
    assert r.status_code == 404
    assert session.execute(select(func.count()).select_from(Task)).scalar_one() == 0
    ok = client.post("/api/v1/tasks", json={"title": "mine", "project_id": str(project.id)}, headers=as_user(alice))
    assert ok.status_code == 201


def test_xp_is_never_shared_between_users(client, seeded, as_user, alice, bob):
    task_id = new_task(client, as_user(alice))
    done = client.post(f"/api/v1/tasks/{task_id}/status", json={"status": "done"}, headers=as_user(alice))
    assert done.json()["xp"] == {"amount": 6, "created": True}
    assert client.get("/api/v1/me", headers=as_user(alice)).json()["total_xp"] == 6
    assert client.get("/api/v1/me", headers=as_user(bob)).json()["total_xp"] == 0


def test_database_level_isolation_holds_even_if_a_bug_skipped_the_backend_check(session, user_id, other_user_id):
    """Defence in depth: the composite FK blocks a cross-user reference at the database."""
    from sqlalchemy.exc import IntegrityError

    foreign_project = make_project(session, other_user_id)
    session.add(Task(user_id=user_id, title="x", project_id=foreign_project.id))
    with pytest.raises(IntegrityError):
        session.commit()


# --- retries and concurrent duplicates ---------------------------------------------------------------------
def test_retrying_a_completion_request_never_pays_twice(client, session, seeded, as_user, alice):
    h = as_user(alice)
    task_id = new_task(client, h)
    first = client.post(f"/api/v1/tasks/{task_id}/status", json={"status": "done"}, headers=h)
    retry = client.post(f"/api/v1/tasks/{task_id}/status", json={"status": "done"}, headers=h)
    assert (first.status_code, retry.status_code) == (200, 200)
    assert first.json()["xp"]["created"] is True and retry.json()["xp"]["created"] is False
    assert client.get("/api/v1/me", headers=h).json()["total_xp"] == 6


def test_reopening_through_the_api_does_not_allow_a_second_reward(client, seeded, as_user, alice):
    h = as_user(alice)
    task_id = new_task(client, h, priority="high")
    client.post(f"/api/v1/tasks/{task_id}/status", json={"status": "done"}, headers=h)
    assert client.post(f"/api/v1/tasks/{task_id}/status", json={"status": "todo"}, headers=h).status_code == 200
    again = client.post(f"/api/v1/tasks/{task_id}/status", json={"status": "done"}, headers=h)
    assert again.json()["xp"]["created"] is False
    assert client.get("/api/v1/me", headers=h).json()["total_xp"] == 10


def test_retrying_a_habit_log_request_is_idempotent(client, seeded, as_user, alice):
    h = as_user(alice)
    habit_id = new_habit(client, h)
    body = {"occurrence_date": (date.today() - timedelta(days=1)).isoformat()}
    a = client.post(f"/api/v1/habits/{habit_id}/logs", json=body, headers=h).json()
    b = client.post(f"/api/v1/habits/{habit_id}/logs", json=body, headers=h).json()
    assert a["id"] == b["id"] and a["xp"]["created"] and not b["xp"]["created"]


def test_simultaneous_identical_requests_pay_once(make_client, session, seeded, as_user, alice):
    headers = as_user(alice)
    task_id = new_task(make_client(), headers, priority="high")

    def call(_):
        c = make_client()  # each thread has its own client, like independent connections
        return c.post(f"/api/v1/tasks/{task_id}/status", json={"status": "done"}, headers=headers).json()["xp"]["created"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        created = list(pool.map(call, range(8)))
    assert created.count(True) == 1
    session.expire_all()
    assert session.execute(select(func.count()).select_from(XpTransaction)).scalar_one() == 1
    assert session.execute(select(UserGameProfile.total_xp).where(UserGameProfile.user_id == alice)).scalar_one() == 10


# --- error mapping -------------------------------------------------------------------------------------------
def test_invalid_transition_is_a_conflict(client, as_user, alice):
    h = as_user(alice)
    task_id = new_task(client, h)
    client.post(f"/api/v1/tasks/{task_id}/status", json={"status": "cancelled"}, headers=h)
    r = client.post(f"/api/v1/tasks/{task_id}/status", json={"status": "done"}, headers=h)
    assert r.status_code == 409 and r.json()["error"] == "invalid_transition"


def test_future_occurrence_is_rejected(client, seeded, as_user, alice):
    h = as_user(alice)
    habit_id = new_habit(client, h)
    body = {"occurrence_date": (date.today() + timedelta(days=2)).isoformat()}
    assert client.post(f"/api/v1/habits/{habit_id}/logs", json=body, headers=h).status_code == 422


def test_a_malformed_id_is_a_validation_error(client, as_user, alice):
    assert client.get("/api/v1/habits/not-a-uuid", headers=as_user(alice)).status_code == 422
