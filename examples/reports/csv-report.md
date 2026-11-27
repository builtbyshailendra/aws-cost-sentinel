# AWS Cost Sentinel report

Generated 2026-11-08 09:25 UTC | region `global` | source `csv` (examples/cost_explorer_by_service.csv)

**Estimated monthly savings: $1,302.11** across 3 finding(s) (0 high, 1 medium, 2 low confidence).

## Summary by check

| Check | Findings | Est. monthly savings |
|---|---:|---:|
| Savings Plan opportunity (`savings-plan`) | 1 | $851.02 |
| Services with unexplained spend growth (`spend-growth`) | 2 | $451.09 |

## Findings

### Savings Plan opportunity ($851.02/month)

| Resource | Region | Monthly cost | Savings | Confidence | Why | Recommendation |
|---|---|---:|---:|---|---|---|
| `EC2-Instances` | global | $4,255.10 | $851.02 | medium | EC2-Instances spend never dropped below $4,255.10 per month over the last 3 months, which looks like steady on-demand usage | Check Cost Explorer's Savings Plans recommendations and commit to the baseline with a 1-year, no-upfront Compute Savings Plan. |

### Services with unexplained spend growth ($451.09/month)

| Resource | Region | Monthly cost | Savings | Confidence | Why | Recommendation |
|---|---|---:|---:|---|---|---|
| `EC2-Other` | global | $1,048.65 | $380.27 | low | 2026-10 cost $1,048.65 against a trailing average of $668.38 (+57%) | Open Cost Explorer for this service grouped by usage type and linked account to confirm the increase is intended; set an AWS Budgets alert at the trailing average. |
| `CloudWatch` | global | $168.92 | $70.82 | low | 2026-10 cost $168.92 against a trailing average of $98.10 (+72%) | Open Cost Explorer for this service grouped by usage type and linked account to confirm the increase is intended; set an AWS Budgets alert at the trailing average. |

## Spend by service (2026-05 to 2026-10)

| Service | 2026-10 | Avg of earlier months | Trend | Total |
|---|---:|---:|---:|---:|
| EC2-Instances | $4,318.90 | $4,249.40 | +2% | $25,565.90 |
| Relational Database Service | $1,862.30 | $1,841.37 | +1% | $11,069.15 |
| EC2-Other | $1,048.65 | $668.38 | +57% | $4,390.55 |
| S3 | $463.35 | $416.44 | +11% | $2,545.53 |
| Elastic Load Balancing | $172.45 | $169.33 | +2% | $1,019.09 |
| VPC | $125.54 | $121.17 | +4% | $731.40 |
| CloudWatch | $168.92 | $98.10 | +72% | $659.40 |
| Lambda | $45.61 | $43.06 | +6% | $260.93 |
| DynamoDB | $31.20 | $27.72 | +13% | $169.81 |
| Key Management Service | $12.00 | $12.00 | +0% | $72.00 |
| **All services** | **$8,257.43** | **$7,655.47** | | **$46,534.79** |

## Notes

- Prices come from the bundled table (version 2025-06, us-east-1 list prices with a regional multiplier). Treat every figure as an estimate.
- A Cost Explorer export has no resource ids, so only the savings-plan and spend-growth checks run; use 'cost-sentinel scan' for resource-level findings.
- The total counts each resource once: when several checks flag the same resource, only the largest saving is added, and the Savings Plan baseline leaves out idle instances.
- Confidence reflects how likely the saving is real, not how precise the number is.
