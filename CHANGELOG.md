# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

## 0.1.0 - 2026-11-07 (not tagged as a release yet)

### Added

- `scripts/demo_moto_scan.py`, a live-mode scan against a seeded moto account, and
  rendered example reports under `examples/reports/`.
- Cost Explorer CSV loader that detects both export orientations (services as rows
  or as columns), strips currency suffixes and total rows, and normalises months.
- Live collection with boto3: Cost Explorer `GetCostAndUsage`, EC2 instances with
  CloudWatch CPU, EBS volumes and snapshots (AMI-aware), Elastic IPs, ELBv2 target
  health, NAT gateway traffic, RDS connections and CPU, S3 lifecycle rules and size.
- Eleven checks with a monthly savings estimate and a confidence level: idle EC2,
  unattached EBS, old snapshots, unused Elastic IPs, load balancers without healthy
  targets, NAT gateway spend, gp2 to gp3, idle RDS, S3 buckets without lifecycle
  rules, Savings Plan coverage and month-over-month spend growth.
- Bundled, approximate price table (us-east-1 list prices with regional multipliers).
- Markdown, self-contained HTML and JSON report writers.
- `cost-sentinel` CLI with `scan`, `report`, `checks` and `comment` commands and a
  `--fail-above` savings gate.
- Composite GitHub Action under `action/` that runs a scan and keeps one up-to-date
  report comment on the pull request.
- CI on Ubuntu and Windows, multi-stage Dockerfile, architecture notes and ADRs 0001
  (bundled price table) and 0002 (pure checks over an inventory).
