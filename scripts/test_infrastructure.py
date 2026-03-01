#!/usr/bin/env python3
"""
End-to-end infrastructure test suite for Locaweb CloudStack deployment.

Validates all deployment scenarios by calling provision_infrastructure.py
and teardown_infrastructure.py, then verifying results via cmk and SSH.

Test execution order (optimized to minimize deploys):

  Phase 0: Initial teardown (clean slate)
  Phase 1: Complete deploy (web+3w+db) -> scale down 3->1 -> teardown
  Phase 2: Web-only deploy -> teardown
  Phase 3: Deploy with workers+db (1w) -> scale up 1->3 -> teardown

Environment variables:
  REPO_NAME  - Repository name (default: from cwd)
  UNIQUE_ID  - Unique identifier for resource isolation (default: "test")
  ZONE       - CloudStack zone (default: "ZP01")
"""
import json
import os
import subprocess
import sys
import time
import traceback

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_NAME = os.environ.get("REPO_NAME", "locaweb-cloud-deploy")
UNIQUE_ID = os.environ.get("UNIQUE_ID", "test")
ENV_NAME = os.environ.get("ENV_NAME", "test")
ZONE = os.environ.get("ZONE", "ZP01")
NETWORK_NAME = f"{REPO_NAME}-{UNIQUE_ID}-{ENV_NAME}"

SSH_KEY_PATH = "/tmp/ssh_key"
PUBLIC_KEY_PATH = "/tmp/ssh_key.pub"
PROVISION_OUTPUT = "/tmp/provision-output-test.json"
RESULTS_PATH = "/tmp/test-results.json"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROVISION_SCRIPT = os.path.join(SCRIPT_DIR, "provision_infrastructure.py")
TEARDOWN_SCRIPT = os.path.join(SCRIPT_DIR, "teardown_infrastructure.py")

CMK_MAX_RETRIES = 5


# ---------------------------------------------------------------------------
# CloudMonkey helper
# ---------------------------------------------------------------------------

def cmk(*args):
    """Run a cmk command with retry logic. Returns parsed JSON or None."""
    cmd = ["cmk"] + list(args)
    for attempt in range(CMK_MAX_RETRIES + 1):
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            if not result.stdout.strip():
                return {}
            return json.loads(result.stdout)
        error_msg = result.stderr.strip() or result.stdout.strip()
        if attempt < CMK_MAX_RETRIES:
            backoff = 2 ** (attempt + 1)
            print(f"    cmk retry {attempt + 1}/{CMK_MAX_RETRIES}: {error_msg} (backoff {backoff}s)")
            time.sleep(backoff)
        else:
            print(f"    cmk failed after {CMK_MAX_RETRIES + 1} attempts: {error_msg}")
            return None


# ---------------------------------------------------------------------------
# SSH Verifier
# ---------------------------------------------------------------------------

class SSHVerifier:
    """SSH connectivity and remote command execution."""

    def __init__(self, key_path=SSH_KEY_PATH):
        self.key_path = key_path
        self.ssh_opts = [
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=10",
            "-o", "BatchMode=yes",
            "-i", self.key_path,
        ]

    def wait_for_ssh(self, ip, timeout=180):
        """Poll SSH every 10s until it responds or timeout is reached."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            rc, _, _ = self.run_command(ip, "true")
            if rc == 0:
                return True
            time.sleep(10)
        return False

    def run_command(self, ip, command):
        """Run a remote command via SSH. Returns (rc, stdout, stderr)."""
        cmd = ["ssh"] + self.ssh_opts + [f"root@{ip}", command]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            return result.returncode, result.stdout.strip(), result.stderr.strip()
        except subprocess.TimeoutExpired:
            return 1, "", "SSH command timed out"

    def verify_mount_point(self, ip, path, timeout=120):
        """Poll mountpoint with retry (cloud-init may still be running)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            rc, _, _ = self.run_command(ip, f"mountpoint -q {path}")
            if rc == 0:
                return True
            time.sleep(10)
        return False


