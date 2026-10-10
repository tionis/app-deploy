# App Deploy

A small, dependency-free action for GitHub Actions and Forgejo Actions that
deploys a pushed container image digest through the Gandalf gateway.

**How it works:**
1. The job's OIDC identity token proves its repository and branch, so the job needs no
   secret.
2. The gateway accepts only digests of the image repository declared for the app.
3. It records the deployment and notifies the node the app is placed on.
4. The node applies it: backup gate, pull, restart, health check, and rollback on
   failure.
5. This action follows the deployment and prints the node's progress. While it waits, it
   rides out a forge or gateway that can't be reached or answers 429/5xx (with one warning
   per outage), since the deployment goes on without it.
6. It fails the job when the deployment is rolled back, fails, or gets no result within
   `timeout`. A refused request (4xx) fails it at once.

A newer deployment of the same app that supersedes this one ends the job with a notice.

The runner needs Bash and Python 3.11 or newer. The action builds nothing: build and
push the image first, then pass the pushed digest.

## GitHub Actions

```yaml
jobs:
  publish:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      packages: write
      id-token: write
    steps:
      - uses: actions/checkout@v6
      - uses: docker/setup-buildx-action@v4
      - uses: docker/login-action@v4
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
      - id: build
        uses: docker/build-push-action@v7
        with:
          push: true
          tags: ghcr.io/${{ github.repository }}:latest
      - name: Deploy through Gandalf
        if: github.ref == 'refs/heads/main'
        uses: tionis/app-deploy@<commit-sha>
        with:
          service: notes
          image: ghcr.io/${{ github.repository }}@${{ steps.build.outputs.digest }}
```

## Forgejo Actions

The job needs `enable-openid-connect: true`. The build steps depend on the runner;
with Docker available:

```yaml
jobs:
  publish:
    runs-on: docker
    enable-openid-connect: true
    steps:
      - uses: https://data.forgejo.org/actions/checkout@v6
      - name: Build and push
        id: build
        run: |
          image=forge.tionis.dev/${{ github.repository }}
          echo "${{ secrets.REGISTRY_TOKEN }}" | docker login forge.tionis.dev -u "${{ github.actor }}" --password-stdin
          docker build -t "$image:latest" .
          docker push "$image:latest"
          echo "image=$(docker inspect --format '{{index .RepoDigests 0}}' "$image:latest")" >> "$GITHUB_OUTPUT"
      - name: Deploy through Gandalf
        uses: https://forge.tionis.dev/actions/app-deploy@<commit-sha>
        with:
          service: notes
          image: ${{ steps.build.outputs.image }}
```

## Inputs

| Input | Default | Purpose |
| --- | --- | --- |
| `service` | required | The app's service id in Gandalf (a `web_app` with a `deploy` block). |
| `image` | required | The pushed image as `repository@sha256:<digest>`. |
| `gateway` | `https://gandalf.tionis.dev` | The Gandalf gateway. |
| `timeout` | `900` | Seconds to wait for the node's result. |

The design is in Gandalf's wiki, `admin/Reference/app-platform.md`.
