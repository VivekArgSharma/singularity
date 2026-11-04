import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import pytest
import requests

AGENT_BASE = 'http://localhost:8080'
ADMIN_BASE = 'http://localhost:8081'

def is_engine_connected():
    try:
        r = requests.get(f"{AGENT_BASE}/state", timeout=2)
        return r.status_code == 200
    except Exception:
        return False

@pytest.mark.skipif(not is_engine_connected(), reason="Engine browser tab not connected")
class TestEndpointsContract:
    def setup_method(self):
        # Reset engine before each test
        requests.post(f"{ADMIN_BASE}/admin/reset", timeout=5)

    def test_get_state(self):
        r = requests.get(f"{AGENT_BASE}/state")
        assert r.status_code == 200
        data = r.json()
        expected_keys = {'name', 'x', 'y', 'heading', 'holding', 'retries'}
        assert set(data.keys()) == expected_keys
        assert isinstance(data['name'], str)
        assert isinstance(data['x'], (int, float))
        assert isinstance(data['y'], (int, float))
        assert isinstance(data['heading'], (int, float))
        assert isinstance(data['holding'], bool)
        assert isinstance(data['retries'], int)

    def test_get_look(self):
        r = requests.get(f"{AGENT_BASE}/look")
        assert r.status_code == 200
        assert r.headers['content-type'].startswith('image/jpeg')
        # JPEG header check: FF D8
        assert len(r.content) > 100
        assert r.content[:2] == b'\xff\xd8'

    def test_get_depth(self):
        r = requests.get(f"{AGENT_BASE}/depth")
        assert r.status_code == 200
        data = r.json()
        assert set(data.keys()) == {'depth'}
        assert data['depth'] is None or isinstance(data['depth'], (int, float))

    def test_post_turn(self):
        r = requests.post(f"{AGENT_BASE}/turn", json={'degrees': 45})
        assert r.status_code == 200
        data = r.json()
        assert set(data.keys()) == {'heading'}
        assert data['heading'] == pytest.approx(45.0, abs=1.0)

    def test_post_move(self):
        r = requests.post(f"{AGENT_BASE}/move", json={'distance': 0.5})
        assert r.status_code == 200
        data = r.json()
        assert set(data.keys()) == {'moved', 'collided'}
        assert isinstance(data['moved'], (int, float))
        assert isinstance(data['collided'], bool)

    def test_post_pickup_empty(self):
        r = requests.post(f"{AGENT_BASE}/pickup")
        assert r.status_code == 200
        data = r.json()
        assert set(data.keys()) == {'ok'}
        assert isinstance(data['ok'], bool)

    def test_post_drop_empty(self):
        r = requests.post(f"{AGENT_BASE}/drop")
        assert r.status_code == 200
        data = r.json()
        assert set(data.keys()) == {'ok', 'in_zone'}
        assert isinstance(data['ok'], bool)
        assert isinstance(data['in_zone'], bool)
