# Sandbox compute providers

The sandbox module provisions one SSH-reachable VM per request through a
provider-neutral `SandboxBackend` port. One provider is configured with
`MERV_EXECUTION_BACKEND` (default `lambda_labs`); a fleet is
configured with `MERV_EXECUTION_BACKENDS` (comma-separated), which
wires every named backend behind one multiplexer:

- agents pick a provider per request via `sandbox.request(provider=...)`
  (omit for the default; `sandbox.options` tags every hardware option with the
  provider that serves it);
- sandbox ids are stored as `<provider>:<native_id>` so every later operation
  (liveness, terminate, transcript reads) is routed to the owning provider —
  pre-multiplexer rows keep their un-prefixed ids and route to the default;
- rows and the `sandbox_generations` spend ledger record the owning provider
  (empty = created before multi-provider support = the default backend).

Removing a provider from the config while its VMs still exist makes their ids
unroutable: operations on them fail loudly instead of guessing (a wrong
provider answering "not found" would strand a billing VM behind a terminated
row). Terminate a provider's sandboxes before dropping it from the list.

All VM providers share the same bootstrap: cloud-init authorizes the caller's
public key and the control plane's management key, installs the `rec.sh`
transcript wrapper + `merv_run`, and then installs the heavy ML toolchain in a
second phase. Secrets (HF_TOKEN) are pushed post-boot over the management SSH
channel, never embedded in provider user_data.

## Lambda Labs (`lambda_labs`)

- Env: `MERV_LAMBDA_API_KEY` (or `LAMBDA_LABS_API_KEY` /
  `LAMBDA_API_KEY`); optional `MERV_LAMBDA_REGION`,
  `MERV_LAMBDA_INSTANCE_TYPE`.
- Credentials: <https://cloud.lambda.ai> -> API keys -> Generate. Pay-as-you-go
  with a card on file.
- Quirks: fixed machine SKUs (`gpu_1x_a10`, ...); live capacity via the
  instance-types API; per-minute billing. Deep stock of A10/A100/H100.

## Thunder Compute (`thunder_compute`)

- Env: `MERV_THUNDER_API_KEY` (or `THUNDER_COMPUTE_API_KEY` /
  `TNR_API_TOKEN`).
- Quirks: virtualized GPUs behind a port-forwarded SSH endpoint; the bootstrap
  is pushed over SSH rather than user_data. Cheap A100 capacity; per-minute
  billing; prototyping-mode instances can be slow for sustained training.

## Hyperstack (`hyperstack`)

- Env: `MERV_HYPERSTACK_API_KEY` (or `HYPERSTACK_API_KEY`) and
  `MERV_HYPERSTACK_ENVIRONMENT`; optional
  `MERV_HYPERSTACK_IMAGE` (default
  `Ubuntu Server 24.04 LTS (Noble Numbat)`), `MERV_HYPERSTACK_FLAVOR`.
- Credentials: sign up at <https://console.hyperstack.cloud>, add credit
  (prepaid balance or card), then Settings -> API Keys -> Generate. Create an
  **environment** once in the console (it pins the region) and put its name in
  `MERV_HYPERSTACK_ENVIRONMENT`.
- Quirks: VMs are secure-by-default with ZERO inbound ports — the backend
  attaches an inline TCP-22 ingress rule at create, or SSH never answers.
  Flavors carry `stock_available`; prices come from the account pricebook.
  `SHUTOFF` VMs still bill (only delete stops charges). Per-minute billing.
  Login user is `ubuntu`.

## DigitalOcean GPU Droplets (`digitalocean`)

- Env: `MERV_DIGITALOCEAN_TOKEN` (or `DIGITALOCEAN_TOKEN` /
  `DIGITALOCEAN_ACCESS_TOKEN`); optional `MERV_DIGITALOCEAN_IMAGE`
  (default `gpu-h100x1-base`, the AI/ML-ready Ubuntu with NVIDIA drivers),
  `MERV_DIGITALOCEAN_REGION`.
- Credentials: <https://cloud.digitalocean.com> -> API -> Tokens -> Generate
  New Token (full access). GPU sizes stay HIDDEN until the account gets the
  one-time GPU unlock — request it in the console under Create -> GPU Droplets.
- Quirks: powered-off droplets still bill (destroy is the only stop); root SSH
  and public IPv4 are the default; user_data caps at 64 KiB; no A100 SKUs
  (H100/H200/L40S/RTX-Ada fleet). Per-hour billing (hourly cap = monthly rate).

## Verda, formerly DataCrunch (`verda`, alias `datacrunch`)

- Env: `MERV_VERDA_CLIENT_ID` + `MERV_VERDA_CLIENT_SECRET`
  (or `DATACRUNCH_CLIENT_ID`/`DATACRUNCH_CLIENT_SECRET`); optional
  `MERV_VERDA_IMAGE` (default `ubuntu-24.04`),
  `MERV_VERDA_LOCATION` (e.g. `FIN-01`).
- Credentials: <https://cloud.datacrunch.io> (redirects to the verda.com
  console as the rename lands) -> Keys -> REST API credentials -> Generate:
  an OAuth2 client id + secret pair. Prepaid balance or card.
- Quirks: OAuth2 client-credentials (the backend mints and refreshes tokens);
  SSH keys AND the bootstrap startup script are pre-registered account
  resources referenced by id; billing rounds UP to 10-minute increments;
  `offline` instances keep billing their OS volume. The API base is pinned to
  `api.datacrunch.io` while the verda.com host migration is in flight
  (`MERV_VERDA_API_BASE` overrides).

## Voltage Park (`voltage_park`)

- Env: `MERV_VOLTAGE_PARK_TOKEN` (or `VOLTAGE_PARK_TOKEN`).
- Credentials: <https://dashboard.voltagepark.com> -> account/developer
  settings -> API token (Bearer).
- Quirks: H100-SXM5-only on-demand fleet sold as instant-deploy PRESETS — the
  preset uuid is the `instance_type`; SSH public keys are passed raw per
  deploy; the bootstrap rides as structured cloud-init (b64 `write_files` +
  `runcmd`). `Stopped`/`StoppedDisassociated` VMs still hold storage.
  NEEDS LIVE SMOKE TEST: whether bare port 22 answers on the public IP — the
  backend assumes it does and automatically switches to a port forward
  mapping internal 22 when the VM reports one.

## TensorDock (`tensordock`)

- Env: `MERV_TENSORDOCK_TOKEN` (or `TENSORDOCK_TOKEN`); optional
  `MERV_TENSORDOCK_IMAGE` (default `ubuntu2404`).
- Credentials: <https://dashboard.tensordock.com> -> Developer Settings ->
  Generate API token (Bearer). Prepaid balance required (minimum $1 to
  deploy).
- Quirks: a marketplace of third-party hosts; machines are composed, so the
  catalog synthesizes `<count>x-<gpu>` shapes with default vCPU/RAM and the
  100 GB storage minimum. Only locations with `dedicated_ip_available` are
  offered — port-mapped hosts cannot serve direct SSH. Per-second billing
  against the prepaid balance; there is no billing API, so the provision-time
  quote is the recorded rate. Host quality varies by uptime tier.
