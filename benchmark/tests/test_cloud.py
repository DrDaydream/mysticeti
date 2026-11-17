"""AWS contract tests: all network calls are intercepted by botocore Stubber."""

import base64
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch, MagicMock

import boto3
from botocore.stub import ANY, Stubber

from mysticeti_aws.cloud import Cloud


OWNER_TAGS = [
    {"Key": "ManagedBy", "Value": "mysticeti-fabric"},
    {"Key": "Testbed", "Value": "test-cloud"},
]
OWN_FILTERS = [
    {"Name": "tag:ManagedBy", "Values": ["mysticeti-fabric"]},
    {"Name": "tag:Testbed", "Values": ["test-cloud"]},
]
LIVE_STATES = ["pending", "running", "stopping", "stopped", "shutting-down"]
GROUP_FILTERS = [{"Name": "group-name", "Values": ["mysticeti-test-cloud"]}]
PUBLIC_KEY = "ssh-ed25519 " + base64.b64encode(b"matching public key material").decode()
IMAGE_FILTERS = [
    {"Name": "name", "Values": ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"]},
    {"Name": "architecture", "Values": ["x86_64"]},
    {"Name": "virtualization-type", "Values": ["hvm"]},
    {"Name": "root-device-type", "Values": ["ebs"]},
    {"Name": "state", "Values": ["available"]},
]


def ami(identifier="ami-new", date="2026-11-01T00:00:00.000Z", owner="099720109477", architecture="x86_64"):
    return {
        "ImageId": identifier, "CreationDate": date, "OwnerId": owner,
        "Architecture": architecture, "State": "available", "RootDeviceType": "ebs",
        "RootDeviceName": "/dev/sda1", "VirtualizationType": "hvm",
        "Name": "ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-20261101",
    }


def permission(port_from, port_to, cidrs):
    return {
        "IpProtocol": "tcp", "FromPort": port_from, "ToPort": port_to,
        "IpRanges": [{"CidrIp": cidr} for cidr in cidrs],
    }


def group(permissions=None, tags=None):
    return {
        "GroupId": "sg-owned", "GroupName": "mysticeti-test-cloud",
        "VpcId": "vpc-default", "Description": "Mysticeti testbed",
        "Tags": OWNER_TAGS if tags is None else tags,
        "IpPermissions": permissions or [], "IpPermissionsEgress": [],
    }


def instance(identifier, state="running", ip="198.51.100.1", tags=None):
    result = {
        "InstanceId": identifier,
        "State": {"Name": state, "Code": {
            "pending": 0, "running": 16, "shutting-down": 32, "terminated": 48,
            "stopping": 64, "stopped": 80,
        }[state]},
        "Tags": OWNER_TAGS if tags is None else tags,
    }
    if ip:
        result["PublicIpAddress"] = ip
    return result


class CloudTests(unittest.TestCase):
    def setUp(self):
        self.settings = {
            "testbed_id": "test-cloud",
            "key": {"name": "test-key", "path": "unused-private-key"},
            "instances": {
                "regions": ["us-east-1", "eu-west-1"],
                "type": "m5.8xlarge",
                "volume_size": 200,
            },
            "ssh_cidrs": ["203.0.113.8/32"],
        }
        self.clients = {
            region: boto3.client(
                "ec2", region_name=region,
                aws_access_key_id="testing", aws_secret_access_key="testing",
            )
            for region in self.settings["instances"]["regions"]
        }
        self.stubs = {region: Stubber(client) for region, client in self.clients.items()}
        for stub in self.stubs.values():
            stub.activate()
            self.addCleanup(stub.deactivate)
        self.cloud = Cloud(self.settings, clients=self.clients)

    def add_instances(self, region, rows, states=LIVE_STATES, next_token=None, request_token=None):
        response = {"Reservations": [{"Instances": rows}]}
        expected = {
            "Filters": OWN_FILTERS + [{"Name": "instance-state-name", "Values": states}]
        }
        if next_token:
            response["NextToken"] = next_token
        if request_token:
            expected["NextToken"] = request_token
        self.stubs[region].add_response("describe_instances", response, expected)

    def assert_consumed(self):
        for stub in self.stubs.values():
            stub.assert_no_pending_responses()

    def add_wait(self, region, ids, state):
        self.stubs[region].add_response("describe_instances", {
            "Reservations": [{"Instances": [instance(identifier, state) for identifier in ids]}]
        }, {"InstanceIds": ids})

    def add_empty_network(self):
        for region in self.stubs:
            self.add_instances(region, [], ["running"])
            self.stubs[region].add_response("describe_security_groups", {"SecurityGroups": []}, {"Filters": GROUP_FILTERS})

    def add_running_network(self, regional):
        peers = sorted({row["PublicIpAddress"] + "/32" for rows in regional.values() for row in rows})
        for region in self.stubs:
            self.add_instances(region, regional.get(region, []), ["running"])
            self.stubs[region].add_response("describe_security_groups", {"SecurityGroups": [group([
                permission(22, 22, ["203.0.113.8/32"]), permission(1500, 1627, peers),
            ])]}, {"Filters": GROUP_FILTERS})

    def prepare_private_key(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "key with spaces; no shell.pem"
        path.write_text("private-key-fixture", encoding="ascii")
        self.settings["key"]["path"] = str(path)
        return path

    def add_images(self, region, images):
        self.stubs[region].add_response("describe_images", {"Images": images}, {
            "Owners": ["099720109477"], "Filters": IMAGE_FILTERS,
        })

    def add_preflight(self, region, key_public=PUBLIC_KEY, missing_group=False, subnets=None):
        self.add_images(region, [ami("ami-old", "2025-01-01T00:00:00.000Z"), ami(), ami("ami-attacker", "2099-01-01T00:00:00.000Z", owner="123456789012")])
        self.stubs[region].add_response("describe_vpcs", {"Vpcs": [{"VpcId": "vpc-default", "IsDefault": True, "State": "available"}]}, {
            "Filters": [{"Name": "is-default", "Values": ["true"]}],
        })
        if subnets is None:
            subnets = [{
                "SubnetId": "subnet-default", "VpcId": "vpc-default", "State": "available",
                "DefaultForAz": True, "MapPublicIpOnLaunch": False,
                "AvailabilityZone": region + "a", "AvailableIpAddressCount": 200,
            }]
        self.stubs[region].add_response("describe_subnets", {"Subnets": subnets}, {
            "Filters": [
                {"Name": "vpc-id", "Values": ["vpc-default"]},
                {"Name": "default-for-az", "Values": ["true"]},
                {"Name": "state", "Values": ["available"]},
            ],
        })
        if not subnets:
            return
        expected_key = {"KeyNames": ["test-key"], "IncludePublicKey": True}
        if key_public is None:
            self.stubs[region].add_client_error("describe_key_pairs", service_error_code="InvalidKeyPair.NotFound", expected_params=expected_key)
        else:
            self.stubs[region].add_response("describe_key_pairs", {"KeyPairs": [{"KeyName": "test-key", "PublicKey": key_public}]}, expected_key)
            if key_public.split()[:2] != PUBLIC_KEY.split()[:2]:
                return
        self.stubs[region].add_response("describe_security_groups", {"SecurityGroups": [] if missing_group else [group()]}, {
            "Filters": GROUP_FILTERS + [{"Name": "vpc-id", "Values": ["vpc-default"]}],
        })

    def expected_launch(self, count):
        tags = OWNER_TAGS + [{"Key": "Name", "Value": "mysticeti-test-cloud"}]
        return {
            "ImageId": "ami-new", "InstanceType": "m5.8xlarge", "KeyName": "test-key",
            "MinCount": count, "MaxCount": count, "ClientToken": ANY,
            "NetworkInterfaces": [{"DeviceIndex": 0, "SubnetId": "subnet-default", "Groups": ["sg-owned"], "AssociatePublicIpAddress": True, "DeleteOnTermination": True}],
            "BlockDeviceMappings": [{"DeviceName": "/dev/sda1", "Ebs": {"VolumeSize": 200, "VolumeType": "gp3", "DeleteOnTermination": True, "Encrypted": True}}],
            "TagSpecifications": [{"ResourceType": resource, "Tags": tags} for resource in ["instance", "volume"]],
            "MetadataOptions": {"HttpTokens": "required", "HttpEndpoint": "enabled"},
        }

    def test_hosts_paginate_interleave_and_exclude_foreign_tags(self):
        self.add_instances("us-east-1", [instance("i-003", ip="198.51.100.3")], ["running"], "next")
        self.add_instances("us-east-1", [
            instance("i-001"),
            instance("i-foreign", tags=[{"Key": "Testbed", "Value": "test-cloud"}]),
        ], ["running"], request_token="next")
        self.add_instances("eu-west-1", [instance("i-002", ip="198.51.100.2")], ["running"])
        hosts = self.cloud.hosts()
        self.assertEqual([(h.region, h.instance_id, h.ip) for h in hosts], [
            ("us-east-1", "i-001", "198.51.100.1"),
            ("eu-west-1", "i-002", "198.51.100.2"),
            ("us-east-1", "i-003", "198.51.100.3"),
        ])
        self.assert_consumed()

    def test_running_instance_without_public_ipv4_fails_visibly(self):
        self.add_instances("us-east-1", [instance("i-private", ip=None)], ["running"])
        with self.assertRaisesRegex(RuntimeError, "i-private.*public IPv4"):
            self.cloud.hosts()

    def test_info_reports_all_owned_live_states(self):
        self.add_instances("us-east-1", [instance("i-running"), instance("i-stopped", "stopped", None)])
        self.add_instances("eu-west-1", [])
        self.assertEqual(self.cloud.info(), [
            {"region": "us-east-1", "instance_id": "i-running", "state": "running", "ip": "198.51.100.1"},
            {"region": "us-east-1", "instance_id": "i-stopped", "state": "stopped", "ip": None},
        ])
        self.assert_consumed()

    def test_network_replaces_broad_and_stale_ingress_with_ssh_and_current_peers(self):
        self.assertTrue(callable(getattr(self.cloud, "sync_network", None)), "network synchronization is missing")
        self.add_instances("us-east-1", [instance("i-001")], ["running"])
        self.add_instances("eu-west-1", [instance("i-002", ip="198.51.100.2")], ["running"])
        for region in self.stubs:
            existing = [permission(22, 22, ["0.0.0.0/0"]), permission(1500, 1627, ["198.51.100.99/32"])]
            self.stubs[region].add_response("describe_security_groups", {"SecurityGroups": [group(existing)]}, {"Filters": GROUP_FILTERS})
            self.stubs[region].add_response("revoke_security_group_ingress", {"Return": True}, {
                "GroupId": "sg-owned", "IpPermissions": existing,
            })
            self.stubs[region].add_response("authorize_security_group_ingress", {"Return": True}, {
                "GroupId": "sg-owned", "IpPermissions": [
                    permission(22, 22, ["203.0.113.8/32"]),
                    permission(1500, 1627, ["198.51.100.1/32", "198.51.100.2/32"]),
                ],
            })
        self.cloud.sync_network()
        self.assert_consumed()

    def test_network_is_idempotent_and_rejects_an_unowned_group_name(self):
        self.assertTrue(callable(getattr(self.cloud, "sync_network", None)), "network synchronization is missing")
        self.add_instances("us-east-1", [instance("i-001")], ["running"])
        self.add_instances("eu-west-1", [], ["running"])
        self.stubs["us-east-1"].add_response("describe_security_groups", {"SecurityGroups": [group([
            permission(22, 22, ["203.0.113.8/32"]), permission(1500, 1627, ["198.51.100.1/32"]),
        ])]}, {"Filters": GROUP_FILTERS})
        self.stubs["eu-west-1"].add_response("describe_security_groups", {"SecurityGroups": [group(tags=[
            {"Key": "ManagedBy", "Value": "bullshark"}, {"Key": "Testbed", "Value": "test-cloud"},
        ])]}, {"Filters": GROUP_FILTERS})
        with self.assertRaisesRegex(RuntimeError, "eu-west-1.*ownership"):
            self.cloud.sync_network()
        self.assert_consumed()

    def test_network_detects_controller_public_ip_when_no_ssh_cidr_is_configured(self):
        self.assertTrue(callable(getattr(self.cloud, "sync_network", None)), "network synchronization is missing")
        self.settings["ssh_cidrs"] = []
        for region in self.stubs:
            self.add_instances(region, [], ["running"])
            self.stubs[region].add_response("describe_security_groups", {"SecurityGroups": [group()]}, {"Filters": GROUP_FILTERS})
            self.stubs[region].add_response("authorize_security_group_ingress", {"Return": True}, {
                "GroupId": "sg-owned", "IpPermissions": [permission(22, 22, ["8.8.4.4/32"])],
            })
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b"8.8.4.4\n"
        with patch("mysticeti_aws.cloud.urlopen", return_value=response) as request:
            self.cloud.sync_network()
        self.assertEqual(request.call_args.kwargs["timeout"], 10)
        self.assert_consumed()

    def test_start_counts_running_and_pending_towards_each_region_limit(self):
        self.assertTrue(callable(getattr(self.cloud, "start", None)), "instance start is missing")
        self.add_instances("us-east-1", [
            instance("i-running"), instance("i-pending", "pending"),
            instance("i-stopped-z", "stopped"), instance("i-stopped-a", "stopped"),
        ])
        self.add_instances("eu-west-1", [instance("i-running-1"), instance("i-running-2"), instance("i-running-3"), instance("i-stopped", "stopped")])
        self.stubs["us-east-1"].add_response("start_instances", {}, {"InstanceIds": ["i-stopped-a"]})
        self.add_wait("us-east-1", ["i-pending", "i-stopped-a"], "running")
        self.add_running_network({
            "us-east-1": [instance("i-running"), instance("i-pending"), instance("i-stopped-a")],
            "eu-west-1": [instance("i-running-1"), instance("i-running-2"), instance("i-running-3")],
        })
        self.cloud.start(3)
        self.assert_consumed()

    def test_stop_waits_for_pending_and_stops_only_owned_active_nodes(self):
        self.assertTrue(callable(getattr(self.cloud, "stop", None)), "instance stop is missing")
        self.add_instances("us-east-1", [
            instance("i-pending", "pending"), instance("i-running"), instance("i-stopping", "stopping"),
            instance("i-stopped", "stopped"), instance("i-foreign", tags=[]),
        ])
        self.add_instances("eu-west-1", [])
        self.add_wait("us-east-1", ["i-pending"], "running")
        self.stubs["us-east-1"].add_response("stop_instances", {}, {"InstanceIds": ["i-pending", "i-running"]})
        self.add_wait("us-east-1", ["i-pending", "i-running", "i-stopping"], "stopped")
        self.add_empty_network()
        self.assertEqual(self.cloud.stop(), 3)
        self.assert_consumed()

    def test_destroy_waits_for_termination_before_deleting_owned_group(self):
        self.assertTrue(callable(getattr(self.cloud, "destroy", None)), "instance destroy is missing")
        self.add_instances("us-east-1", [instance("i-stopped", "stopped"), instance("i-going", "shutting-down"), instance("i-foreign", tags=[])])
        self.add_instances("eu-west-1", [])
        self.stubs["us-east-1"].add_response("describe_security_groups", {"SecurityGroups": [group()]}, {"Filters": GROUP_FILTERS})
        self.stubs["eu-west-1"].add_response("describe_security_groups", {"SecurityGroups": []}, {"Filters": GROUP_FILTERS})
        self.stubs["us-east-1"].add_response("terminate_instances", {}, {"InstanceIds": ["i-stopped"]})
        self.add_wait("us-east-1", ["i-going", "i-stopped"], "terminated")
        self.stubs["us-east-1"].add_response("delete_security_group", {}, {"GroupId": "sg-owned"})
        self.assertEqual(self.cloud.destroy(), 2)
        self.assert_consumed()

    def test_aws_errors_are_actionable_runtime_errors(self):
        self.stubs["us-east-1"].add_client_error("describe_instances", service_error_code="UnauthorizedOperation", service_message="Missing ec2:DescribeInstances")
        with self.assertRaises(Exception) as caught:
            self.cloud.info()
        self.assertIsInstance(caught.exception, RuntimeError)
        self.assertRegex(str(caught.exception), "info.*UnauthorizedOperation")

    def test_failed_instance_waiter_is_reported_without_unbounded_retry(self):
        self.assertTrue(callable(getattr(self.cloud, "start", None)), "instance start is missing")
        self.add_instances("us-east-1", [instance("i-stopped", "stopped")])
        self.add_instances("eu-west-1", [])
        self.stubs["us-east-1"].add_response("start_instances", {}, {"InstanceIds": ["i-stopped"]})
        self.add_wait("us-east-1", ["i-stopped"], "terminated")
        with self.assertRaisesRegex(RuntimeError, "us-east-1.*instance_running.*i-stopped"):
            self.cloud.start(1)
        self.assert_consumed()

    def test_create_launches_requested_count_in_every_region_with_noble_and_public_ip(self):
        self.assertTrue(callable(getattr(self.cloud, "create", None)), "instance creation is missing")
        path = self.prepare_private_key()
        for region in self.stubs:
            self.add_preflight(region, key_public=PUBLIC_KEY + " existing-comment")
            self.stubs[region].add_response("run_instances", {"Instances": [instance("i-new-1", "pending"), instance("i-new-2", "pending")]}, self.expected_launch(2))
            self.add_wait(region, ["i-new-1", "i-new-2"], "running")
        self.add_running_network({
            "us-east-1": [instance("i-new-1", ip="198.51.100.1"), instance("i-new-2", ip="198.51.100.2")],
            "eu-west-1": [instance("i-new-1", ip="198.51.100.3"), instance("i-new-2", ip="198.51.100.4")],
        })
        with patch("mysticeti_aws.cloud.subprocess.run", return_value=SimpleNamespace(stdout=PUBLIC_KEY + " local-comment\n")) as derive:
            hosts = self.cloud.create(2)
        self.assertEqual([host.ip for host in hosts], ["198.51.100.1", "198.51.100.3", "198.51.100.2", "198.51.100.4"])
        self.assertEqual(derive.call_args.args[0], ["ssh-keygen", "-y", "-P", "", "-f", str(path)])
        self.assertNotIn("shell", derive.call_args.kwargs)
        self.assert_consumed()

    def test_create_preflights_every_region_before_import_or_launch(self):
        self.assertTrue(callable(getattr(self.cloud, "create", None)), "instance creation is missing")
        self.prepare_private_key()
        self.add_preflight("us-east-1", key_public=None, missing_group=True)
        self.add_preflight("eu-west-1", key_public="ssh-ed25519 " + base64.b64encode(b"different key").decode())
        with patch("mysticeti_aws.cloud.subprocess.run", return_value=SimpleNamespace(stdout=PUBLIC_KEY)):
            with self.assertRaisesRegex(RuntimeError, "eu-west-1.*key.*match"):
                self.cloud.create(2)
        self.assert_consumed()

    def test_create_rejects_missing_default_subnet_before_any_mutation(self):
        self.assertTrue(callable(getattr(self.cloud, "create", None)), "instance creation is missing")
        self.prepare_private_key()
        self.add_preflight("us-east-1", subnets=[])
        with patch("mysticeti_aws.cloud.subprocess.run", return_value=SimpleNamespace(stdout=PUBLIC_KEY)):
            with self.assertRaisesRegex(RuntimeError, "us-east-1.*default subnet"):
                self.cloud.create(2)
        self.assert_consumed()

    def test_create_rejects_noncanonical_or_arm_images(self):
        self.assertTrue(callable(getattr(self.cloud, "create", None)), "instance creation is missing")
        self.prepare_private_key()
        self.add_images("us-east-1", [ami(owner="123456789012"), ami(architecture="arm64")])
        with patch("mysticeti_aws.cloud.subprocess.run", return_value=SimpleNamespace(stdout=PUBLIC_KEY)):
            with self.assertRaisesRegex(RuntimeError, "us-east-1.*Canonical.*24.04.*amd64"):
                self.cloud.create(2)
        self.assert_consumed()

    def test_create_imports_missing_key_and_tags_new_security_group(self):
        self.assertTrue(callable(getattr(self.cloud, "create", None)), "instance creation is missing")
        self.prepare_private_key()
        tags = OWNER_TAGS + [{"Key": "Name", "Value": "mysticeti-test-cloud"}]
        for region in self.stubs:
            self.add_preflight(region, key_public=None, missing_group=True)
            self.stubs[region].add_response("import_key_pair", {}, {
                "KeyName": "test-key", "PublicKeyMaterial": PUBLIC_KEY.encode("ascii"),
                "TagSpecifications": [{"ResourceType": "key-pair", "Tags": tags}],
            })
            self.stubs[region].add_response("create_security_group", {"GroupId": "sg-owned"}, {
                "GroupName": "mysticeti-test-cloud", "Description": "Dedicated Mysticeti testbed test-cloud",
                "VpcId": "vpc-default", "TagSpecifications": [{"ResourceType": "security-group", "Tags": tags}],
            })
            self.stubs[region].add_response("run_instances", {"Instances": [instance("i-new", "pending")]}, self.expected_launch(1))
            self.add_wait(region, ["i-new"], "running")
        self.add_running_network({region: [instance("i-new")] for region in self.stubs})
        with patch("mysticeti_aws.cloud.subprocess.run", return_value=SimpleNamespace(stdout=PUBLIC_KEY)):
            self.cloud.create(1)
        self.assert_consumed()

    def test_create_waits_until_new_instances_are_visible_in_owned_inventory(self):
        self.prepare_private_key()
        for region in self.stubs:
            self.add_preflight(region)
            self.stubs[region].add_response("run_instances", {"Instances": [instance("i-new", "pending")]}, self.expected_launch(1))
            self.add_wait(region, ["i-new"], "running")
            self.add_instances(region, [], ["running"])
        self.add_running_network({region: [instance("i-new")] for region in self.stubs})
        with patch("mysticeti_aws.cloud.subprocess.run", return_value=SimpleNamespace(stdout=PUBLIC_KEY)), patch("time.sleep"):
            try:
                hosts = self.cloud.create(1)
            except RuntimeError as exc:
                self.fail(f"A newly launched instance should be awaited before network sync: {exc}")
        self.assertEqual(len(hosts), 2)
        self.assert_consumed()

    def test_destroy_retries_group_dependency_while_terminated_network_interface_disappears(self):
        for region in self.stubs:
            self.add_instances(region, [])
        self.stubs["us-east-1"].add_response("describe_security_groups", {"SecurityGroups": [group()]}, {"Filters": GROUP_FILTERS})
        self.stubs["eu-west-1"].add_response("describe_security_groups", {"SecurityGroups": []}, {"Filters": GROUP_FILTERS})
        self.stubs["us-east-1"].add_client_error("delete_security_group", service_error_code="DependencyViolation", expected_params={"GroupId": "sg-owned"})
        self.stubs["us-east-1"].add_response("delete_security_group", {}, {"GroupId": "sg-owned"})
        with patch("time.sleep"):
            try:
                self.cloud.destroy()
            except RuntimeError as exc:
                self.fail(f"Transient network-interface dependency should be retried: {exc}")
        self.assert_consumed()

    def test_waiter_stops_after_120_pending_responses(self):
        self.add_instances("us-east-1", [instance("i-pending", "pending")])
        self.add_instances("eu-west-1", [])
        for _ in range(120):
            self.add_wait("us-east-1", ["i-pending"], "pending")
        with patch("time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "instance_running.*i-pending"):
                self.cloud.start(1)
        self.assert_consumed()

    def test_create_partial_aws_failure_reports_launched_ids_and_additive_retry_risk(self):
        self.prepare_private_key()
        for region in self.stubs:
            self.add_preflight(region)
        self.stubs["us-east-1"].add_response("run_instances", {"Instances": [instance("i-created", "pending")]}, self.expected_launch(1))
        self.stubs["eu-west-1"].add_client_error("run_instances", service_error_code="InsufficientInstanceCapacity", expected_params=self.expected_launch(1))
        with patch("mysticeti_aws.cloud.subprocess.run", return_value=SimpleNamespace(stdout=PUBLIC_KEY)):
            with self.assertRaisesRegex(RuntimeError, "i-created.*additive.*InsufficientInstanceCapacity"):
                self.cloud.create(1)
        self.assert_consumed()

    def test_sync_network_waits_briefly_for_public_ipv4_assignment(self):
        self.add_instances("us-east-1", [instance("i-new", ip=None)], ["running"])
        self.add_instances("us-east-1", [instance("i-new")], ["running"])
        self.add_instances("eu-west-1", [], ["running"])
        for region in self.stubs:
            self.stubs[region].add_response("describe_security_groups", {"SecurityGroups": []}, {"Filters": GROUP_FILTERS})
        with patch("time.sleep"):
            try:
                hosts = self.cloud.sync_network()
            except RuntimeError as exc:
                self.fail(f"Public IPv4 assignment should be retried briefly: {exc}")
        self.assertEqual(hosts[0].ip, "198.51.100.1")
        self.assert_consumed()


if __name__ == "__main__":
    unittest.main()
