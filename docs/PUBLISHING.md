# Publishing wombat-gate

0.8.0 is a local candidate. Publishing requires the maintainer's approval.

## Diagnosis (2026-11-04)

[Run 37317309028](https://github.com/liyifreddy/wombat-gate/actions/runs/37317309028), release v0.6.2, failed in the PyPI publishing step after build and tag validation succeeded. Job 111787193563 reports `invalid-publisher`: no publisher matched the GitHub OIDC claims. The repository claim is `liyifreddy/wombat-gate`, the workflow is `.github/workflows/publish.yml` and the environment is `pypi`. The previous v0.6.1 run 37315248729 was still waiting when inspected.

The workflow now builds and tests in a job without OIDC privileges, validates the release tag from wheel metadata, and hands artifacts to a dedicated `pypi` job. This does not create the PyPI-side publisher. That account setting must be configured separately.

## Maintainer setup (no token)

1. Sign in to **pypi.org** with the account that owns `wombat-gate`. Use the site's normal two-factor authentication flow. Do not copy credentials into this repository or an agent conversation.
2. If the project exists, open **Your projects → wombat-gate → Manage → Publishing → Add a new publisher → GitHub**. Direct page: <https://pypi.org/manage/project/wombat-gate/settings/publishing/>.
3. Fill in owner **liyifreddy**, repository **wombat-gate**, workflow filename **publish.yml** (filename only), environment **pypi** (exactly this spelling). Add the publisher. Confirm these values match the intended repository; do not copy unexpected claims blindly.
4. If the project has never been created, use <https://pypi.org/manage/account/publishing/> to add a **pending publisher** with project name **wombat-gate** and the same four GitHub values. A successful first publish creates the project. If someone else owns the name, stop and resolve ownership first.
5. In GitHub **liyifreddy/wombat-gate → Settings → Environments → pypi**, confirm the environment exists and review its deployment rules. If reviewers are required, approve the deployment when GitHub asks. The workflow's environment must match PyPI's field.
6. After review and approval to publish, merge the candidate and let the new test workflow pass. Verify CLI version, CHANGELOG, package metadata and release tag all say **0.8.0 / v0.8.0**. Publish a GitHub Release on that exact commit and tag; this triggers `publish.yml`.
7. Open the new Actions run. Confirm tests, build, metadata validation and upload all succeed; verify the uploaded version on the PyPI project page. An old run uses its old workflow: rerunning v0.6.2 will not test the new workflow or publish 0.8.0. Do not rerun historical uploads casually.

Official setup: [existing project](https://docs.pypi.org/trusted-publishers/adding-a-publisher/), [pending publisher](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/), [troubleshooting](https://docs.pypi.org/trusted-publishers/troubleshooting/).

## Validation before approval

```sh
python3 selftest_wombat_gate.py
python3 selftest_release.py
bash hooks/selftest_guard.sh
python3 -m build
python3 -m twine check dist/*
```

The new CI configuration covers the full original suite on Linux, the 0.8 regression suite and guard on Linux/macOS, Python 3.8/3.13 on Linux and 3.13 on macOS, plus wheel installation. The original diagnostic tests contain Linux `/proc` assumptions; macOS gets the portable regression suite. Adding configuration is not evidence that remote CI passed. Native Windows is unsupported; use WSL.