# ---------------------------------------------------------------------------
# Infrastructure Verifier
# ---------------------------------------------------------------------------

class InfrastructureVerifier:
    """cmk-based resource verification."""

    def __init__(self, network_name):
        self.network_name = network_name
        self.keypair_name = f"{network_name}-key"

    # --- Network ---

    def get_network_id(self):
        data = cmk("list", "networks", "filter=id,name")
        if data:
            for n in data.get("network", []):
                if n["name"] == self.network_name:
                    return n["id"]
        return None

    def verify_network_exists(self):
        return self.get_network_id() is not None

    def verify_network_absent(self):
        return self.get_network_id() is None

    # --- VMs ---

    def verify_vm_exists(self, name):
        """Return VM dict if found, else None."""
        data = cmk("list", "virtualmachines", f"name={name}",
                    "filter=id,name,state")
        if data:
            for vm in data.get("virtualmachine", []):
                if vm["name"] == name:
                    return vm
        return None

    def verify_vm_absent(self, name):
        return self.verify_vm_exists(name) is None

    def count_worker_vms(self):
        """Scan worker-1, worker-2, ... until not found."""
        count = 0
        i = 1
        while True:
            name = f"worker-{i}"
            if self.verify_vm_exists(name):
                count += 1
                i += 1
            else:
                break
        return count

    # --- Volumes ---

    def verify_volume_exists(self, name):
        """Return volume dict if found, else None."""
        data = cmk("list", "volumes", f"name={name}", "type=DATADISK",
                    "filter=id,name,state")
        if data:
            for v in data.get("volume", []):
                if v["name"] == name:
                    return v
        return None

    def verify_volume_absent(self, name):
        return self.verify_volume_exists(name) is None

    def verify_volume_tags(self, name):
        """Check volume has the locaweb-cloud-deploy-id tag."""
        data = cmk("list", "volumes", f"name={name}", "type=DATADISK",
                    "tags[0].key=locaweb-cloud-deploy-id",
                    f"tags[0].value={self.network_name}",
                    "filter=id,name")
        if data:
            for v in data.get("volume", []):
                if v["name"] == name:
                    return True
        return False

    def verify_no_tagged_volumes(self):
        """Ensure no volumes with our tag remain."""
        data = cmk("list", "volumes", "type=DATADISK",
                    "tags[0].key=locaweb-cloud-deploy-id",
                    f"tags[0].value={self.network_name}",
                    "filter=id,name")
        if data and data.get("volume"):
            return len(data["volume"]) == 0
        return True

    # --- Snapshot Policies ---

    def verify_snapshot_policy(self, vol_id):
        """Check that a snapshot policy exists for the volume."""
        data = cmk("list", "snapshotpolicies", f"volumeid={vol_id}")
        if data and data.get("snapshotpolicy"):
            return len(data["snapshotpolicy"]) > 0
        return False

    # --- Public IPs ---

    def count_non_sourcenat_ips(self):
        net_id = self.get_network_id()
        if not net_id:
            return 0
        data = cmk("list", "publicipaddresses",
                    f"associatednetworkid={net_id}",
                    "filter=id,ipaddress,issourcenat")
        count = 0
        if data:
            for ip in data.get("publicipaddress", []):
                if not ip.get("issourcenat", False):
                    count += 1
        return count

    def find_ip_for_vm(self, network_id, vm_id):
        """Find public IP with static NAT pointing to a specific VM."""
        data = cmk("list", "publicipaddresses",
                    f"associatednetworkid={network_id}",
                    "filter=id,ipaddress,issourcenat,isstaticnat,virtualmachineid")
        if data:
            for ip in data.get("publicipaddress", []):
                if ip.get("virtualmachineid") == vm_id:
                    return ip
        return None

    # --- Firewall Rules ---

    def verify_firewall_rules(self, ip_id, expected_ports):
        """Verify exact set of firewall ports on an IP."""
        data = cmk("list", "firewallrules", f"ipaddressid={ip_id}",
                    "filter=id,startport,endport")
        actual_ports = set()
        if data:
            for r in data.get("firewallrule", []):
                actual_ports.add(int(r["startport"]))
        return actual_ports == set(expected_ports)

    # --- Static NAT ---

    def verify_static_nat(self, ip_id, expected_vm_id):
        """Verify static NAT points to the expected VM."""
        data = cmk("list", "publicipaddresses", f"id={ip_id}",
                    "filter=id,isstaticnat,virtualmachineid")
        if data and data.get("publicipaddress"):
            ip = data["publicipaddress"][0]
            return (ip.get("isstaticnat", False)
                    and ip.get("virtualmachineid") == expected_vm_id)
        return False

    # --- VM Offering ---

    def verify_vm_offering(self, name, expected_offering_id):
        """Check that a VM's service offering matches expected."""
        data = cmk("list", "virtualmachines", f"name={name}",
                    "filter=id,name,serviceofferingid")
        if data:
            for vm in data.get("virtualmachine", []):
                if vm["name"] == name:
                    return vm.get("serviceofferingid") == expected_offering_id
        return False

    # --- Volume Size ---

    def get_volume_size_gb(self, name):
        """Return volume size in GB via cmk, or None if not found."""
        data = cmk("list", "volumes", f"name={name}", "type=DATADISK",
                    "filter=id,name,size")
        if data:
            for v in data.get("volume", []):
                if v["name"] == name:
                    return v.get("size", 0) // (1024 ** 3)
        return None

    # --- SSH Key Pair ---

    def verify_keypair_exists(self):
        data = cmk("list", "sshkeypairs", f"name={self.keypair_name}")
        return bool(data and data.get("sshkeypair"))

    def verify_keypair_absent(self):
        return not self.verify_keypair_exists()


