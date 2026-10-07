import importlib.util
from pathlib import Path
import sys

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "app_deploy.py"
SPEC = importlib.util.spec_from_file_location("app_deploy_action", MODULE_PATH)
assert SPEC and SPEC.loader
app_deploy = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = app_deploy
SPEC.loader.exec_module(app_deploy)

IMAGE = f"forge.example/eric/notes@sha256:{'a' * 64}"


class FakeGateway:
    """Answers the start and then the given statuses, one per poll."""

    def __init__(self, statuses, notified=True):
        self.statuses = list(statuses)
        self.requests = []
        self.notified = notified

    def __call__(self, gateway, path, token, body=None):
        self.requests.append((path, token, body))
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        detail = {"applying": "pulling", "rolled_back": "not healthy after 120 s"}.get(status)
        return {
            "deployment": {
                "deployment_id": "d-1",
                "node": "citadel",
                "status": status,
                "detail": detail,
                "notified": self.notified,
            }
        }


class Clock:
    def __init__(self):
        self.now = 0.0

    def sleep(self, seconds):
        self.now += seconds


def run(statuses, timeout=60, **kwargs):
    gateway, clock, lines, tokens = FakeGateway(statuses, **kwargs), Clock(), [], iter(range(1000))
    code = app_deploy.deploy(
        "notes",
        IMAGE,
        "https://gandalf.example/",
        "abc1234",
        "eric/notes:42:1",
        timeout,
        lambda audience: f"token-{next(tokens)}-{audience}",
        gateway,
        lines.append,
        clock.sleep,
        lambda: clock.now,
    )
    return code, gateway, lines


def test_a_live_deployment_succeeds_and_prints_the_progress():
    code, gateway, lines = run(["pending", "applying", "applying", "live"])
    assert code == 0
    path, token, body = gateway.requests[0]
    assert path == "/v1/apps/notes/deployments"
    assert body == {"image": IMAGE, "revision": "abc1234", "idempotency_key": "eric/notes:42:1"}
    assert token.endswith("-https://gandalf.example")
    # Each request has its own identity token; every status is printed once.
    assert len({token for _path, token, _body in gateway.requests}) == len(gateway.requests)
    assert gateway.requests[1][0] == "/v1/apps/notes/deployments/d-1"
    assert lines[1:] == ["pending", "applying: pulling", "live"]


def test_a_rollback_fails_the_job_with_the_reason():
    code, _gateway, lines = run(["pending", "rolled_back"])
    assert code == 1
    assert "rolled_back: not healthy after 120 s" in lines
    assert lines[-1] == "::error::notes was rolled back on citadel"


def test_a_superseded_deployment_ends_with_a_notice():
    code, _gateway, lines = run(["pending", "superseded"])
    assert code == 0
    assert lines[-1].startswith("::notice::")


def test_waiting_ends_with_the_timeout():
    code, _gateway, lines = run(["pending"], timeout=10)
    assert code == 1
    assert lines[-1].startswith("::error::no result from citadel after 10 s")


def test_an_unnotified_node_is_mentioned():
    _code, _gateway, lines = run(["live"], notified=False)
    assert "next poll" in lines[0]


@pytest.mark.parametrize(
    "image",
    [
        "forge.example/eric/notes:latest",
        "forge.example/eric/notes@sha256:" + "a" * 63,
        "notes@sha256:" + "a" * 64,
    ],
)
def test_only_pushed_digests_deploy(image):
    with pytest.raises(app_deploy.DeployError, match="digest"):
        app_deploy.deploy("notes", image, "https://g", None, "r", 1, str, None)


def test_the_identity_token_needs_the_job_permission(monkeypatch):
    monkeypatch.delenv("ACTIONS_ID_TOKEN_REQUEST_URL", raising=False)
    with pytest.raises(app_deploy.DeployError, match="id-token: write"):
        app_deploy.request_identity_token("https://g")
