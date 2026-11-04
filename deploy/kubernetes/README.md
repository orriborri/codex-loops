# Kubernetes deployment for the Flux/Talos homelab

This is an operator-editable starting point for **one** runner and one target repository. It runs the implement, review and merge controllers in one Pod, with an optional improve container; the existing shared file lock serializes work. Each model or configured JUnit run uses a disposable Kubernetes Job in a separate namespace. Docker Compose remains supported with `JOB_BACKEND=docker` (the default).

The example uses the `syno-storage` block CSI StorageClass defined in the homelab repository. Confirm that it is installed, healthy, and backed up on the actual cluster. It is intentionally not using the NFS class for SQLite. `ReadWriteOnce` means a single **node**, not necessarily one Pod; this example uses one replica and `Recreate` to avoid ordinary overlapping rollouts. It is not HA or a distributed lock. Avoid forced Pod deletion during a node partition; confirm the old controller and Jobs have stopped before recovery. Use `ReadWriteOncePod` only after verifying that the cluster and CSI driver support it.

## Before applying

1. Verify cluster access, node architecture and resources; check `kubectl get nodes` and `kubectl get storageclass syno-storage`. The agent defaults to two CPUs and 4 GiB RAM per job, plus the staging container and workspace disk. Reduce `KUBE_JOB_CPU` / `KUBE_JOB_MEMORY` or the namespace quota only after allowing enough space for the actual project.
2. Build and publish a **controller** image from `Dockerfile.kubernetes`. Build the base agent from `Dockerfile.agent` with a pinned `CODEX_VERSION`, then derive the **Kubernetes agent** with `Dockerfile.agent.kubernetes` and `BASE_AGENT_IMAGE` set to that base. The derivative adds Python 3.12 and checks for the safe archive filter required by the Kubernetes protocol; the regular Docker agent is unchanged. For the controller build, set `KUBECTL_VERSION` compatible with the cluster, `TARGETARCH` (`amd64` or `arm64`) and the matching official `KUBECTL_SHA256`; the image build checks the binary before installation. Do not build credentials into either image. Publish to a registry reachable by every eligible node, and use immutable image digests in production.
3. In a reviewed overlay, replace the controller image in `deployment.yaml` and `AGENT_IMAGE` in `config.yaml`; set `GITHUB_REPOSITORY`, `REVIEWER_LOGIN`, `REQUIRED_CHECKS`, and `RISK_APPROVERS` for the target repository. Keep `AUTO_MERGE=false` until the integration trial and server-side branch rules have been verified. `KUBE_RUNNER_ID` must be unique among runners sharing the worker namespace; prefer a dedicated worker namespace per runner.
4. Provision a Secret **only in `codex-loops`** named `codex-github-tokens` with keys `implement`, `review`, `merge` and optionally `improve`. Use separate GitHub identities for author and reviewer, and the permissions listed in the main README. Store it encrypted with SOPS in the homelab GitOps repository, or create it securely outside Git; never commit plaintext. The worker namespace must not contain these GitHub tokens, controller state, SOPS keys, or other applications' Secrets.
5. Render the manifests with `kubectl kustomize deploy/kubernetes` and review namespaces, quota, images, storage, RBAC and substitutions. `login-job.example.yaml` is deliberately excluded from the Kustomization. Use the existing homelab Flux/Kustomize/SOPS conventions in a separate reviewed overlay; do not put placeholder credentials or these unedited manifests into an active Flux path. This example does not publish images or configure Flux automatically.

For example, from a checkout on a machine with Docker, substitute your registry, version values, architecture and independently verified kubectl checksum:

```sh
docker build -f Dockerfile.agent --build-arg CODEX_VERSION=YOUR_PINNED_VERSION -t REGISTRY/agent-base:TAG .
docker build -f Dockerfile.agent.kubernetes --build-arg BASE_AGENT_IMAGE=REGISTRY/agent-base:TAG -t REGISTRY/agent:TAG .
docker build -f Dockerfile.kubernetes --build-arg KUBECTL_VERSION=YOUR_KUBECTL_VERSION \
  --build-arg KUBECTL_SHA256=YOUR_ARCH_SPECIFIC_SHA256 --build-arg TARGETARCH=amd64 \
  -t REGISTRY/controller:TAG .
docker push REGISTRY/agent:TAG
docker push REGISTRY/controller:TAG
```

