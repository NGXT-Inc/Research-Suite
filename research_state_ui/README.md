# Research State UI

Browser frontend for Research Plugin. It gives researchers a live view of
projects, experiments, reviews, resources, sandboxes, and reflection waves while
agents work through the backend workflow.

## Run Locally

Start the `research_plugin` HTTP daemon first, usually on `127.0.0.1:8787`.
Then run the UI:

```bash
npm install
npm run dev
```

Vite serves the app on `http://127.0.0.1:5173` by default and proxies `/api`
and `/health` to the daemon.

## Backend Target

For local development, point the Vite proxy at a non-default daemon with:

```bash
RSUI_API=http://127.0.0.1:8788 npm run dev
```

For hosted or static builds, use:

```bash
VITE_API_BASE=https://your-control-plane.example.com npm run build
```

Hosted control planes that require auth can receive a build-time token through
`VITE_API_TOKEN`. During development, the same values can also be overridden in
browser local storage with `rsui:apiBase` and `rsui:apiToken`.

## Commands

```bash
npm run dev      # development server
npm run build    # production build into dist/
npm run preview  # preview the production build
```
