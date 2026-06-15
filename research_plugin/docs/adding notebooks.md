# Adding Notebook Support

This is an exploratory design note for making Python notebooks a first-class
part of experiment execution, especially in sandboxes.

## Motivation

Notebooks are useful when an experiment benefits from visible, incremental
work:

- data inspection and debugging
- literature-review or analysis notebooks
- plotting and exploratory metrics work
- quick model or preprocessing checks before turning logic into scripts
- user-visible progress while the agent is investigating

The goal is not to replace `report.md`. The notebook is an execution and
exploration surface; the report remains the executive submission artifact for
review.

## Product purpose

Notebook support should feel native to the existing Research Plugin product,
not like a new workflow. It should use the current experiment folder, sandbox
dashboard, sync, resource, report, and review model.

The main purpose is to improve experiment-agent performance. For Python-heavy
experiments, the agent should usually work in a notebook because each small
decision can become its own executable block: inspect the data, validate a
preprocessing assumption, train a small baseline, check a metric, plot an
intermediate result, or try a narrow hyperparameter change. This shortens the
feedback loop and lets the agent fix small local errors instead of repeatedly
rewriting and rerunning one large script.

The second purpose is user visibility. A notebook naturally divides logic into
readable cells, with outputs directly attached to the code that produced them.
This makes it easier for a user to follow the experiment as it develops, inspect
tables and plots, and understand why the agent made a decision without reading a
long monolithic Python file.

Treat notebooks as both an operating surface and a storytelling surface. When a
Python notebook is a good fit for the experiment, the agent should use one for
exploration, debugging, and incremental validation. Final submissions still need
durable evidence outside notebook state: compact metrics, selected figures or
tables, and `report.md`.

## Current fit

The existing sandbox model is already close to supporting this well:

- Sandboxes expose dashboard-like services through a `dashboards` map.
- MLflow and TensorBoard already use this path.
- Lambda Labs can expose local-only services through SSH tunnels.
- Modal can expose service ports through encrypted tunnels.
- The experiment folder is already mirrored back to the local repo.

That suggests Jupyter should be added as another sandbox dashboard instead of
as a separate transport model.

## Proposed sandbox shape

Add JupyterLab as a third dashboard:

```text
mlflow       -> 5000
tensorboard  -> 6006
jupyter      -> 8888
```

For Lambda Labs:

- install `jupyterlab`, `ipykernel`, `nbconvert`, and `nbclient`
- start JupyterLab bound to `127.0.0.1:8888`
- expose it through the existing SSH-forward dashboard mechanism

For Modal:

- install the same notebook packages in the sandbox image
- start JupyterLab bound to `0.0.0.0:8888`
- add port `8888` to the encrypted dashboard ports

The sandbox response should include the notebook URL in
`sandbox.request` / `sandbox.get`:

```json
{
  "dashboards": {
    "mlflow": "...",
    "tensorboard": "...",
    "jupyter": "..."
  }
}
```

## Notebook location

Use a conventional location inside the synced experiment folder:

```text
$RP_EXPERIMENT_DIR/notebooks/*.ipynb
```

Useful environment variables:

- `$RP_EXPERIMENT_DIR`: durable synced experiment files
- `$RP_DATASET_DIR`: large datasets, caches, checkpoints, and temporary bulk
- `$RP_TB_LOGDIR`: TensorBoard event output
- `$MLFLOW_TRACKING_URI`: MLflow tracking server

Set `RP_NOTEBOOK_DIR=$RP_EXPERIMENT_DIR/notebooks` in sandbox environments.

## Submission model

Notebook files can be registered as ordinary repo resources:

- `code` when the notebook is mostly exploratory or procedural
- `result` when the notebook itself is a produced analysis artifact

The notebook should not be the only review surface. The agent should still
submit:

- `report.md` as the concise reviewed outcome
- `results/*.json` or `results/*.csv` for compact metrics
- `figures/*.png` for plots referenced by the report
- optionally an exported notebook view (`.html`, `.md`, or `.pdf`) when that is
  the natural artifact, such as a literature review or detailed analysis

## Execution helper

Add a small helper command for deterministic notebook runs, for example:

```sh
rp-run-notebook notebooks/explore.ipynb
```

The helper can wrap:

```sh
jupyter nbconvert --execute --inplace notebooks/explore.ipynb
```

or an `nbclient` implementation. It should:

- execute from `$RP_EXPERIMENT_DIR`
- preserve output in the notebook
- fail with a nonzero exit code on cell errors
- leave stdout/stderr in the sandbox terminal transcript
- make it easy to rerun notebooks before associating result resources

## Agent guidance

Add brief skill guidance near the execution-environment section:

> Use notebooks when they improve exploration, debugging, or literate analysis.
> Keep them under `$RP_EXPERIMENT_DIR/notebooks`. A notebook may be submitted as
> `code` or `result`, but it does not replace `report.md`. For reviewed
> evidence, execute notebooks deterministically and save key plots/tables as
> separate files under `figures/` and `results/`. Avoid large embedded outputs;
> do not write secrets into notebook cells or outputs.

## Risks and guardrails

Notebooks can make sync and review worse if used carelessly:

- Large embedded outputs can bloat `.ipynb` files.
- Secrets can leak into cell source, outputs, or saved widget state.
- Interactive-only notebooks are hard to review if they cannot be rerun.
- Dashboard URLs should be treated as runtime access, not durable evidence.

Recommended defaults:

- save durable evidence outside notebook outputs
- clear bulky outputs before submission when they are not needed
- execute notebooks before registering them as results
- keep datasets and checkpoints outside the experiment folder
- keep selected final figures/tables under `figures/` and `results/`

## Open questions

- Should Jupyter start automatically for every sandbox, or only when requested?
- Should notebook auth use a generated token in the dashboard URL, or rely on
  the provider tunnel plus localhost forwarding?
- Should `.ipynb` files receive special UI rendering, or remain ordinary
  resources initially?
- Should the workflow lint warn when a report cites a notebook but the notebook
  has not been executed/exported?
- Should the helper also support parameterized runs via Papermill?
