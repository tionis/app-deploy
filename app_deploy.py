#!/usr/bin/env python3
"""Deploy a pushed image digest through the Gandalf gateway and wait for it.

The job's OIDC identity token proves the repository and branch; the gateway
accepts only digests of the app's declared image repository, records the
deployment and notifies the node, which applies it with a health check and
rolls back on failure. This waits for that result and prints the node's
progress. Dependency-free; Python 3.11 or newer.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

DIGEST = re.compile(r"^[a-z0-9.-]+(?::[0-9]+)?(?:/[a-z0-9._-]+)+@sha256:[0-9a-f]{64}$")
POLL_SECONDS = 3
FINAL = {"live", "rolled_back", "failed", "superseded"}


class DeployError(Exception):
    pass


def request_identity_token(audience: str, opener=urllib.request.urlopen) -> str:
    """The running workflow's OIDC identity token, from Forgejo or GitHub."""
    url = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL", "")
    token = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "")
    if not url or not token:
        raise DeployError(
            "no workflow identity: let the job request an identity token "
            "(Forgejo: enable-openid-connect: true; GitHub: permissions: id-token: write)"
        )
    separator = "&" if "?" in url else "?"
    request = urllib.request.Request(
        f"{url}{separator}audience={urllib.parse.quote(audience, safe='')}",
        headers={"Authorization": f"bearer {token}", "Accept": "application/json"},
    )
    try:
        with opener(request, timeout=30) as response:
            value = json.loads(response.read()).get("value")
    except urllib.error.HTTPError as exc:
        exc.read()
        raise DeployError(f"the workflow identity token could not be requested: HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, ValueError, AttributeError) as exc:
        raise DeployError(f"the workflow identity token could not be requested: {exc}") from exc
    if not isinstance(value, str) or not value:
        raise DeployError("the CI provider returned no identity token")
    return value


def gateway_request(
    gateway: str,
    path: str,
    token: str,
    body: dict[str, Any] | None = None,
    opener=urllib.request.urlopen,
) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{gateway.rstrip('/')}{path}",
        data=None if body is None else json.dumps(body).encode(),
        method="GET" if body is None else "POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            **({} if body is None else {"Content-Type": "application/json"}),
        },
    )
    try:
        with opener(request, timeout=60) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read()
        try:
            message = str(json.loads(detail).get("error") or "")
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            message = ""
        raise DeployError(f"Gandalf {request.method} {path} failed: HTTP {exc.code}{': ' + message if message else ''}") from exc
    except urllib.error.URLError as exc:
        raise DeployError(f"Gandalf could not be reached: {exc.reason}") from exc


def deploy(
    service: str,
    image: str,
    gateway: str,
    revision: str | None,
    run: str,
    timeout: float,
    identity: Callable[[str], str],
    call: Callable[..., dict[str, Any]],
    log: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """0 when live (or superseded by a newer deployment), 1 otherwise."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", service):
        raise DeployError(f"{service!r} is not a service id")
    if not DIGEST.match(image):
        raise DeployError(f"{image!r} is not repository@sha256:<digest>; deploy the pushed digest, not a tag")
    audience = urllib.parse.urlsplit(gateway)._replace(path="", query="", fragment="").geturl()
    if revision is not None and not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", revision):
        revision = None
    started = call(
        gateway,
        f"/v1/apps/{service}/deployments",
        identity(audience),
        {"image": image, "revision": revision, "idempotency_key": run[:200]},
    )["deployment"]
    deployment_id = started["deployment_id"]
    log(
        f"deployment {deployment_id}: {image} for {service} on {started['node']}"
        + ("" if started.get("notified") else " (the node finds it on its next poll)")
    )
    deadline = clock() + timeout
    seen = None
    current = started
    while True:
        status, detail = current["status"], current.get("detail")
        if (status, detail) != seen:
            seen = (status, detail)
            log(f"{status}{': ' + detail if detail else ''}")
        if status in FINAL:
            break
        if clock() >= deadline:
            log(f"::error::no result from {started['node']} after {int(timeout)} s (status {status})")
            return 1
        sleep(POLL_SECONDS)
        # A fresh identity token for each request: they expire within minutes.
        current = call(
            gateway, f"/v1/apps/{service}/deployments/{deployment_id}", identity(audience)
        )["deployment"]
    if status == "live":
        return 0
    if status == "superseded":
        log("::notice::a newer deployment of this app superseded this one")
        return 0
    log(f"::error::{service} was {status.replace('_', ' ')} on {started['node']}")
    return 1


def main() -> int:
    try:
        return deploy(
            os.environ.get("APP_DEPLOY_SERVICE", "").strip(),
            os.environ.get("APP_DEPLOY_IMAGE", "").strip(),
            os.environ.get("APP_DEPLOY_GATEWAY", "https://gandalf.tionis.dev").strip(),
            os.environ.get("APP_DEPLOY_REVISION", "").strip()[:40] or None,
            os.environ.get("APP_DEPLOY_RUN", "manual").strip(),
            float(os.environ.get("APP_DEPLOY_TIMEOUT", "900")),
            request_identity_token,
            gateway_request,
        )
    except (DeployError, KeyError, ValueError) as exc:
        print(f"app-deploy: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
