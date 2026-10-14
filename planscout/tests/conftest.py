from datetime import date

import pytest
from fastapi.testclient import TestClient

from planscout.catalog import Catalog
from planscout.config import Settings
from planscout.main import create_app
from planscout.models import AccountSnapshot

TODAY = date(2026, 10, 14)


@pytest.fixture(scope="session")
def catalog() -> Catalog:
    return Catalog.load()


@pytest.fixture
def make_snapshot():
    catalog = Catalog.load()

    def make(carrier_id="summit", plan_id="summit-unlimited-elite", cost=None, autopay=True, lines=None):
        lines = lines or [{"label": "Line 1", "avg_data_gb": 3}]
        if cost is None:
            cost = catalog.plan(plan_id).monthly_cost(len(lines), autopay)
        return AccountSnapshot(
            carrier_id=carrier_id,
            plan_name=plan_id or "",
            plan_id=plan_id,
            monthly_plan_cost=cost,
            autopay=autopay,
            lines=lines,
        )

    return make


@pytest.fixture
def client(tmp_path):
    settings = Settings(db_path=str(tmp_path / "test.db"))
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def signed_in(client):
    response = client.post("/api/auth/signup", json={"email": "pat@example.com", "password": "correct horse battery"})
    assert response.status_code == 201
    return client