# ---------------------------------------------------------------------------
# Test Scenario (context manager for assertion tracking)
# ---------------------------------------------------------------------------

class TestScenario:
    """Tracks assertions and duration for a single test scenario."""

    def __init__(self, name):
        self.name = name
        self.assertions = []
        self.status = "PASS"
        self.start_time = None
        self.duration = 0

    def __enter__(self):
        self.start_time = time.time()
        print(f"\n{'=' * 60}")
        print(f"SCENARIO: {self.name}")
        print(f"{'=' * 60}")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.duration = time.time() - self.start_time
        if exc_type:
            self.status = "FAIL"
            self.assertions.append({
                "message": f"Exception: {exc_val}",
                "passed": False,
            })
            print(f"  [FAIL] Exception: {exc_val}")
            traceback.print_exception(exc_type, exc_val, exc_tb)
        passed = sum(1 for a in self.assertions if a["passed"])
        failed = sum(1 for a in self.assertions if not a["passed"])
        print(f"\n  Result: [{self.status}] {passed} passed, {failed} failed ({self.duration:.0f}s)")
        return True  # suppress exceptions so suite continues

    def assert_true(self, condition, message):
        passed = bool(condition)
        self.assertions.append({"message": message, "passed": passed})
        tag = "PASS" if passed else "FAIL"
        print(f"  [{tag}] {message}")
        if not passed:
            self.status = "FAIL"
        return passed

    def assert_equal(self, actual, expected, message):
        passed = actual == expected
        full_msg = f"{message} (expected={expected}, actual={actual})"
        self.assertions.append({"message": full_msg, "passed": passed})
        tag = "PASS" if passed else "FAIL"
        print(f"  [{tag}] {full_msg}")
        if not passed:
            self.status = "FAIL"
        return passed


# ---------------------------------------------------------------------------
# Test Runner
# ---------------------------------------------------------------------------

