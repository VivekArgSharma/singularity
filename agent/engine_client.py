import requests

class EngineClient:
    def __init__(self, base_url='http://localhost:8080', timeout=30):
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout
        self.session = requests.Session()

    def state(self):
        resp = self.session.get(f"{self.base_url}/state", timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def look(self) -> bytes:
        resp = self.session.get(f"{self.base_url}/look", timeout=self.timeout)
        resp.raise_for_status()
        return resp.content

    def depth(self):
        resp = self.session.get(f"{self.base_url}/depth", timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def move(self, distance: float):
        resp = self.session.post(
            f"{self.base_url}/move",
            json={"distance": float(distance)},
            timeout=self.timeout
        )
        resp.raise_for_status()
        return resp.json()

    def turn(self, degrees: float):
        resp = self.session.post(
            f"{self.base_url}/turn",
            json={"degrees": float(degrees)},
            timeout=self.timeout
        )
        resp.raise_for_status()
        return resp.json()

    def pickup(self):
        resp = self.session.post(f"{self.base_url}/pickup", timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def drop(self):
        resp = self.session.post(f"{self.base_url}/drop", timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

