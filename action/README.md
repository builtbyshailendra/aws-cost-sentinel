# AWS Cost Sentinel GitHub Action

A composite action that installs `aws-cost-sentinel` from this repository, runs a
scan (or analyses a Cost Explorer CSV export), writes the Markdown report to the job
summary, uploads Markdown/HTML/JSON reports as an artifact and posts the report as a
pull request comment. Re-runs edit the same comment instead of adding new ones.

## Live scan with an OIDC role

```yaml
name: Cost report
on:
  pull_request:
    paths: ["infra/**"]

permissions:
  contents: read
  pull-requests: write
  id-token: write   # for OIDC

jobs:
  cost-report:
    runs-on: ubuntu-latest
    steps:
      - uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: arn:aws:iam::123456789012:role/cost-sentinel-readonly
          aws-region: eu-west-1
      - uses: builtbyshailendra/aws-cost-sentinel/action@main
        with:
          mode: scan
          aws-region: eu-west-1
          min-savings: "5"
          fail-above: "500"
```

The role needs the read-only permissions listed in the main README
(`docs/ARCHITECTURE.md` has the full IAM policy).

## Cost Explorer CSV

```yaml
      - uses: actions/checkout@v5
      - uses: builtbyshailendra/aws-cost-sentinel/action@main
        with:
          mode: csv
          csv: finops/cost_explorer_by_service.csv
```

## Inputs

| Input | Default | Description |
|---|---|---|
| `mode` | `scan` | `scan` (boto3) or `csv` (Cost Explorer export). |
| `csv` | | CSV path, required in `csv` mode. |
| `aws-region` | `us-east-1` | Region to scan. Credentials must already be configured. |
| `checks` | all | Comma-separated check ids. |
| `min-savings` | `0` | Drop findings below this many dollars per month. |
| `lookback-days` | `14` | CloudWatch window. |
| `no-cost-explorer` | `false` | Skip `GetCostAndUsage` (billed per request by AWS). |
| `fail-above` | | Fail the job when estimated monthly savings exceed this amount. |
| `comment` | `true` | Post or update the PR comment on `pull_request` events. |
| `upload-artifact` | `true` | Upload the reports as the `cost-sentinel-report` artifact. |
| `github-token` | `github.token` | Needs `pull-requests: write`. |
| `python-version` | `3.12` | Python used to run the scanner. |

## Outputs

| Output | Description |
|---|---|
| `report-path` | Path of the Markdown report on the runner. |
| `exit-code` | `0` ok, `1` error, `3` savings above `fail-above`. |

Pull requests from forks receive a read-only `GITHUB_TOKEN`, so the comment step
fails there; guard the job with
`if: github.event.pull_request.head.repo.full_name == github.repository`.
