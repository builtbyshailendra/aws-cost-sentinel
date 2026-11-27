# AWS Cost Sentinel report

Generated 2026-11-08 09:26 UTC | region `us-east-1` | source `live` (moto demo account, resources back-dated 120 days)

**Estimated monthly savings: $622.01** across 12 finding(s) (7 high, 3 medium, 2 low confidence).

## Summary by check

| Check | Findings | Est. monthly savings |
|---|---:|---:|
| Idle RDS instances (`idle-rds`) | 1 | $318.28 |
| Idle EC2 instances (`idle-ec2`) | 1 | $140.16 |
| NAT gateway spend (`nat-gateway`) | 1 | $60.75 |
| Unattached EBS volumes (`unattached-ebs`) | 1 | $40.00 |
| gp2 volumes convertible to gp3 (`gp2-to-gp3`) | 3 | $22.82 |
| Load balancers with no healthy targets (`idle-load-balancer`) | 1 | $16.43 |
| Savings Plan opportunity (`savings-plan`) | 1 | $12.41 |
| Old EBS snapshots (`old-snapshots`) | 1 | $5.00 |
| Unused Elastic IPs (`unused-eip`) | 1 | $3.65 |
| S3 buckets without lifecycle rules (`s3-lifecycle`) | 1 | $2.52 |

## Findings

### Idle RDS instances ($318.28/month)

| Resource | Region | Monthly cost | Savings | Confidence | Why | Recommendation |
|---|---|---:|---:|---|---|---|
| `orders-staging` | us-east-1 | $341.28 | $318.28 | high | db.r6g.large postgres averaged 0.00 connections and 1.8% CPU over the last 14 days | Stop the instance (RDS restarts it after 7 days, so schedule it) or take a final snapshot and delete it. |

### Idle EC2 instances ($140.16/month)

| Resource | Region | Monthly cost | Savings | Confidence | Why | Recommendation |
|---|---|---:|---:|---|---|---|
| `i-26fcedf277ef02421` | us-east-1 | $140.16 | $140.16 | high | m5.xlarge (legacy-reporting) averaged 1.3% CPU over the last 14 days (threshold 5%) | Stop the instance if nobody owns it, otherwise move it to a smaller type or schedule it to stop outside working hours. |

### NAT gateway spend ($60.75/month)

| Resource | Region | Monthly cost | Savings | Confidence | Why | Recommendation |
|---|---|---:|---:|---|---|---|
| `nat-123f4393b241a704d` | us-east-1 | $235.35 | $60.75 | low | data processing costs about $202.50 per month, more than the $32.85 hourly charge | Add gateway endpoints for S3 and DynamoDB (free) and interface endpoints for the AWS services that dominate the traffic, and keep cross-AZ traffic inside the AZ. |

### Unattached EBS volumes ($40.00/month)

| Resource | Region | Monthly cost | Savings | Confidence | Why | Recommendation |
|---|---|---:|---:|---|---|---|
| `vol-ee5c6fe1961784e12` | us-east-1 | $40.00 | $40.00 | high | 500 GiB gp3 has no attachment and was created 120 days ago | Take a snapshot if the data matters, then delete the volume. |

### gp2 volumes convertible to gp3 ($22.82/month)

| Resource | Region | Monthly cost | Savings | Confidence | Why | Recommendation |
|---|---|---:|---:|---|---|---|
| `vol-d187482cfff141e0f` | us-east-1 | $150.00 | $22.50 | medium | 1500 GiB gp2 costs $150.00/month; as gp3 with 4500 IOPS it costs $127.50/month | Run ModifyVolume to gp3 (online, no downtime) with 4500 provisioned IOPS to keep the gp2 baseline, and raise throughput above 125 MiB/s only if CloudWatch shows the volume needs it. |
| `vol-a3d0ed1619a7d9486` | us-east-1 | $0.80 | $0.16 | high | 8 GiB gp2 costs $0.80/month; as gp3 with 3000 IOPS it costs $0.64/month | Run ModifyVolume to gp3 (online, no downtime); the included 3000 IOPS and 125 MiB/s already exceed the gp2 baseline of 100 IOPS. |
| `vol-8b97509ab4eff5557` | us-east-1 | $0.80 | $0.16 | high | 8 GiB gp2 costs $0.80/month; as gp3 with 3000 IOPS it costs $0.64/month | Run ModifyVolume to gp3 (online, no downtime); the included 3000 IOPS and 125 MiB/s already exceed the gp2 baseline of 100 IOPS. |

### Load balancers with no healthy targets ($16.43/month)

| Resource | Region | Monthly cost | Savings | Confidence | Why | Recommendation |
|---|---|---:|---:|---|---|---|
| `old-campaign-alb` | us-east-1 | $16.43 | $16.43 | high | application load balancer has no registered targets across 1 target group(s) | Delete the load balancer if the service behind it is gone; otherwise fix the target group health checks. |

### Savings Plan opportunity ($12.41/month)

| Resource | Region | Monthly cost | Savings | Confidence | Why | Recommendation |
|---|---|---:|---:|---|---|---|
| `1 steady-state instances` | us-east-1 | $62.05 | $12.41 | medium | 1 on-demand instances have run for more than 30 days and cost about $62.05 per month; 1 idle instance(s) excluded (see idle-ec2) | Buy a 1-year, no-upfront Compute Savings Plan with an hourly commitment of about $0.07; it applies across instance families, sizes and regions, so the baseline can keep changing. |

### Old EBS snapshots ($5.00/month)

| Resource | Region | Monthly cost | Savings | Confidence | Why | Recommendation |
|---|---|---:|---:|---|---|---|
| `snap-691770066ce99488a` | us-east-1 | $10.00 | $5.00 | medium | snapshot of 200 GiB volume vol-9684b5cfe67d9faf3 is 120 days old and the source volume no longer exists | Delete it, or move it to the EBS Snapshots Archive tier (75% cheaper) if it must be kept for compliance. Use Data Lifecycle Manager to expire future snapshots automatically. |

### Unused Elastic IPs ($3.65/month)

| Resource | Region | Monthly cost | Savings | Confidence | Why | Recommendation |
|---|---|---:|---:|---|---|---|
| `eipalloc-f8ffb2d8dfaae6a7a` | us-east-1 | $3.65 | $3.65 | high | 127.64.75.85 is not associated with any resource | Release the address, or associate it with the resource that needs it. |

### S3 buckets without lifecycle rules ($2.52/month)

| Resource | Region | Monthly cost | Savings | Confidence | Why | Recommendation |
|---|---|---:|---:|---|---|---|
| `demo-app-logs` | us-east-1 | $18.40 | $2.52 | low | no lifecycle rules; 800.0 GB in Standard storage | Add lifecycle rules: abort incomplete multipart uploads after 7 days, transition objects to Standard-IA or Intelligent-Tiering after 30 days, and expire old noncurrent versions. |

## Resources scanned

2 instances, 4 volumes, 2 snapshots, 2 elastic ips, 1 load balancers, 1 nat gateways, 1 db instances, 1 buckets

## Notes

- Prices come from the bundled table (version 2025-06, us-east-1 list prices with a regional multiplier). Treat every figure as an estimate.
- CloudWatch lookback: 14 days; EC2 idle CPU threshold: 5%; snapshot max age: 90 days.
- The total counts each resource once: when several checks flag the same resource, only the largest saving is added, and the Savings Plan baseline leaves out idle instances.
- Checks not run: spend-growth (needs Cost Explorer data, which was not collected).
- Confidence reflects how likely the saving is real, not how precise the number is.
