# Open Code Review (Alibaba OCR)

PR auto-review via
[`alibaba/open-code-review`](https://github.com/alibaba/open-code-review)
GitHub Action. Same pattern NormanAI-CRMx will use: pinned action release,
repo secrets/vars for the LLM, and trusted base-branch custom rules.

## Workflow

| Item | Value |
|------|--------|
| Workflow | `.github/workflows/ocr-review.yml` |
| Action pin | `alibaba/open-code-review@fc0bf59…` (`v1.9.1`) |
| npm package | `ocr_version: 1.9.1` |
| Rules | `.opencodereview/rule.json` (loaded from the **base** checkout) |
| Triggers | `pull_request_target` (opened / synchronize / reopened); on-demand PR comment `/open-code-review` or `@open-code-review` (MEMBER / OWNER / COLLABORATOR only) |

Official example (this workflow tracks it):
`examples/github_actions/ocr-review.yml` in the OCR repo.

## Secrets and variables

Configure under **Settings → Secrets and variables → Actions**. Do not invent
or commit values.

| Name | Kind | Purpose |
|------|------|---------|
| `OCR_LLM_URL` | secret | LLM API endpoint |
| `OCR_LLM_AUTH_TOKEN` | secret | LLM auth token (action maps to `OCR_LLM_TOKEN`) |
| `OCR_LLM_MODEL` | variable | Model name |
| `OCR_LLM_USE_ANTHROPIC` | variable | `'true'` for Anthropic, `'false'` for OpenAI-compatible |

Until these are set, the workflow cannot complete a review run.

## Research-specific rules

OCR is asked to flag regressions against the research operating contract:

1. **Qualify ≠ score** — Research qualifies; CRMx scores Fit.
2. **No Notion SoR writes** — discovery/handoff only; no Notion mutation as SoR.
3. **Promote fail-closed to CRMx** — missing path/DB/evidence must fail, not skip.
4. **Unknown ≠ 0** — missing numeric facts stay blank, never coerced to `0`.
5. **Evidence sidecar with promote** — `ingest_csv` must receive `--evidence`.

See `docs/research-operating-contract.md` and `docs/crmx-handoff-migration.md`.

## Security note

`pull_request_target` exposes secrets for fork PRs. The reusable action reviews
the base→head diff without checking out PR code into an executable workspace.
Custom `rule` paths must stay on the trusted base branch (as here); do not point
`rule` at files from the PR head.
