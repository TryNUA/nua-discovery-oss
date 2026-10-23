# setup-ui

The source of the local setup page that `uv run python -m nua setup` serves. It is a small Next.js app
built as a static export. The built files are committed in `nua/setup/static/` and shipped in the Python
package, so running `nua setup` needs no Node. You only need this folder to change the page.

The API it calls is `nua/setup/server.py` (FastAPI, 127.0.0.1 only, a per-run token on every call).

## Rebuild

Needs Node 20+ and pnpm. The build downloads the Geist fonts from Google Fonts once and bundles them.

```sh
cd setup-ui
pnpm install
pnpm build        # next build, then scripts/copy-out.mjs copies out/ to ../nua/setup/static/
```

Commit `nua/setup/static/` together with the source change. `node_modules/`, `.next/` and `out/` are
gitignored.

## Develop

Run the API against a scratch copy of the repo, not your real one: it writes `config/`, `.env` and
`terraform.tfvars`.

```sh
uv run python -m nua setup --no-browser --port 8765 --root /path/to/scratch-copy
```

Then rebuild (`pnpm build`) and reload `http://127.0.0.1:8765/?token=...`: the server reads the static
files on every request. `pnpm dev` serves the page alone on port 3000; its API calls fail there, since the
server refuses any host but its own.

## Layout

- `app/`: the root layout (fonts, theme) and the one page.
- `src/components/setup/`: the ten steps, the step frame and shared form pieces.
- `src/components/ui/`, `src/styles/`, `src/lib/cn.ts`, `src/components/theme-provider.tsx`: copied
  unchanged from the Nua design system (tokens, light and dark themes, Radix-based controls). Keep them
  in sync by copying, not by editing here.
- `src/lib/api.ts`: the typed client for `nua/setup/server.py`.

Dependency versions are pinned to the ones the Nua frontend uses.
