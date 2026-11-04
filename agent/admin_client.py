import requests

class AdminClient:
    """Harness-only admin client on port 8081. Never imported by loop.py."""
    def __init__(self, base_url='http://localhost:8081', timeout=30):
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout
        self.session = requests.Session()

    def set_command(self, text: str):
        resp = self.session.post(f"{self.base_url}/admin/command", json={"text": text}, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def reset(self):
        resp = self.session.post(f"{self.base_url}/admin/reset", timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def give_up(self):
        resp = self.session.post(f"{self.base_url}/admin/giveup", timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def verdict(self, is_pass: bool):
        resp = self.session.post(f"{self.base_url}/admin/verdict", json={"pass": bool(is_pass)}, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def result(self):
        resp = self.session.get(f"{self.base_url}/admin/result", timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

