"""Command-line interface: ``cost-sentinel scan | report | checks | comment``.

Exit codes:

* ``0`` - the command succeeded.
* ``1`` - an input, AWS or GitHub error (the message is printed to stderr). A live
  scan that cannot collect any resource (for example because there are no valid
  credentials) is an error, never an empty report.
* ``2`` - invalid arguments: argparse rejected them, ``--checks`` names an unknown
  check, or none of the selected checks can run on the given input.
* ``3`` - ``--fail-above`` was given and the estimated monthly savings exceed it.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from botocore.exceptions import BotoCoreError, ClientError, ProfileNotFound

from cost_sentinel import __version__
from cost_sentinel.aws.session import AwsClients, build_session
from cost_sentinel.checks import ALL_CHECKS, check_ids, get_checks
from cost_sentinel.github import client_from_environment, pr_number_from_event
from cost_sentinel.models import CostSentinelError, ScanConfig, ScanResult
from cost_sentinel.report import render
from cost_sentinel.scanner import scan_csv, scan_live

logger = logging.getLogger("cost_sentinel")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_THRESHOLD = 3
FORMAT_CHOICES = ("markdown", "md", "html", "json")


class UsageError(CostSentinelError):
    """The arguments are valid on their own but make no sense together (exit code 2)."""


def _env(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise CostSentinelError(f"{name} must be an integer, got {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise CostSentinelError(f"{name} must be a number, got {raw!r}") from exc


def parse_checks(value: str) -> tuple[str, ...]:
    """``argparse`` type for ``--checks``: comma-separated, known check ids only."""
    ids = tuple(item.strip() for item in value.split(",") if item.strip())
    if not ids:
        raise argparse.ArgumentTypeError("expected at least one check id")
    known = check_ids()
    unknown = [check_id for check_id in ids if check_id not in known]
    if unknown:
        raise argparse.ArgumentTypeError(
            f"unknown check(s): {', '.join(unknown)}; available: {', '.join(known)}"
        )
    return ids


def _add_output_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--format",
        "-f",
        choices=FORMAT_CHOICES,
        default=None,
        help="report format (default: $COST_SENTINEL_FORMAT or markdown)",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=None,
        help="write the report to this file instead of stdout",
    )
    parser.add_argument(
        "--checks",
        type=parse_checks,
        default=None,
        help="comma-separated check ids to run (default: all; see 'cost-sentinel checks')",
    )
    parser.add_argument(
        "--min-savings",
        type=float,
        default=0.0,
        help="drop findings that save less than this many dollars per month",
    )
    parser.add_argument(
        "--fail-above",
        type=float,
        default=None,
        metavar="DOLLARS",
        help="exit with code 3 when total estimated monthly savings exceed this amount",
    )
    parser.add_argument(
        "--savings-plan-discount",
        type=float,
        default=0.20,
        help="assumed Compute Savings Plan discount, 0..1 (default: 0.20)",
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser (exposed for tests and documentation)."""
    parser = argparse.ArgumentParser(
        prog="cost-sentinel",
        description="Find AWS cost waste and estimate the monthly savings of fixing it.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--log-level",
        default=_env("COST_SENTINEL_LOG_LEVEL", "WARNING"),
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="logging verbosity on stderr (default: WARNING)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    scan = subparsers.add_parser(
        "scan",
        help="scan a live AWS account with boto3",
        description=(
            "Collect EC2, EBS, ELBv2, NAT, RDS, S3 and Cost Explorer data and report waste."
        ),
    )
    scan.add_argument("--profile", default=None, help="AWS profile (default: $AWS_PROFILE)")
    scan.add_argument("--region", default=None, help="AWS region (default: $AWS_REGION)")
    scan.add_argument(
        "--lookback-days",
        type=int,
        default=None,
        help="CloudWatch window in days (default: $COST_SENTINEL_LOOKBACK_DAYS or 14)",
    )
    scan.add_argument(
        "--cpu-threshold",
        type=float,
        default=None,
        help="average CPU %% below which an EC2 instance is idle (default: 5)",
    )
    scan.add_argument(
        "--snapshot-age-days",
        type=int,
        default=None,
        help="snapshots older than this are reported (default: 90)",
    )
    scan.add_argument(
        "--months",
        type=int,
        default=None,
        help="months of Cost Explorer history to fetch (default: 6)",
    )
    scan.add_argument(
        "--no-cost-explorer",
        action="store_true",
        help="skip GetCostAndUsage (each Cost Explorer API request is billed by AWS)",
    )
    scan.add_argument(
        "--strict",
        action="store_true",
        help="exit with code 1 if any service cannot be collected (default: skip it)",
    )
    _add_output_options(scan)
    scan.set_defaults(handler=_cmd_scan)

    report = subparsers.add_parser(
        "report",
        help="analyse a Cost Explorer CSV export (no AWS access needed)",
        description="Load a Cost Explorer CSV in either orientation and report on it.",
    )
    report.add_argument("--csv", type=Path, required=True, help="path to the CSV export")
    report.add_argument(
        "--region",
        default=None,
        help="region label for the report (default: global)",
    )
    _add_output_options(report)
    report.set_defaults(handler=_cmd_report)

    checks = subparsers.add_parser("checks", help="list the available checks")
    checks.set_defaults(handler=_cmd_checks)

    comment = subparsers.add_parser(
        "comment",
        help="post or update a Markdown report as a pull request comment",
    )
    comment.add_argument("--file", type=Path, required=True, help="Markdown report to post")
    comment.add_argument("--repo", default=None, help="owner/name (default: $GITHUB_REPOSITORY)")
    comment.add_argument(
        "--pr",
        type=int,
        default=None,
        help="pull request number (default: read from $GITHUB_EVENT_PATH)",
    )
    comment.add_argument("--token", default=None, help="GitHub token (default: $GITHUB_TOKEN)")
    comment.add_argument(
        "--api-url", default=None, help="GitHub API URL (default: $GITHUB_API_URL)"
    )
    comment.set_defaults(handler=_cmd_comment)
    return parser


