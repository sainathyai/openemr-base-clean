#!/usr/bin/env python3
"""Start / stop / status the Clinical Co-Pilot fleet.

The three tiers are tagged Project=clinical-copilot by Terraform. This toggles their
power together so you only pay/burn free-tier hours while actually demoing. Terraform
owns the *shape* of the fleet; this owns its *power*.

    python fleet.py status
    python fleet.py start
    python fleet.py stop
    python fleet.py start --region us-east-1

Needs boto3 and AWS credentials in the environment (aws configure, or a profile via
AWS_PROFILE). Read-only for `status`; `start`/`stop` need ec2:Start/StopInstances.
"""
from __future__ import annotations

import argparse
import os
import sys

import boto3

PROJECT = "clinical-copilot"
_ORDER = {"db": 0, "emr": 1, "agent": 2}  # start DB first; agent last


def _tag(inst: dict, key: str, default: str = "-") -> str:
    return next((t["Value"] for t in inst.get("Tags", []) if t["Key"] == key), default)


def _fleet(ec2) -> list[dict]:
    resp = ec2.describe_instances(
        Filters=[{"Name": "tag:Project", "Values": [PROJECT]}])
    out = [i for r in resp["Reservations"] for i in r["Instances"]
           if i["State"]["Name"] != "terminated"]
    out.sort(key=lambda i: _ORDER.get(_tag(i, "Role"), 9))
    return out


def _show(instances: list[dict]) -> None:
    if not instances:
        print(f"no instances tagged Project={PROJECT} found.")
        return
    print(f"{'ROLE':<7} {'NAME':<22} {'STATE':<12} {'PUBLIC':<16} {'PRIVATE':<16} ID")
    for i in instances:
        print(f"{_tag(i, 'Role'):<7} {_tag(i, 'Name'):<22} "
              f"{i['State']['Name']:<12} {i.get('PublicIpAddress', '-'):<16} "
              f"{i.get('PrivateIpAddress', '-'):<16} {i['InstanceId']}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Control the Clinical Co-Pilot fleet.")
    ap.add_argument("action", choices=["status", "start", "stop"])
    ap.add_argument("--region", default=os.getenv("AWS_REGION", "us-east-1"))
    args = ap.parse_args()

    ec2 = boto3.client("ec2", region_name=args.region)
    instances = _fleet(ec2)
    if not instances:
        print(f"no instances tagged Project={PROJECT} in {args.region}.")
        return 1

    ids = [i["InstanceId"] for i in instances]
    if args.action == "start":
        ec2.start_instances(InstanceIds=ids)
        print(f"starting {len(ids)} instance(s)...")
    elif args.action == "stop":
        ec2.stop_instances(InstanceIds=ids)
        print(f"stopping {len(ids)} instance(s)...")

    # re-describe so status reflects the requested transition
    _show(_fleet(ec2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
