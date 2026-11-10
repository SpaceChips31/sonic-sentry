from fastapi.testclient import TestClient as BaseTestClient


class TestClient(BaseTestClient):
    """Exercise form endpoints with the same CSRF cookie/header contract as a browser."""
    __test__ = False

    def request(self, method, url, **kwargs):
        if method.upper() not in {"GET", "HEAD", "OPTIONS"}:
            if not self.cookies.get("sonic_csrf"):
                super().request("GET", "/health")
            headers = dict(kwargs.get("headers") or {})
            headers.setdefault("X-CSRF-Token", self.cookies.get("sonic_csrf", ""))
            kwargs["headers"] = headers
        return super().request(method, url, **kwargs)
