# ADR 0002: Checks are pure functions over a collected inventory

- Status: accepted
- Date: 2026-11-07

## Context

The first sketch had each check call boto3 directly. That made checks hard to test
(every test needed AWS mocks), caused duplicate API calls (three checks listed EBS
volumes) and left no way to support a CSV-only mode, because there was no shared
shape for "what we know about the account".

## Decision

Split collection from analysis:

- `aws.collectors.collect_inventory` makes every AWS call once and produces an
  `Inventory` of frozen dataclasses, recording per-service failures in
  `Inventory.errors`.
- `csv_loader` produces the same `Inventory` with only a `CostReport` attached.
- Every check implements `run(inventory, config) -> list[Finding]` and never touches
  the network. `needs_cost_report` marks checks that only make sense with cost data.

## Consequences

- A scan makes one pass over each API; adding a check usually needs no new calls.
- Checks can be unit-tested with hand-built inventories and integration-tested
  through moto with the same code.
- CSV mode reuses the scanner, report writers, CLI and GitHub Action unchanged.
- The inventory is held in memory. Accounts with hundreds of thousands of snapshots
  would need a streaming design; the README lists this as a limitation.