Use the Kubernetes agent image, rather than the unmodified base agent, for `AGENT_IMAGE` and the login Job. On ARM nodes change `TARGETARCH` and use that architecture's checksum; publish a multi-architecture image if jobs can run on both architectures.

## Device sign-in

`AUTH_MODE=codex` uses the dedicated writable `codex-auth` PVC in the worker namespace. To initialize or renew it, first stop all controller processes and ensure all agent Jobs have terminated. If Flux manages the Deployment, change or suspend reconciliation appropriately so it does not immediately restart the controllers. Copy `login-job.example.yaml` to a local temporary file, replace its image with the same trusted agent image, and apply it. Wait for the Job's Pod to start, then use `kubectl -n codex-loops-jobs exec -it <login-pod-name> -- codex -c 'cli_auth_credentials_store="file"' login --device-auth`. Complete the printed device flow yourself; do not put its code, credential file or contents in Git or chat. Delete the login Job **and wait for its Pod to terminate** before restarting the controllers, since the authentication PVC is shared and token refresh is serialized.

For API billing instead, set `AUTH_MODE=api` and create `codex-model-auth` in the worker namespace with key `CODEX_API_KEY`, securely and separately from GitHub credentials. Test Jobs receive neither this Secret nor the Codex authentication PVC. No mode silently falls back to the other.

## Trial and operation

1. After reviewing images, secrets and policies, deploy the overlay; inspect PVCs, the single controller Pod, controller logs and worker namespace quota. No ingress or NodePort is provided.
2. With auto-merge still off, add one small test issue with the required acceptance criteria and `agent:ready`. Check that a Job runs, a PR is created, and a separate reviewer submits a structured review. Exercise a failing check and an approval. Only enable auto-merge after independent review, CI and server-side protection are proven with a disposable repository.
3. Inspect a Job with `kubectl -n codex-loops-jobs get jobs,pods` while it runs. The controller retains logs and job output under its state volume; Jobs have a deadline and TTL. After a controller crash, the next run deletes and waits for its previous Jobs before another model job may use the shared login PVC. Failed cleanup prevents a new job.
4. To roll back, stop the Kubernetes controllers and wait for their Jobs to terminate before starting the Docker stack. Restore a verified SQLite backup when moving state; the Docker auth volume and Kubernetes auth PVC are separate, so authenticate explicitly on the chosen backend. Do not run both controllers against the same repository/state at once.

## Trust and limits

The controller can create Jobs and exec into Pods in the dedicated worker namespace. Kubernetes workload creation rights can indirectly access Secrets, PVCs and service accounts in that namespace; keep it isolated and never place GitHub credentials or unrelated workloads there. The `restricted` Pod Security admission label and resource quota must actually be enforced by the cluster. Worker Pods run without a service-account token, root privileges, added capabilities, host network, host paths or a Docker socket. They use separate temporary volumes; review source and baseline are mounted read-only. The controller stages data through `kubectl exec`, never mounts its Git metadata or SQLite into workers, and rejects unsafe or excessive returned archives.

The controller containers run as root with limited filesystem capabilities to perform existing workspace ownership operations. Jobs are ordinary Kubernetes containers sharing the host kernel, not a hardened hostile-code sandbox. Their outbound network is not restricted by this example; apply network controls supported by the actual CNI if needed for your environment. A model job can use its own Codex credentials and reach whatever the network allows. Source, logs, prompts and retained job directories may contain private data; protect and prune the state volume. Controller crashes can leave Jobs until their deadline/TTL if the API is unavailable.

Job startup has a separate `KUBE_START_TIMEOUT_SECONDS` (default 300) within the overall job timeout. Invalid image names, image-pull backoff and missing Secret configuration fail early without printing Kubernetes error details or credentials. Increase the startup deadline if pulling large images on a slow node. Successful implementation Jobs return tracked and non-ignored source paths; generated files ignored by Git are excluded. Archive size is capped by `KUBE_ARCHIVE_MAX_BYTES` (default 512 MiB, maximum 2 GiB). Cross-tree or absolute symlinks in returned work are rejected, and test and review Jobs do not return a source archive.

Live scheduling, image builds/pulls, storage, SOPS decryption, device login and GitHub integration must be tested on the actual cluster. Automated tests use a fake Kubernetes client and do not prove those integrations. This is a single-host-style controller using Kubernetes as an executor; it does not provide multi-controller failover.