class TestRunner:
    """Orchestrates all test scenarios."""

    def __init__(self):
        self.scenarios = []
        self.verifier = InfrastructureVerifier(NETWORK_NAME)
        self.ssh = SSHVerifier()
        self.last_output = None

    # --- Helpers ---

    def resolve_offering_id(self, name):
        """Resolve a service offering name to its ID via cmk."""
        data = cmk("list", "serviceofferings", "filter=id,name")
        if data:
            for so in data.get("serviceoffering", []):
                if so["name"] == name:
                    return so["id"]
        return None

    def provision(self, config):
        """Write config to /tmp and call provision_infrastructure.py."""
        config_path = "/tmp/test-config.json"
        with open(config_path, "w") as f:
            json.dump(config, f, indent=2)
        print(f"\n  Provisioning with config:")
        for k, v in config.items():
            print(f"    {k}: {v}")
        result = subprocess.run(
            ["python3", "-u", PROVISION_SCRIPT,
             "--repo-name", REPO_NAME,
             "--unique-id", UNIQUE_ID,
             "--env-name", ENV_NAME,
             "--config", config_path,
             "--public-key", PUBLIC_KEY_PATH,
             "--output", PROVISION_OUTPUT],
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Provisioning failed with exit code {result.returncode}")
        with open(PROVISION_OUTPUT) as f:
            self.last_output = json.load(f)
        return self.last_output

    def teardown(self):
        """Call teardown_infrastructure.py."""
        print(f"\n  Tearing down: {NETWORK_NAME} (zone={ZONE})")
        result = subprocess.run(
            ["python3", "-u", TEARDOWN_SCRIPT,
             "--network-name", NETWORK_NAME,
             "--zone", ZONE],
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Teardown failed with exit code {result.returncode}")

    def save_results(self):
        """Write test results JSON to /tmp/test-results.json."""
        total_pass = sum(
            sum(1 for a in s.assertions if a["passed"])
            for s in self.scenarios
        )
        total_fail = sum(
            sum(1 for a in s.assertions if not a["passed"])
            for s in self.scenarios
        )
        total_duration = sum(s.duration for s in self.scenarios)
        results = {
            "scenarios": [
                {
                    "name": s.name,
                    "status": s.status,
                    "duration": s.duration,
                    "assertions": s.assertions,
                }
                for s in self.scenarios
            ],
            "total_pass": total_pass,
            "total_fail": total_fail,
            "total_duration": total_duration,
        }
        with open(RESULTS_PATH, "w") as f:
            json.dump(results, f, indent=2)
        print(f"\nResults written to {RESULTS_PATH}")
        return total_fail == 0

    # --- Scenario runner ---

    def run_all(self):
        """Run all test phases in order."""
        self._phase0_initial_teardown()

        self._phase1_complete_deploy()
        self._phase1_scale_down()
        self._phase1_teardown()

        self._phase2_web_only_deploy()
        self._phase2_teardown()

        self._phase3_deploy_with_features()
        self._phase3_scale_up()
        self._phase3_teardown()

        return self.save_results()

    # ------------------------------------------------------------------
    # Phase 0: Clean slate
    # ------------------------------------------------------------------

    def _phase0_initial_teardown(self):
        s = TestScenario("Phase 0: Initial Teardown")
        with s:
            self.teardown()
            s.assert_true(
                self.verifier.verify_network_absent(),
                "Network absent after initial teardown")
        self.scenarios.append(s)

    # ------------------------------------------------------------------
    # Phase 1: Complete deploy chain
    # ------------------------------------------------------------------

    def _phase1_complete_deploy(self):
        s = TestScenario("1. Complete Deploy")
        with s:
            output = self.provision({
                "zone": ZONE,
                "web_plan": "small",
                "web_disk_size_gb": 30,
                "workers_replicas": 3,
                "workers_plan": "small",
                "accessories": [
                    {"name": "db", "plan": "medium", "disk_size_gb": 25},
                ],
            })

            # --- Network & keypair ---
            s.assert_true(self.verifier.verify_network_exists(),
                          "Network exists")
            s.assert_true(self.verifier.verify_keypair_exists(),
                          "SSH keypair exists")

            # --- Web VM ---
            web = self.verifier.verify_vm_exists("web")
            s.assert_true(web is not None, "Web VM exists")
            if web:
                s.assert_equal(web.get("state"), "Running",
                               "Web VM is Running")

            # --- Workers 1-3 ---
            for i in range(1, 4):
                name = f"worker-{i}"
                vm = self.verifier.verify_vm_exists(name)
                s.assert_true(vm is not None, f"Worker-{i} VM exists")
                if vm:
                    s.assert_equal(vm.get("state"), "Running",
                                   f"Worker-{i} VM is Running")
            s.assert_equal(self.verifier.count_worker_vms(), 3,
                           "Worker count is 3")

            # --- DB VM ---
            db = self.verifier.verify_vm_exists("db")
            s.assert_true(db is not None, "DB VM exists")
            if db:
                s.assert_equal(db.get("state"), "Running",
                               "DB VM is Running")

            # --- Volumes ---
            web_vol_name = f"{NETWORK_NAME}-webdata"
            s.assert_true(
                self.verifier.verify_volume_exists(web_vol_name) is not None,
                "Web volume exists")
            s.assert_true(
                self.verifier.verify_volume_tags(web_vol_name),
                "Web volume has correct tag")

            db_vol_name = f"{NETWORK_NAME}-dbdata"
            s.assert_true(
                self.verifier.verify_volume_exists(db_vol_name) is not None,
                "DB volume exists")
            s.assert_true(
                self.verifier.verify_volume_tags(db_vol_name),
                "DB volume has correct tag")

            # --- Snapshot policies ---
            s.assert_true(
                self.verifier.verify_snapshot_policy(output["web_volume_id"]),
                "Web snapshot policy exists")
            s.assert_true(
                self.verifier.verify_snapshot_policy(
                    output["accessories"]["db"]["volume_id"]),
                "DB snapshot policy exists")

            # --- Public IPs ---
            s.assert_equal(self.verifier.count_non_sourcenat_ips(), 5,
                           "5 non-source-NAT public IPs")

            # --- Firewall rules ---
            s.assert_true(
                self.verifier.verify_firewall_rules(
                    output["web_ip_id"], [22, 80, 443]),
                "Web firewall: ports 22, 80, 443")
            s.assert_true(
                self.verifier.verify_firewall_rules(
                    output["accessories"]["db"]["ip_id"], [22]),
                "DB firewall: port 22")

            net_id = output["network_id"]
            for i, wvm_id in enumerate(output.get("worker_vm_ids", []), 1):
                wip = self.verifier.find_ip_for_vm(net_id, wvm_id)
                if wip:
                    s.assert_true(
                        self.verifier.verify_firewall_rules(wip["id"], [22]),
                        f"Worker-{i} firewall: port 22")
                else:
                    s.assert_true(False, f"Worker-{i} public IP found")

            # --- Static NAT ---
            s.assert_true(
                self.verifier.verify_static_nat(
                    output["web_ip_id"], output["web_vm_id"]),
                "Web IP static NAT -> web VM")
            s.assert_true(
                self.verifier.verify_static_nat(
                    output["accessories"]["db"]["ip_id"],
                    output["accessories"]["db"]["vm_id"]),
                "DB IP static NAT -> DB VM")

            # --- SSH verification ---
            web_ip = output["web_ip"]
            s.assert_true(
                self.ssh.wait_for_ssh(web_ip, timeout=180),
                "SSH to web VM: reachable")
            s.assert_true(
                self.ssh.verify_mount_point(web_ip, "/data/", timeout=120),
                "SSH to web VM: /data/ mounted")

            db_ip = output["accessories"]["db"]["ip"]
            s.assert_true(
                self.ssh.wait_for_ssh(db_ip, timeout=180),
                "SSH to DB VM: reachable")
            s.assert_true(
                self.ssh.verify_mount_point(db_ip, "/data/", timeout=120),
                "SSH to DB VM: /data/ mounted")

            for i, wip_addr in enumerate(output.get("worker_ips", []), 1):
                s.assert_true(
                    self.ssh.wait_for_ssh(wip_addr, timeout=180),
                    f"SSH to Worker-{i} VM: reachable")

            # --- Output JSON fields ---
            s.assert_true("web_vm_id" in output, "Output has web_vm_id")
            s.assert_true("web_ip" in output, "Output has web_ip")
            s.assert_true("web_volume_id" in output,
                          "Output has web_volume_id")
            s.assert_equal(len(output.get("worker_vm_ids", [])), 3,
                           "Output has 3 worker_vm_ids")
            s.assert_true("accessories" in output,
                          "Output has accessories")
            s.assert_true("db" in output.get("accessories", {}),
                          "Output accessories has db")
            s.assert_true("vm_id" in output.get("accessories", {}).get("db", {}),
                          "Output accessories.db has vm_id")
            s.assert_true("volume_id" in output.get("accessories", {}).get("db", {}),
                          "Output accessories.db has volume_id")
            s.assert_true("ip" in output.get("accessories", {}).get("db", {}),
                          "Output accessories.db has ip")

        self.scenarios.append(s)

    def _phase1_scale_down(self):
        s = TestScenario("2. Scale Down Workers 3->1")
        with s:
            self.provision({
                "zone": ZONE,
                "web_plan": "small",
                "web_disk_size_gb": 30,
                "workers_replicas": 1,
                "workers_plan": "small",
                "accessories": [
                    {"name": "db", "plan": "medium", "disk_size_gb": 25},
                ],
            })

            s.assert_true(
                self.verifier.verify_vm_exists(
                    f"worker-1") is not None,
                "Worker-1 still exists")
            s.assert_true(
                self.verifier.verify_vm_absent(f"worker-2"),
                "Worker-2 gone")
            s.assert_true(
                self.verifier.verify_vm_absent(f"worker-3"),
                "Worker-3 gone")
            s.assert_equal(self.verifier.count_worker_vms(), 1,
                           "Worker count is 1")

            s.assert_true(
                self.verifier.verify_vm_exists(
                    "web") is not None,
                "Web VM unaffected")
            s.assert_true(
                self.verifier.verify_vm_exists(
                    "db") is not None,
                "DB VM unaffected")

            s.assert_equal(self.verifier.count_non_sourcenat_ips(), 3,
                           "3 public IPs remain")

        self.scenarios.append(s)

    def _phase1_teardown(self):
        s = TestScenario("3. Teardown Verify")
        with s:
            self.teardown()

            s.assert_true(self.verifier.verify_network_absent(),
                          "Network gone")
            s.assert_true(self.verifier.verify_keypair_absent(),
                          "Keypair gone")
            s.assert_true(
                self.verifier.verify_vm_absent("web"),
                "Web VM gone")
            s.assert_true(
                self.verifier.verify_vm_absent("db"),
                "DB VM gone")
            s.assert_true(
                self.verifier.verify_vm_absent(f"worker-1"),
                "Worker-1 VM gone")
            s.assert_true(
                self.verifier.verify_volume_absent(f"{NETWORK_NAME}-webdata"),
                "Web volume gone")
            s.assert_true(
                self.verifier.verify_volume_absent(f"{NETWORK_NAME}-dbdata"),
                "DB volume gone")
            s.assert_true(self.verifier.verify_no_tagged_volumes(),
                          "No tagged volumes remain")

        self.scenarios.append(s)

    # ------------------------------------------------------------------
    # Phase 2: Web-only deploy (no workers, no accessories)
    # ------------------------------------------------------------------

    def _phase2_web_only_deploy(self):
        s = TestScenario("4. Web-Only Deploy")
        with s:
            output = self.provision({
                "zone": ZONE,
                "web_plan": "small",
                "web_disk_size_gb": 20,
                "workers_replicas": 0,
                "workers_plan": "small",
                "accessories": [],
            })

            s.assert_true(
                self.verifier.verify_vm_exists(
                    "web") is not None,
                "Web VM exists")
            s.assert_equal(self.verifier.count_worker_vms(), 0,
                           "Zero workers")
            s.assert_true(
                self.verifier.verify_vm_absent("db"),
                "No DB VM")

            s.assert_true(
                self.verifier.verify_volume_exists(
                    f"{NETWORK_NAME}-webdata") is not None,
                "Web volume exists")
            s.assert_true(
                self.verifier.verify_volume_absent(f"{NETWORK_NAME}-dbdata"),
                "No DB volume")

            s.assert_equal(self.verifier.count_non_sourcenat_ips(), 1,
                           "1 public IP")

            # SSH
            web_ip = output["web_ip"]
            s.assert_true(
                self.ssh.wait_for_ssh(web_ip, timeout=180),
                "SSH to web: reachable")
            s.assert_true(
                self.ssh.verify_mount_point(web_ip, "/data/", timeout=120),
                "SSH to web: /data/ mounted")

            # Output JSON
            s.assert_true("worker_vm_ids" not in output,
                          "Output has no worker_vm_ids")
            accessories = output.get("accessories", {})
            s.assert_true(
                not accessories,
                "Output has no accessories (empty or absent)")

        self.scenarios.append(s)

    def _phase2_teardown(self):
        s = TestScenario("5. Teardown Verify")
        with s:
            self.teardown()

            s.assert_true(self.verifier.verify_network_absent(),
                          "Network gone")
            s.assert_true(self.verifier.verify_keypair_absent(),
                          "Keypair gone")
            s.assert_true(
                self.verifier.verify_vm_absent("web"),
                "Web VM gone")
            s.assert_true(
                self.verifier.verify_volume_absent(f"{NETWORK_NAME}-webdata"),
                "Web volume gone")
            s.assert_true(self.verifier.verify_no_tagged_volumes(),
                          "No tagged volumes remain")

        self.scenarios.append(s)

    # ------------------------------------------------------------------
    # Phase 3: Deploy with features, then scale up
    # ------------------------------------------------------------------

    def _phase3_deploy_with_features(self):
        s = TestScenario("6. Deploy with Workers+DB (small plans)")
        with s:
            self.provision({
                "zone": ZONE,
                "web_plan": "small",
                "web_disk_size_gb": 20,
                "workers_replicas": 1,
                "workers_plan": "small",
                "accessories": [
                    {"name": "db", "plan": "small", "disk_size_gb": 20},
                ],
            })

            s.assert_equal(self.verifier.count_worker_vms(), 1,
                           "1 worker (default replicas)")
            s.assert_true(
                self.verifier.verify_vm_exists(
                    "db") is not None,
                "DB VM exists")
            s.assert_true(
                self.verifier.verify_volume_exists(
                    f"{NETWORK_NAME}-dbdata") is not None,
                "DB volume exists")
            s.assert_equal(self.verifier.count_non_sourcenat_ips(), 3,
                           "3 public IPs")

            # Verify initial offerings
            small_id = self.resolve_offering_id("small")
            s.assert_true(small_id is not None,
                          "Resolved 'small' offering ID")
            if small_id:
                s.assert_true(
                    self.verifier.verify_vm_offering(
                        "web", small_id),
                    "Web VM has 'small' offering")
                s.assert_true(
                    self.verifier.verify_vm_offering(
                        f"worker-1", small_id),
                    "Worker-1 has 'small' offering")
                s.assert_true(
                    self.verifier.verify_vm_offering(
                        "db", small_id),
                    "DB VM has 'small' offering")

            # Verify initial disk sizes
            s.assert_equal(
                self.verifier.get_volume_size_gb(f"{NETWORK_NAME}-webdata"),
                20, "Web volume is 20GB")
            s.assert_equal(
                self.verifier.get_volume_size_gb(f"{NETWORK_NAME}-dbdata"),
                20, "DB volume is 20GB")

        self.scenarios.append(s)

    def _phase3_scale_up(self):
        s = TestScenario("7. Scale Up Workers 1->3 + Offerings & Disks")
        with s:
            self.provision({
                "zone": ZONE,
                "web_plan": "medium",
                "web_disk_size_gb": 30,
                "workers_replicas": 3,
                "workers_plan": "medium",
                "accessories": [
                    {"name": "db", "plan": "medium", "disk_size_gb": 25},
                ],
            })

            for i in range(1, 4):
                vm = self.verifier.verify_vm_exists(
                    f"worker-{i}")
                s.assert_true(vm is not None, f"Worker-{i} exists")
                if vm:
                    s.assert_equal(vm.get("state"), "Running",
                                   f"Worker-{i} is Running")
            s.assert_equal(self.verifier.count_worker_vms(), 3,
                           "Worker count is 3")
            s.assert_equal(self.verifier.count_non_sourcenat_ips(), 5,
                           "5 public IPs")

            # Verify offerings changed to medium
            medium_id = self.resolve_offering_id("medium")
            s.assert_true(medium_id is not None,
                          "Resolved 'medium' offering ID")
            if medium_id:
                s.assert_true(
                    self.verifier.verify_vm_offering(
                        "web", medium_id),
                    "Web VM scaled to 'medium' offering")
                s.assert_true(
                    self.verifier.verify_vm_offering(
                        f"worker-1", medium_id),
                    "Worker-1 scaled to 'medium' offering")
                s.assert_true(
                    self.verifier.verify_vm_offering(
                        "db", medium_id),
                    "DB VM scaled to 'medium' offering")

            # Verify disk sizes grew
            s.assert_equal(
                self.verifier.get_volume_size_gb(f"{NETWORK_NAME}-webdata"),
                30, "Web volume grew to 30GB")
            s.assert_equal(
                self.verifier.get_volume_size_gb(f"{NETWORK_NAME}-dbdata"),
                25, "DB volume grew to 25GB")

            # Verify all VMs are Running after scale
            web = self.verifier.verify_vm_exists("web")
            s.assert_true(web is not None and web.get("state") == "Running",
                          "Web VM is Running after scale")
            db = self.verifier.verify_vm_exists("db")
            s.assert_true(db is not None and db.get("state") == "Running",
                          "DB VM is Running after scale")

        self.scenarios.append(s)

    def _phase3_teardown(self):
        s = TestScenario("8. Teardown Verify")
        with s:
            self.teardown()

            s.assert_true(self.verifier.verify_network_absent(),
                          "Network gone")
            s.assert_true(self.verifier.verify_keypair_absent(),
                          "Keypair gone")
            s.assert_true(
                self.verifier.verify_vm_absent("web"),
                "Web VM gone")
            s.assert_true(
                self.verifier.verify_vm_absent("db"),
                "DB VM gone")
            s.assert_true(
                self.verifier.verify_vm_absent(f"worker-1"),
                "Worker-1 VM gone")
            s.assert_true(self.verifier.verify_no_tagged_volumes(),
                          "No tagged volumes remain")

        self.scenarios.append(s)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print(f"{'#' * 60}")
    print(f"# Infrastructure Test Suite")
    print(f"# Network: {NETWORK_NAME}")
    print(f"# Zone:    {ZONE}")
    print(f"{'#' * 60}")

    runner = TestRunner()
    all_passed = runner.run_all()

    total_pass = sum(
        sum(1 for a in s.assertions if a["passed"])
        for s in runner.scenarios
    )
    total_fail = sum(
        sum(1 for a in s.assertions if not a["passed"])
        for s in runner.scenarios
    )

    print(f"\n{'#' * 60}")
    print(f"# FINAL: {total_pass} passed, {total_fail} failed")
    print(f"{'#' * 60}")

    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