def _pick[T](value: T | None, fallback: T) -> T:
    return fallback if value is None else value


def _base_config(args: argparse.Namespace, region: str) -> ScanConfig:
    return ScanConfig(
        region=region,
        lookback_days=_pick(
            getattr(args, "lookback_days", None), _env_int("COST_SENTINEL_LOOKBACK_DAYS", 14)
        ),
        months=_pick(getattr(args, "months", None), _env_int("COST_SENTINEL_MONTHS", 6)),
        ec2_cpu_threshold=_pick(
            getattr(args, "cpu_threshold", None), _env_float("COST_SENTINEL_CPU_THRESHOLD", 5.0)
        ),
        snapshot_max_age_days=_pick(
            getattr(args, "snapshot_age_days", None),
            _env_int("COST_SENTINEL_SNAPSHOT_AGE_DAYS", 90),
        ),
        savings_plan_discount=args.savings_plan_discount,
        min_savings=args.min_savings,
        checks=args.checks,
    )


def _emit(result: ScanResult, args: argparse.Namespace) -> int:
    fmt = args.format or _env("COST_SENTINEL_FORMAT", "markdown")
    text = render(result, fmt)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8", newline="\n")
        print(
            f"wrote {fmt} report to {args.output}: {len(result.findings)} finding(s), "
            f"${result.total_savings:,.2f}/month estimated savings",
            file=sys.stderr,
        )
    else:
        sys.stdout.write(text if text.endswith("\n") else text + "\n")
    if args.fail_above is not None and result.total_savings > args.fail_above:
        print(
            f"estimated savings ${result.total_savings:,.2f}/month exceed "
            f"--fail-above ${args.fail_above:,.2f}",
            file=sys.stderr,
        )
        return EXIT_THRESHOLD
    return EXIT_OK


def _cmd_scan(args: argparse.Namespace) -> int:
    profile = args.profile or os.environ.get("AWS_PROFILE") or None
    region = args.region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    session = build_session(profile, region)
    clients = AwsClients.from_session(session, region)
    config = _base_config(args, clients.region)
    result = scan_live(
        clients,
        config,
        profile=profile,
        include_cost_report=not args.no_cost_explorer,
        strict=args.strict,
    )
    if result.inventory.errors:
        print(
            f"warning: {len(result.inventory.errors)} service(s) could not be collected and "
            "are listed under 'Skipped' in the report; pass --strict to fail instead",
            file=sys.stderr,
        )
    return _emit(result, args)


def _cmd_report(args: argparse.Namespace) -> int:
    config = _base_config(args, args.region or "global")
    if config.checks is not None:
        selected = get_checks(config.checks)
        inventory_only = [check.id for check in selected if check.needs_inventory]
        if len(inventory_only) == len(selected):
            raise UsageError(
                f"{', '.join(inventory_only)} cannot run on a Cost Explorer export, which has "
                "no resource ids; use 'cost-sentinel scan', or select savings-plan or "
                "spend-growth"
            )
        if inventory_only:
            print(
                f"warning: {', '.join(inventory_only)} cannot run on a Cost Explorer export "
                "and will be skipped; use 'cost-sentinel scan' for resource-level checks",
                file=sys.stderr,
            )
    result = scan_csv(args.csv, config)
    return _emit(result, args)


def _cmd_checks(args: argparse.Namespace) -> int:
    width = max(len(check_id) for check_id in check_ids())
    for check in ALL_CHECKS:
        print(f"{check.id:<{width}}  {check.title} [{check.inputs}]")
    return EXIT_OK


def _cmd_comment(args: argparse.Namespace) -> int:
    try:
        markdown = args.file.read_text(encoding="utf-8")
    except OSError as exc:
        raise CostSentinelError(f"cannot read {args.file}: {exc}") from exc
    pr_number = args.pr or pr_number_from_event(os.environ.get("GITHUB_EVENT_PATH"))
    if pr_number is None:
        raise CostSentinelError(
            "no pull request number: pass --pr or run inside a pull_request workflow"
        )
    client = client_from_environment(args.token, args.repo, args.api_url)
    action, url = client.upsert_report_comment(pr_number, markdown)
    print(f"{action} comment {url}".rstrip())
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the ``cost-sentinel`` console script."""
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    handler: Callable[[argparse.Namespace], int] = args.handler
    try:
        return handler(args)
    except UsageError as exc:
        print(f"{parser.prog}: error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except ProfileNotFound as exc:
        print(f"error: {exc}", file=sys.stderr)
    except (ClientError, BotoCoreError) as exc:
        print(f"error: AWS call failed: {exc}", file=sys.stderr)
    except (CostSentinelError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
    return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
