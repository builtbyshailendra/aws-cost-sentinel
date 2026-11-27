"""Bundled, approximate AWS price table.

The numbers are public on-demand list prices for ``us-east-1`` (Linux, single-AZ)
rounded to what AWS published in 2025.  They exist so that the scanner can turn a
finding into a dollar estimate without calling the Pricing API, which is slow,
needs extra IAM permissions and is not available in every partition.

Every estimate produced from this module is therefore approximate: regional
differences are modelled with a coarse multiplier, the Windows licence is added per
vCPU (other commercial licences such as RHEL, SUSE or SQL Server are not modelled),
and unknown instance sizes are extrapolated from the family's known ``large`` price.
``Price.approximate`` records when extrapolation happened so that checks can lower
their confidence accordingly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from cost_sentinel.models import HOURS_PER_MONTH

logger = logging.getLogger(__name__)

PRICE_TABLE_VERSION = "2025-06"
"""Month the bundled prices were last reconciled with the public price pages."""


@dataclass(frozen=True)
class Price:
    """A price plus a flag telling whether it was extrapolated rather than looked up."""

    amount: float
    approximate: bool = False
    note: str = ""


# ---------------------------------------------------------------------------
# Regions
# ---------------------------------------------------------------------------

REGION_FACTORS: dict[str, float] = {
    "us-east-1": 1.00,
    "us-east-2": 1.00,
    "us-west-1": 1.17,
    "us-west-2": 1.00,
    "ca-central-1": 1.08,
    "eu-west-1": 1.07,
    "eu-west-2": 1.12,
    "eu-west-3": 1.12,
    "eu-central-1": 1.15,
    "eu-north-1": 1.02,
    "eu-south-1": 1.12,
    "ap-south-1": 1.03,
    "ap-southeast-1": 1.17,
    "ap-southeast-2": 1.20,
    "ap-northeast-1": 1.25,
    "ap-northeast-2": 1.12,
    "ap-east-1": 1.35,
    "sa-east-1": 1.55,
    "me-south-1": 1.20,
    "af-south-1": 1.28,
}
DEFAULT_REGION_FACTOR = 1.10
"""Used for regions missing from the table (a mild premium over us-east-1)."""


def region_factor(region: str | None) -> float:
    """Multiplier applied to us-east-1 prices for ``region``."""
    if not region or region == "global":
        return 1.0
    return REGION_FACTORS.get(region, DEFAULT_REGION_FACTOR)


# ---------------------------------------------------------------------------
# EC2 on-demand, Linux, per hour (us-east-1)
# ---------------------------------------------------------------------------

EC2_HOURLY: dict[str, float] = {
    "t2.nano": 0.0058,
    "t2.micro": 0.0116,
    "t2.small": 0.023,
    "t2.medium": 0.0464,
    "t2.large": 0.0928,
    "t2.xlarge": 0.1856,
    "t3.nano": 0.0052,
    "t3.micro": 0.0104,
    "t3.small": 0.0208,
    "t3.medium": 0.0416,
    "t3.large": 0.0832,
    "t3.xlarge": 0.1664,
    "t3.2xlarge": 0.3328,
    "t3a.medium": 0.0376,
    "t3a.large": 0.0752,
    "t4g.nano": 0.0042,
    "t4g.micro": 0.0084,
    "t4g.small": 0.0168,
    "t4g.medium": 0.0336,
    "t4g.large": 0.0672,
    "m5.large": 0.096,
    "m5.xlarge": 0.192,
    "m5.2xlarge": 0.384,
    "m5.4xlarge": 0.768,
    "m5a.large": 0.086,
    "m6i.large": 0.096,
    "m6i.xlarge": 0.192,
    "m6i.2xlarge": 0.384,
    "m6g.large": 0.077,
    "m6g.xlarge": 0.154,
    "m7i.large": 0.1008,
    "m7i.xlarge": 0.2016,
    "m7g.large": 0.0816,
    "c5.large": 0.085,
    "c5.xlarge": 0.17,
    "c5.2xlarge": 0.34,
    "c5.4xlarge": 0.68,
    "c6i.large": 0.085,
    "c6i.xlarge": 0.17,
    "c6g.large": 0.068,
    "c7i.large": 0.08925,
    "c7g.large": 0.0725,
    "r5.large": 0.126,
    "r5.xlarge": 0.252,
    "r5.2xlarge": 0.504,
    "r6i.large": 0.126,
    "r6i.xlarge": 0.252,
    "r6g.large": 0.1008,
    "r7g.large": 0.1071,
    "x2idn.16xlarge": 6.669,
    "i3.large": 0.156,
    "i3.xlarge": 0.312,
    "g4dn.xlarge": 0.526,
    "g5.xlarge": 1.006,
    "p3.2xlarge": 3.06,
}

SIZE_UNITS: dict[str, float] = {
    "nano": 0.25,
    "micro": 0.5,
    "small": 1.0,
    "medium": 2.0,
    "large": 4.0,
    "xlarge": 8.0,
    "2xlarge": 16.0,
    "3xlarge": 24.0,
    "4xlarge": 32.0,
    "6xlarge": 48.0,
    "8xlarge": 64.0,
    "9xlarge": 72.0,
    "12xlarge": 96.0,
    "16xlarge": 128.0,
    "18xlarge": 144.0,
    "24xlarge": 192.0,
    "32xlarge": 256.0,
    "48xlarge": 384.0,
    "metal": 192.0,
}
"""Relative size units: AWS prices scale linearly within a family."""


def _split_type(instance_type: str) -> tuple[str, str] | None:
    family, separator, size = instance_type.partition(".")
    if not separator or not family or not size:
        return None
    return family, size


def _extrapolate(table: dict[str, float], family: str, size: str) -> float | None:
    """Scale a known price of ``family`` to ``size`` using ``SIZE_UNITS``."""
    units = SIZE_UNITS.get(size)
    if units is None:
        return None
    for known_type, known_price in table.items():
        known = _split_type(known_type)
        if known is None or known[0] != family:
            continue
        known_units = SIZE_UNITS.get(known[1])
        if known_units:
            return known_price * units / known_units
    return None


WINDOWS_LICENCE_PER_VCPU_HOUR = 0.046
"""Licence-included Windows Server premium per vCPU-hour (e.g. m5.xlarge $0.376 vs $0.192)."""
WINDOWS_BURSTABLE_LICENCE_PER_VCPU_HOUR = 0.0046
"""The premium is ten times lower on burstable families (t3.micro $0.0196 vs $0.0104)."""
BURSTABLE_FAMILIES = frozenset({"t2", "t3", "t3a", "t4g"})


def platform_kind(platform: str | None) -> str:
    """Classify an EC2 ``PlatformDetails`` value for pricing.

    Returns ``linux`` (no licence premium, including BYOL images), ``windows``
    (licence modelled), ``windows-sql`` (Windows modelled, SQL Server not) or
    ``other-licensed`` (RHEL, SUSE, Ubuntu Pro and similar: premium not modelled).
    """
    value = (platform or "").strip().lower()
    if value in {"", "linux", "linux/unix", "unix"} or "byol" in value:
        return "linux"
    if value.startswith("windows"):
        return "windows-sql" if "sql server" in value else "windows"
    return "other-licensed"


def ec2_vcpus(instance_type: str) -> int | None:
    """vCPU count derived from the size name (``large`` = 2, ``xlarge`` = 4...).

    Sizes below ``large`` are counted as 2 vCPUs, which matches the current burstable
    families; the older t2 nano, micro and small sizes have 1.
    """
    parts = _split_type(instance_type)
    if parts is None:
        return None
    units = SIZE_UNITS.get(parts[1])
    if units is None:
        return None
    return max(2, int(units / 2))


def ec2_licence_hourly(
    instance_type: str, platform: str | None, region: str | None = None
) -> float:
    """Hourly licence premium on top of the Linux price (0 when not modelled)."""
    if platform_kind(platform) not in {"windows", "windows-sql"}:
        return 0.0
    vcpus = ec2_vcpus(instance_type)
    if vcpus is None:
        return 0.0
    family = instance_type.partition(".")[0]
    rate = (
        WINDOWS_BURSTABLE_LICENCE_PER_VCPU_HOUR
        if family in BURSTABLE_FAMILIES
        else WINDOWS_LICENCE_PER_VCPU_HOUR
    )
    return vcpus * rate * region_factor(region)


def ec2_hourly(
    instance_type: str, region: str | None = None, platform: str | None = None
) -> Price | None:
    """On-demand hourly price for an EC2 instance type, or None when unknown.

    ``platform`` is the instance's ``PlatformDetails``. Windows adds a per-vCPU
    licence premium; other licensed platforms are priced as Linux and flagged as
    approximate, because their premium is not in the bundled table.
    """
    base = _ec2_linux_hourly(instance_type, region)
    if base is None:
        return None
    kind = platform_kind(platform)
    if kind == "linux":
        return base
    notes = [base.note] if base.note else []
    licence = ec2_licence_hourly(instance_type, platform, region)
    if kind in {"windows", "windows-sql"}:
        notes.append(f"Windows licence at ${WINDOWS_LICENCE_PER_VCPU_HOUR}/vCPU-hour")
    if kind == "windows-sql":
        notes.append("SQL Server licence not included")
    if kind == "other-licensed":
        notes.append(f"{platform} licence not included")
    return Price(base.amount + licence, approximate=True, note="; ".join(notes))


def _ec2_linux_hourly(instance_type: str, region: str | None) -> Price | None:
    factor = region_factor(region)
    exact = EC2_HOURLY.get(instance_type)
    if exact is not None:
        return Price(exact * factor, approximate=region_factor(region) != 1.0)
    parts = _split_type(instance_type)
    if parts is None:
        logger.debug("cannot parse instance type %s", instance_type)
        return None
    extrapolated = _extrapolate(EC2_HOURLY, *parts)
    if extrapolated is None:
        logger.debug("no bundled price for %s", instance_type)
        return None
    return Price(
        extrapolated * factor,
        approximate=True,
        note=f"extrapolated from the {parts[0]} family",
    )


# ---------------------------------------------------------------------------
# RDS on-demand, single-AZ, open-source engines, per hour (us-east-1)
# ---------------------------------------------------------------------------

RDS_HOURLY: dict[str, float] = {
    "db.t3.micro": 0.017,
    "db.t3.small": 0.034,
    "db.t3.medium": 0.068,
    "db.t3.large": 0.136,
    "db.t3.xlarge": 0.272,
    "db.t4g.micro": 0.016,
    "db.t4g.small": 0.032,
    "db.t4g.medium": 0.065,
    "db.t4g.large": 0.129,
    "db.m5.large": 0.171,
    "db.m5.xlarge": 0.342,
    "db.m5.2xlarge": 0.684,
    "db.m6g.large": 0.152,
    "db.m6g.xlarge": 0.304,
    "db.m6i.large": 0.171,
    "db.m6i.xlarge": 0.342,
    "db.m7g.large": 0.168,
    "db.r5.large": 0.24,
    "db.r5.xlarge": 0.48,
    "db.r6g.large": 0.218,
    "db.r6g.xlarge": 0.436,
    "db.r6i.large": 0.24,
    "db.r7g.large": 0.239,
}

RDS_STORAGE_PER_GB_MONTH: dict[str, float] = {
    "gp2": 0.115,
    "gp3": 0.115,
    "io1": 0.125,
    "io2": 0.125,
    "standard": 0.10,
    "aurora": 0.10,
}


def rds_hourly(instance_class: str, multi_az: bool, region: str | None = None) -> Price | None:
    """On-demand hourly price for an RDS instance class (Multi-AZ doubles it)."""
    factor = region_factor(region)
    multiplier = 2.0 if multi_az else 1.0
    exact = RDS_HOURLY.get(instance_class)
    if exact is not None:
        return Price(exact * multiplier * factor, approximate=factor != 1.0)
    stripped = instance_class.removeprefix("db.")
    parts = _split_type(stripped)
    if parts is None:
        return None
    family, size = parts
    extrapolated = _extrapolate(
        {key.removeprefix("db."): value for key, value in RDS_HOURLY.items()}, family, size
    )
    if extrapolated is None:
        logger.debug("no bundled price for %s", instance_class)
        return None
    return Price(
        extrapolated * multiplier * factor,
        approximate=True,
        note=f"extrapolated from the db.{family} family",
    )


def rds_storage_monthly(storage_type: str, size_gib: int, region: str | None = None) -> Price:
    """Monthly storage price for an RDS instance."""
    per_gb = RDS_STORAGE_PER_GB_MONTH.get(storage_type)
    approximate = per_gb is None
    if per_gb is None:
        per_gb = RDS_STORAGE_PER_GB_MONTH["gp3"]
    return Price(per_gb * size_gib * region_factor(region), approximate=approximate)


# ---------------------------------------------------------------------------
# EBS, snapshots, Elastic IPs, load balancers, NAT, S3
# ---------------------------------------------------------------------------

EBS_PER_GB_MONTH: dict[str, float] = {
    "gp2": 0.10,
    "gp3": 0.08,
    "io1": 0.125,
    "io2": 0.125,
    "st1": 0.045,
    "sc1": 0.015,
    "standard": 0.05,
}
GP3_BASELINE_IOPS = 3000
GP3_BASELINE_THROUGHPUT_MBPS = 125
GP3_EXTRA_IOPS_PRICE = 0.005
"""Per provisioned IOPS-month above the gp3 baseline."""
GP3_EXTRA_THROUGHPUT_PRICE = 0.04
"""Per MB/s-month above the gp3 baseline."""
IO1_IOPS_PRICE = 0.065
IO2_IOPS_PRICE = 0.065
GP2_IOPS_PER_GIB = 3
GP2_MIN_IOPS = 100
GP2_MAX_IOPS = 16000

SNAPSHOT_PER_GB_MONTH = 0.05
ELASTIC_IP_HOURLY = 0.005
"""Public IPv4 charge; AWS bills every public IPv4 address at this rate since 2024."""

ELB_HOURLY: dict[str, float] = {
    "application": 0.0225,
    "network": 0.0225,
    "gateway": 0.0125,
}
NAT_GATEWAY_HOURLY = 0.045
NAT_GATEWAY_PER_GB = 0.045
S3_STANDARD_PER_GB_MONTH = 0.023
S3_INFREQUENT_ACCESS_PER_GB_MONTH = 0.0125


def ebs_monthly(
    volume_type: str,
    size_gib: int,
    iops: int | None = None,
    throughput_mbps: int | None = None,
    region: str | None = None,
) -> Price:
    """Monthly price for an EBS volume including provisioned IOPS and throughput."""
    factor = region_factor(region)
    per_gb = EBS_PER_GB_MONTH.get(volume_type)
    approximate = per_gb is None
    if per_gb is None:
        per_gb = EBS_PER_GB_MONTH["gp3"]
    total = per_gb * size_gib
    if volume_type == "gp3":
        if iops and iops > GP3_BASELINE_IOPS:
            total += (iops - GP3_BASELINE_IOPS) * GP3_EXTRA_IOPS_PRICE
        if throughput_mbps and throughput_mbps > GP3_BASELINE_THROUGHPUT_MBPS:
            total += (throughput_mbps - GP3_BASELINE_THROUGHPUT_MBPS) * GP3_EXTRA_THROUGHPUT_PRICE
    elif volume_type == "io1" and iops:
        total += iops * IO1_IOPS_PRICE
    elif volume_type == "io2" and iops:
        total += iops * IO2_IOPS_PRICE
    return Price(total * factor, approximate=approximate or factor != 1.0)


def gp2_baseline_iops(size_gib: int) -> int:
    """IOPS a gp2 volume of ``size_gib`` gets from its size (3 IOPS/GiB, 100 to 16000)."""
    return max(GP2_MIN_IOPS, min(GP2_MAX_IOPS, size_gib * GP2_IOPS_PER_GIB))


def gp3_equivalent_monthly(size_gib: int, region: str | None = None) -> Price:
    """What a gp2 volume would cost as gp3 while keeping the same IOPS baseline."""
    needed_iops = gp2_baseline_iops(size_gib)
    extra_iops = max(0, needed_iops - GP3_BASELINE_IOPS)
    return ebs_monthly("gp3", size_gib, iops=GP3_BASELINE_IOPS + extra_iops, region=region)


def snapshot_monthly(size_gib: float, region: str | None = None) -> Price:
    """Monthly price of storing ``size_gib`` of snapshot data."""
    return Price(SNAPSHOT_PER_GB_MONTH * size_gib * region_factor(region), approximate=True)


def elastic_ip_monthly(region: str | None = None) -> Price:
    """Monthly price of one public IPv4 address."""
    return Price(ELASTIC_IP_HOURLY * HOURS_PER_MONTH * region_factor(region))


def elb_monthly(lb_type: str, region: str | None = None) -> Price:
    """Monthly fixed hourly price of a load balancer (capacity units excluded)."""
    hourly = ELB_HOURLY.get(lb_type.lower())
    approximate = hourly is None
    if hourly is None:
        hourly = ELB_HOURLY["application"]
    return Price(
        hourly * HOURS_PER_MONTH * region_factor(region),
        approximate=approximate,
        note="capacity-unit charges excluded",
    )


def nat_gateway_monthly(region: str | None = None) -> Price:
    """Monthly hourly charge of a NAT gateway (data processing excluded)."""
    return Price(NAT_GATEWAY_HOURLY * HOURS_PER_MONTH * region_factor(region))


def nat_gateway_processing(gigabytes: float, region: str | None = None) -> Price:
    """Data-processing charge for ``gigabytes`` through a NAT gateway."""
    return Price(NAT_GATEWAY_PER_GB * gigabytes * region_factor(region))


def s3_standard_monthly(size_gb: float, region: str | None = None) -> Price:
    """Monthly cost of ``size_gb`` in S3 Standard."""
    return Price(S3_STANDARD_PER_GB_MONTH * size_gb * region_factor(region), approximate=True)


def s3_tiering_saving_per_gb(region: str | None = None) -> float:
    """Saving per GB-month when data moves from Standard to Standard-IA."""
    return (S3_STANDARD_PER_GB_MONTH - S3_INFREQUENT_ACCESS_PER_GB_MONTH) * region_factor(region)


def monthly(hourly_price: float) -> float:
    """Convert an hourly price to a monthly one."""
    return hourly_price * HOURS_PER_MONTH
