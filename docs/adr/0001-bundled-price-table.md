# ADR 0001: Use a bundled price table instead of the AWS Pricing API

- Status: accepted
- Date: 2026-11-07

## Context

Every finding needs a dollar figure; without one the report cannot be ranked and
nobody can decide what to fix first. AWS offers two sources:

1. The **Price List Query API** (`pricing:GetProducts`). It is authoritative, but it
   is only served from a few regions, returns large nested JSON documents that need
   a filter per product family, adds tens of calls per scan and needs an extra IAM
   permission that read-only audit roles often lack.
2. **Cost Explorer / Cost and Usage Reports**, which give actual spend but not the
   unit price needed to price one idle volume or instance.

The scanner must also run offline in tests, finish in a few seconds on a pull
request, and work in accounts where the scanning role is deliberately narrow.

## Decision

Ship a small price table in `cost_sentinel/pricing.py`:

- us-east-1 on-demand list prices for the instance types, RDS classes, EBS volume
  types, snapshots, public IPv4, ELBv2, NAT gateways and S3 Standard / Standard-IA
  that the checks need;
- one multiplier per region for everything outside us-east-1;
- linear extrapolation inside an instance family for sizes that are not listed
  (AWS prices scale linearly by size within a family);
- a `Price.approximate` flag whenever a multiplier or extrapolation was used, which
  checks use to lower confidence;
- a `PRICE_TABLE_VERSION` that every report prints.

## Consequences

- Scans need no pricing permissions and make no extra API calls; tests can assert
  exact numbers.
- Estimates are approximate. Regional prices can be off by a few percent, Windows
  and commercial database licences are not modelled, and Savings Plans or Reserved
  Instances the account already owns are not subtracted. The README and every report
  say so.
- The table needs a periodic refresh. Prices for these services change rarely, so a
  quarterly review is enough.
- A future `--pricing api` option can implement the same `Price` interface on top of
  `pricing:GetProducts` without touching the checks.
