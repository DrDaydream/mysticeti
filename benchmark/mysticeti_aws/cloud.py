"""EC2 lifecycle for a separately tagged Mysticeti testbed.

AWS credentials come from Boto3's normal provider chain.  Every mutable resource
must carry both ownership tags; names alone never establish ownership.
"""

import base64
import binascii
from dataclasses import dataclass
from functools import wraps
import ipaddress
from itertools import zip_longest
from pathlib import Path
import subprocess
import time
from urllib.request import urlopen
from uuid import uuid4

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, WaiterError


def _aws_errors(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except (BotoCoreError, ClientError) as exc:
            raise RuntimeError(f"EC2 {method.__name__} failed ({', '.join(self.regions)}): {exc}") from exc
    return wrapped


@dataclass(frozen=True)
class Host:
    region: str
    instance_id: str
    ip: str


class _PublicIPv4Pending(RuntimeError):
    pass


class Cloud:
    LIVE_STATES = ["pending", "running", "stopping", "stopped", "shutting-down"]
    CANONICAL_OWNER = "099720109477"
    IMAGE_PREFIX = "ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-"

    def __init__(self, settings: dict, clients=None):
        self.settings = settings
        self.regions = list(settings["instances"]["regions"])
        self.tags = {"ManagedBy": "mysticeti-fabric", "Testbed": settings["testbed_id"]}
        self.group_name = f"mysticeti-{settings['testbed_id']}"
        self.clients = clients if clients is not None else {
            region: boto3.client(
                "ec2", region_name=region,
                config=Config(connect_timeout=10, read_timeout=30, retries={"max_attempts": 5, "mode": "standard"}),
            )
            for region in self.regions
        }

    def _owned(self, resource):
        tags = {tag["Key"]: tag["Value"] for tag in resource.get("Tags", [])}
        return all(tags.get(key) == value for key, value in self.tags.items())

    def _filters(self):
        return [{"Name": f"tag:{key}", "Values": [value]} for key, value in self.tags.items()]

    def _instances(self, region, states=None):
        states = self.LIVE_STATES if states is None else states
        pages = self.clients[region].get_paginator("describe_instances").paginate(
            Filters=self._filters() + [{"Name": "instance-state-name", "Values": states}]
        )
        rows = [
            item
            for page in pages
            for reservation in page.get("Reservations", [])
            for item in reservation.get("Instances", [])
            if self._owned(item) and item["State"]["Name"] in states
        ]
        return sorted(rows, key=lambda row: row["InstanceId"])

    @_aws_errors
    def hosts(self):
        """Return owned running hosts, round-robin in configured region order."""
        regional = []
        for region in self.regions:
            hosts = []
            for row in self._instances(region, ["running"]):
                if not row.get("PublicIpAddress"):
                    raise _PublicIPv4Pending(
                        f"{region}: instance {row['InstanceId']} has no public IPv4; "
                        "check its subnet and public-IP assignment before continuing"
                    )
                hosts.append(Host(region, row["InstanceId"], row["PublicIpAddress"]))
            regional.append(hosts)
        return [host for group in zip_longest(*regional) for host in group if host is not None]

    @_aws_errors
    def info(self):
        """Return owned, nonterminated instance inventory (including stopped nodes)."""
        return [
            {
                "region": region,
                "instance_id": row["InstanceId"],
                "state": row["State"]["Name"],
                "ip": row.get("PublicIpAddress"),
            }
            for region in self.regions
            for row in self._instances(region)
        ]

    def _security_group(self, region, vpc_id=None):
        # Query the name without a tag filter so a conflicting, unowned group
        # cannot be silently reused or hidden by the ownership filter.
        filters = [{"Name": "group-name", "Values": [self.group_name]}]
        if vpc_id:
            filters.append({"Name": "vpc-id", "Values": [vpc_id]})
        groups = [
            group for page in self.clients[region].get_paginator("describe_security_groups").paginate(Filters=filters)
            for group in page.get("SecurityGroups", [])
        ]
        if len(groups) > 1:
            raise RuntimeError(f"{region}: multiple security groups named {self.group_name}; resolve the ambiguity")
        if groups and not self._owned(groups[0]):
            raise RuntimeError(
                f"{region}: security group {self.group_name} has conflicting ownership tags; "
                "choose a different testbed_id or inspect the existing group"
            )
        return groups[0] if groups else None

    def _ssh_cidrs(self):
        cidrs = self.settings.get("ssh_cidrs", [])
        if cidrs:
            return sorted({str(ipaddress.IPv4Network(cidr, strict=False)) for cidr in cidrs})
        try:
            with urlopen("https://checkip.amazonaws.com", timeout=10) as response:
                address = ipaddress.IPv4Address(response.read(64).decode("ascii").strip())
            if not address.is_global:
                raise ValueError("the detected address is not public")
        except (OSError, ValueError, UnicodeError) as exc:
            raise RuntimeError(
                "Cannot detect the controller's public IPv4; configure ssh_cidrs explicitly"
            ) from exc
        return [f"{address}/32"]

    @staticmethod
    def _rule_entries(permissions):
        """Split permissions into individual sources, ignoring descriptions."""
        rules = {}
        for permission in permissions:
            common = {key: permission[key] for key in ("IpProtocol", "FromPort", "ToPort") if key in permission}
            for category, source_key in (
                ("IpRanges", "CidrIp"), ("Ipv6Ranges", "CidrIpv6"),
                ("UserIdGroupPairs", "GroupId"), ("PrefixListIds", "PrefixListId"),
            ):
                for source in permission.get(category, []):
                    identity = (common["IpProtocol"], common.get("FromPort"), common.get("ToPort"), category, source[source_key])
                    rules[identity] = {**common, category: [source]}
        return rules

    @staticmethod
    def _bundle_rules(rules):
        bundled = {}
        for permission in rules:
            common = {key: permission[key] for key in ("IpProtocol", "FromPort", "ToPort") if key in permission}
            identity = tuple(common.items())
            row = bundled.setdefault(identity, common.copy())
            for category in ("IpRanges", "Ipv6Ranges", "UserIdGroupPairs", "PrefixListIds"):
                row.setdefault(category, []).extend(permission.get(category, []))
        return [{key: value for key, value in row.items() if value != []} for row in bundled.values()]

    @_aws_errors
    def sync_network(self):
        """Reconcile owned group ingress: restricted SSH and current peers only."""
        return self._sync_network()

    def _sync_network(self, expected=None):
        hosts = self._ready_hosts(expected)
        peers = sorted({f"{ipaddress.IPv4Address(host.ip)}/32" for host in hosts})
        desired = [{"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22,
                    "IpRanges": [{"CidrIp": cidr} for cidr in self._ssh_cidrs()]}]
        if peers:
            desired.append({"IpProtocol": "tcp", "FromPort": 1500, "ToPort": 1627,
                            "IpRanges": [{"CidrIp": cidr} for cidr in peers]})
        wanted = self._rule_entries(desired)
        groups = {region: self._security_group(region) for region in self.regions}
        for region, group in groups.items():
            if group is None:
                continue
            existing = self._rule_entries(group.get("IpPermissions", []))
            remove = [rule for key, rule in existing.items() if key not in wanted]
            add = [rule for key, rule in wanted.items() if key not in existing]
            client = self.clients[region]
            if remove:
                client.revoke_security_group_ingress(GroupId=group["GroupId"], IpPermissions=self._bundle_rules(remove))
            if add:
                client.authorize_security_group_ingress(GroupId=group["GroupId"], IpPermissions=self._bundle_rules(add))
        return hosts

    def _ready_hosts(self, expected=None):
        # A running instance may briefly have no address in DescribeInstances.
        # Retry only this known transient condition, never arbitrary AWS errors.
        for attempt in range(12):
            try:
                hosts = self.hosts()
                required = {(region, identifier) for region, ids in (expected or {}).items() for identifier in ids}
                missing = required - {(host.region, host.instance_id) for host in hosts}
                if missing:
                    raise _PublicIPv4Pending(
                        "Owned running instances are not yet visible with public IPv4: "
                        + ", ".join(f"{region}/{identifier}" for region, identifier in sorted(missing))
                    )
                return hosts
            except _PublicIPv4Pending:
                if attempt == 11:
                    raise
                time.sleep(5)

    def _tag_list(self):
        return [{"Key": key, "Value": value} for key, value in self.tags.items()] + [
            {"Key": "Name", "Value": self.group_name}
        ]

    @staticmethod
    def _key_material(public_key):
        fields = public_key.split()
        if len(fields) < 2 or fields[0] not in ("ssh-rsa", "ssh-ed25519"):
            raise ValueError("Expected an RSA or Ed25519 OpenSSH public key")
        try:
            material = base64.b64decode(fields[1], validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("Invalid OpenSSH public-key encoding") from exc
        if not material:
            raise ValueError("Public-key material is empty")
        return fields[0], material

    def _local_public_key(self):
        path = Path(self.settings["key"]["path"]).expanduser()
        if not path.is_file():
            raise RuntimeError(f"Configured private key does not exist: {path}")
        try:
            result = subprocess.run(
                ["ssh-keygen", "-y", "-P", "", "-f", str(path)],
                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                check=True, timeout=30,
            )
            self._key_material(result.stdout)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            raise RuntimeError(
                f"Cannot derive the public key from {path}; install ssh-keygen and "
                "use a readable, unencrypted RSA or Ed25519 private key"
            ) from exc
        return " ".join(result.stdout.split()[:2])

    def _key_exists(self, region, public_key):
        name = self.settings["key"]["name"]
        try:
            keys = self.clients[region].describe_key_pairs(KeyNames=[name], IncludePublicKey=True)["KeyPairs"]
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "InvalidKeyPair.NotFound":
                return False
            raise
        if len(keys) != 1 or not keys[0].get("PublicKey"):
            raise RuntimeError(
                f"{region}: cannot verify existing key {name}; DescribeKeyPairs must return "
                "its public key (use a current boto3 and ec2:DescribeKeyPairs permission)"
            )
        try:
            matches = self._key_material(keys[0]["PublicKey"]) == self._key_material(public_key)
        except ValueError as exc:
            raise RuntimeError(f"{region}: existing key {name} has unreadable public material; no key was replaced") from exc
        if not matches:
            raise RuntimeError(
                f"{region}: existing key {name} does not match the configured private key; "
                "use its matching private key or choose a new key name; no key was replaced"
            )
        return True

    def _preflight(self, region, public_key, count):
        client = self.clients[region]
        images = [
            image for page in client.get_paginator("describe_images").paginate(
                Owners=[self.CANONICAL_OWNER], Filters=[
                    {"Name": "name", "Values": [self.IMAGE_PREFIX + "*"]},
                    {"Name": "architecture", "Values": ["x86_64"]},
                    {"Name": "virtualization-type", "Values": ["hvm"]},
                    {"Name": "root-device-type", "Values": ["ebs"]},
                    {"Name": "state", "Values": ["available"]},
                ],
            ) for image in page.get("Images", [])
            if image.get("OwnerId") == self.CANONICAL_OWNER
            and image.get("Architecture") == "x86_64"
            and image.get("State") == "available"
            and image.get("VirtualizationType") == "hvm"
            and image.get("RootDeviceType") == "ebs"
            and image.get("Name", "").startswith(self.IMAGE_PREFIX)
            and image.get("RootDeviceName") and image.get("CreationDate")
        ]
        if not images:
            raise RuntimeError(f"{region}: no verified Canonical Ubuntu 24.04 amd64 AMI is available")
        image = max(images, key=lambda item: (item["CreationDate"], item["ImageId"]))
        vpcs = [
            vpc for page in client.get_paginator("describe_vpcs").paginate(
                Filters=[{"Name": "is-default", "Values": ["true"]}]
            ) for vpc in page.get("Vpcs", [])
            if vpc.get("IsDefault") and vpc.get("State") == "available"
        ]
        if len(vpcs) != 1:
            raise RuntimeError(f"{region}: exactly one available default VPC is required; create or restore the default VPC first")
        vpc_id = vpcs[0]["VpcId"]
        subnets = [
            subnet for page in client.get_paginator("describe_subnets").paginate(Filters=[
                {"Name": "vpc-id", "Values": [vpc_id]},
                {"Name": "default-for-az", "Values": ["true"]},
                {"Name": "state", "Values": ["available"]},
            ]) for subnet in page.get("Subnets", [])
            if subnet.get("DefaultForAz") and subnet.get("State") == "available"
            and subnet.get("VpcId") == vpc_id and subnet.get("AvailableIpAddressCount", 0) >= count
        ]
        if not subnets:
            raise RuntimeError(f"{region}: no available default subnet has room for {count} instance(s); restore a default subnet or free addresses")
        subnet = min(subnets, key=lambda item: (item.get("AvailabilityZone", ""), item["SubnetId"]))
        key_exists = self._key_exists(region, public_key)
        group = self._security_group(region, vpc_id)
        return {"image": image, "vpc_id": vpc_id, "subnet_id": subnet["SubnetId"], "key_exists": key_exists, "group": group}

    @_aws_errors
    def create(self, nodes_per_region):
        """Add this many new instances in every region; existing nodes remain."""
        self._positive_count(nodes_per_region, "nodes_per_region")
        public_key = self._local_public_key()
        # Resolve controller access before creating anything, then preflight all
        # regions.  A bad later region cannot leave earlier instances running.
        self._ssh_cidrs()
        plans = {region: self._preflight(region, public_key, nodes_per_region) for region in self.regions}
        launched = {}
        try:
            for region, plan in plans.items():
                client = self.clients[region]
                if not plan["key_exists"]:
                    client.import_key_pair(
                        KeyName=self.settings["key"]["name"], PublicKeyMaterial=public_key.encode("ascii"),
                        TagSpecifications=[{"ResourceType": "key-pair", "Tags": self._tag_list()}],
                    )
                if plan["group"]:
                    group_id = plan["group"]["GroupId"]
                else:
                    group_id = client.create_security_group(
                        GroupName=self.group_name, Description=f"Dedicated Mysticeti testbed {self.settings['testbed_id']}",
                        VpcId=plan["vpc_id"],
                        TagSpecifications=[{"ResourceType": "security-group", "Tags": self._tag_list()}],
                    )["GroupId"]
                print(f"{region}: adding {nodes_per_region} owned instance(s) using {plan['image']['ImageId']}", flush=True)
                response = client.run_instances(
                    ImageId=plan["image"]["ImageId"], InstanceType=self.settings["instances"]["type"],
                    KeyName=self.settings["key"]["name"], MinCount=nodes_per_region, MaxCount=nodes_per_region,
                    ClientToken=str(uuid4()),
                    NetworkInterfaces=[{
                        "DeviceIndex": 0, "SubnetId": plan["subnet_id"], "Groups": [group_id],
                        "AssociatePublicIpAddress": True, "DeleteOnTermination": True,
                    }],
                    BlockDeviceMappings=[{
                        "DeviceName": plan["image"]["RootDeviceName"],
                        "Ebs": {"VolumeSize": self.settings["instances"]["volume_size"], "VolumeType": "gp3",
                                "DeleteOnTermination": True, "Encrypted": True},
                    }],
                    TagSpecifications=[{"ResourceType": resource, "Tags": self._tag_list()} for resource in ("instance", "volume")],
                    MetadataOptions={"HttpTokens": "required", "HttpEndpoint": "enabled"},
                )
                launched[region] = sorted(item["InstanceId"] for item in response["Instances"])
            for region, ids in launched.items():
                self._wait(region, "instance_running", ids)
            return self._sync_network(expected=launched)
        except (BotoCoreError, ClientError, RuntimeError) as exc:
            known = "; ".join(f"{region}: {', '.join(ids)}" for region, ids in launched.items()) or "none recorded"
            raise RuntimeError(
                f"Create did not complete. Instances already launched: {known}. "
                f"Inspect info before retrying: create is additive and does not roll back existing resources. {exc}"
            ) from exc

    @staticmethod
    def _positive_count(value, label):
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError(f"{label} must be a positive integer")

    def _wait(self, region, waiter_name, ids):
        if not ids:
            return
        print(f"{region}: waiting for {waiter_name} on {len(ids)} instance(s) (120 polls, 5 seconds apart)", flush=True)
        try:
            self.clients[region].get_waiter(waiter_name).wait(
                InstanceIds=ids, WaiterConfig={"Delay": 5, "MaxAttempts": 120}
            )
        except WaiterError as exc:
            raise RuntimeError(
                f"{region}: {waiter_name} did not complete for {', '.join(ids)}; "
                f"inspect EC2 state before retrying: {exc}"
            ) from exc

    @_aws_errors
    def start(self, max_per_region):
        """Start stopped nodes up to each region's total running/pending limit."""
        self._positive_count(max_per_region, "max_per_region")
        inventory = {region: self._instances(region) for region in self.regions}
        wait_for = {}
        for region, rows in inventory.items():
            busy = [row for row in rows if row["State"]["Name"] in ("running", "pending")]
            stopped = [row["InstanceId"] for row in rows if row["State"]["Name"] == "stopped"]
            to_start = stopped[:max(0, max_per_region - len(busy))]
            wait_for[region] = sorted(
                to_start + [row["InstanceId"] for row in busy if row["State"]["Name"] == "pending"]
            )
            if to_start:
                print(f"{region}: starting {len(to_start)} owned instance(s)", flush=True)
                self.clients[region].start_instances(InstanceIds=to_start)
        for region, ids in wait_for.items():
            self._wait(region, "instance_running", ids)
        return self._sync_network(expected=wait_for)

    @_aws_errors
    def stop(self):
        """Stop all owned active instances and remove their peer ingress."""
        inventory = {region: self._instances(region) for region in self.regions}
        wait_for = {}
        for region, rows in inventory.items():
            pending = [row["InstanceId"] for row in rows if row["State"]["Name"] == "pending"]
            self._wait(region, "instance_running", pending)
            to_stop = [row["InstanceId"] for row in rows if row["State"]["Name"] in ("pending", "running")]
            wait_for[region] = [row["InstanceId"] for row in rows if row["State"]["Name"] in ("pending", "running", "stopping")]
            if to_stop:
                print(f"{region}: stopping {len(to_stop)} owned instance(s)", flush=True)
                self.clients[region].stop_instances(InstanceIds=to_stop)
        for region, ids in wait_for.items():
            self._wait(region, "instance_stopped", ids)
        self.sync_network()
        return sum(len(ids) for ids in wait_for.values())

    @_aws_errors
    def destroy(self):
        """Terminate owned instances, then remove owned security groups.

        Existing key pairs are deliberately retained: they may be reused by
        other workloads and are not required to be owned by this adapter.
        """
        inventory = {region: self._instances(region) for region in self.regions}
        groups = {region: self._security_group(region) for region in self.regions}
        for region, rows in inventory.items():
            ids = [row["InstanceId"] for row in rows if row["State"]["Name"] != "shutting-down"]
            if ids:
                print(f"{region}: terminating {len(ids)} owned instance(s)", flush=True)
                self.clients[region].terminate_instances(InstanceIds=ids)
        for region, rows in inventory.items():
            self._wait(region, "instance_terminated", [row["InstanceId"] for row in rows])
            if groups[region]:
                self._delete_group(region, groups[region]["GroupId"])
        return sum(len(rows) for rows in inventory.values())

    def _delete_group(self, region, group_id):
        # EC2 can report terminated before deleting an instance's attached ENI.
        for attempt in range(12):
            try:
                self.clients[region].delete_security_group(GroupId=group_id)
                return
            except ClientError as exc:
                code = exc.response["Error"]["Code"]
                if code == "InvalidGroup.NotFound":
                    return
                if code != "DependencyViolation" or attempt == 11:
                    raise
                time.sleep(5)
