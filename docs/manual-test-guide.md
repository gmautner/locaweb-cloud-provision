# Manual Test Guide

This guide translates the automated E2E test suite (`scripts/e2e_test.py`) into
human-readable steps you can reproduce through the cofounder agent in `~/marketplace`.

Each scenario is self-contained: deploy, verify, teardown. Run them in order or
pick individual ones. Every assertion from the automated suite is listed as a
checkbox you can tick off.

---

## Prerequisites

- A GitHub repository at `~/marketplace` with the cofounder plugin installed.
- The repository has a `Dockerfile` at the root, listening on port 80 with a
  `GET /up` health check.
- GitHub secrets are configured: `CLOUDSTACK_API_KEY`, `CLOUDSTACK_SECRET_KEY`,
  `SSH_PRIVATE_KEY`, and `POSTGRES_PASSWORD`.
- You have SSH access via `~/.ssh/<repo-name>` (or the key the cofounder generated).

---

## Scenario 1: Complete Deploy (web + 1 worker + db + nginx)

**Goal:** Verify a full-stack deployment with all component types, including an
accessory whose port is initially blocked, then opened on redeploy.

### 1.1 Deploy

Ask the cofounder to deploy with:

- **Environment:** preview
- **Zone:** ZP01
- **Workers:** 1
- **Accessories:** db (medium plan, 20 GB disk) and nginx (micro plan, 20 GB disk, no ports)
- **Automatic reboot:** enabled, at 03:30 UTC

### 1.2 Verify deployment output

- [ ] Deploy produced a `web_ip`.
- [ ] Deploy produced at least 1 `worker_ip`.
- [ ] Deploy produced a `db` accessory IP.
- [ ] Deploy produced an `nginx` accessory IP.

### 1.3 Verify nginx port is blocked (negative test)

Because nginx was deployed without `ports` in the accessories config, its port 80
should be unreachable from the internet.

- [ ] `curl http://<nginx_ip>/` times out or connection refused.

### 1.4 Verify HTTP health and page content

```bash
curl -s -o /dev/null -w "%{http_code}" http://<web_ip>.nip.io/up
curl -s http://<web_ip>.nip.io/
```

- [ ] `GET /up` returns HTTP 200.
- [ ] `GET /` returns HTTP 200.
- [ ] Page does **not** contain "not set" (environment variables are populated).
- [ ] Page does **not** contain "Database not configured" (DB is connected).
- [ ] Page does **not** contain "Database unavailable" (DB is reachable).

### 1.5 Verify database write/read

```bash
curl -X POST -d "content=manual-test-note-$(date +%s)" http://<web_ip>.nip.io/notes
curl -s http://<web_ip>.nip.io/
```

- [ ] `POST /notes` returns 200 or 302.
- [ ] The note text appears on the page after refresh.

### 1.6 Verify file upload (blob storage)

```bash
curl -X POST -F "file=@/tmp/test-upload.txt" http://<web_ip>.nip.io/upload
curl -s http://<web_ip>.nip.io/
```

- [ ] `POST /upload` returns 200 or 302.
- [ ] The uploaded filename appears on the page after refresh.

### 1.7 Verify SSH, mounts, and disk sizes on web VM

```bash
ssh -i ~/.ssh/<repo-name> root@<web_ip>
mountpoint -q /data/
echo "test" > /data/.manual-test && cat /data/.manual-test && rm /data/.manual-test
blockdev --getsize64 /dev/$(lsblk -rno NAME,MOUNTPOINT | grep '/data/$' | awk '{print $1}')
```

- [ ] SSH to web VM succeeds.
- [ ] `/data/` is a mount point.
- [ ] `/data/` is writable (write/read/delete test file).
- [ ] Disk size is 21474836480 bytes (20 GB).

### 1.8 Verify SSH and mounts on DB VM

```bash
ssh -i ~/.ssh/<repo-name> root@<db_ip>
mountpoint -q /data/
blockdev --getsize64 /dev/$(lsblk -rno NAME,MOUNTPOINT | grep '/data/$' | awk '{print $1}')
```

- [ ] SSH to DB VM succeeds.
- [ ] `/data/` is a mount point.
- [ ] Disk size is 21474836480 bytes (20 GB).

### 1.9 Verify worker VM containers and environment

For each worker IP:

```bash
ssh -i ~/.ssh/<repo-name> root@<worker_ip>
CONTAINER=$(docker ps --format '{{.Names}}' | grep -v kamal-proxy | head -1)
docker exec $CONTAINER printenv MY_VAR
docker exec $CONTAINER printenv MY_SECRET
```

- [ ] SSH to worker VM succeeds.
- [ ] An app container is running (not just `kamal-proxy`).
- [ ] `MY_VAR` is set and non-empty inside the container.
- [ ] `MY_SECRET` is set and non-empty inside the container.

### 1.10 Verify unattended upgrades on all VMs

On each VM (web, worker(s), db, nginx):

```bash
cat /etc/apt/apt.conf.d/20auto-upgrades
cat /etc/apt/apt.conf.d/52-automatic-reboots
```

- [ ] `20auto-upgrades` contains `Update-Package-Lists "1"` and `Unattended-Upgrade "1"`.
- [ ] `52-automatic-reboots` contains `Automatic-Reboot "true"` and `Automatic-Reboot-Time "03:30"`.

### 1.11 Verify fail2ban on all VMs

On each VM (web, worker(s), db, nginx):

```bash
fail2ban-client status sshd
fail2ban-client get sshd maxretry
fail2ban-client get sshd bantime
fail2ban-client get sshd findtime
```

- [ ] sshd jail is active.
- [ ] `maxretry` = 3.
- [ ] `bantime` = 3600.
- [ ] `findtime` = 600.

### 1.12 Verify snapshot policies

Use CloudMonkey (`cmk`) or ask the cofounder to check:

```bash
cmk list snapshotpolicies volumeid=<web_volume_id>
cmk list snapshotpolicies volumeid=<db_volume_id>
```

For each volume (web and db):

- [ ] At least one snapshot policy exists.
- [ ] Policy has `locaweb-cloud-provision-id` tag matching the network name.
- [ ] Policy covers both ZP01 and ZP02 zones.

### 1.13 Redeploy nginx with port 80 open (positive test)

Ask the cofounder to redeploy with the same configuration but change the nginx
accessory to include `ports: "80"`.

```bash
curl -s -o /dev/null -w "%{http_code}" http://<nginx_ip>/
```

- [ ] `GET /` to nginx now returns HTTP 200 (nginx welcome page).

### 1.14 Teardown

Ask the cofounder to teardown the preview environment.

---

## Scenario 2: Web-Only Deploy (no workers, no accessories)

**Goal:** Verify the minimal deployment: a single web VM with no database and no workers.

### 2.1 Deploy

Ask the cofounder to deploy with:

- **Environment:** preview
- **Zone:** ZP01
- **Workers:** 0
- **Accessories:** none (`[]`)
- **Automatic reboot:** disabled

### 2.2 Verify deployment output

- [ ] Deploy produced a `web_ip`.
- [ ] No `worker_ips` in output (empty or absent).
- [ ] No `accessories` in output (empty or absent).

### 2.3 Verify HTTP health and page content

```bash
curl -s -o /dev/null -w "%{http_code}" http://<web_ip>.nip.io/up
curl -s http://<web_ip>.nip.io/
```

- [ ] `GET /up` returns HTTP 200 (works without DB).
- [ ] `GET /` returns HTTP 200.
- [ ] Page contains "Database not configured".
- [ ] Page does **not** contain "not set" (env vars are still populated).

### 2.4 Verify file upload still works

```bash
curl -X POST -F "file=@/tmp/test-upload.txt" http://<web_ip>.nip.io/upload
curl -s http://<web_ip>.nip.io/
```

- [ ] `POST /upload` returns 200 or 302.
- [ ] Uploaded filename appears on the page.

### 2.5 Verify SSH and data mount

```bash
ssh -i ~/.ssh/<repo-name> root@<web_ip>
mountpoint -q /data/
```

- [ ] SSH to web VM succeeds.
- [ ] `/data/` is a mount point.

### 2.6 Verify unattended upgrades (reboot disabled)

```bash
cat /etc/apt/apt.conf.d/20auto-upgrades
test ! -f /etc/apt/apt.conf.d/52-automatic-reboots && echo "OK: no reboot file"
```

- [ ] `20auto-upgrades` contains auto-upgrade directives.
- [ ] `52-automatic-reboots` does **not** exist (reboot was disabled).

### 2.7 Verify fail2ban

```bash
fail2ban-client status sshd
fail2ban-client get sshd maxretry
fail2ban-client get sshd bantime
fail2ban-client get sshd findtime
```

- [ ] sshd jail is active.
- [ ] `maxretry` = 3.
- [ ] `bantime` = 3600.
- [ ] `findtime` = 600.

### 2.8 Verify snapshot policy on web volume

- [ ] Snapshot policy exists on the web volume.
- [ ] Policy tag matches the network name.
- [ ] Policy covers both ZP01 and ZP02 zones.

### 2.9 Teardown

Ask the cofounder to teardown the preview environment.

---

## Scenario 3: Scale Up (1 worker → 3 workers) + Offerings, Disks & TLS

**Goal:** Verify scaling workers up, changing VM plans (small → medium), growing
disks, and TLS with a custom domain via Let's Encrypt.

### 3.1 Initial deploy (small plans, 1 worker)

Ask the cofounder to deploy with:

- **Environment:** e2etest (or a separate environment name)
- **Zone:** ZP01
- **Web plan:** small
- **Web disk size:** 25 GB
- **Workers:** 1, plan: small
- **Accessories:** db (small plan, 20 GB disk)
- **Custom domain:** your test domain (e.g. `e2e.kamal.giba.tech`)

After deploy, create a DNS A record pointing the domain to `<web_ip>`.

### 3.2 Verify initial deploy

- [ ] Deploy produced a `web_ip`.
- [ ] Deploy has exactly 1 worker.

### 3.3 Verify HTTPS via custom domain

```bash
curl -s -o /dev/null -w "%{http_code}" https://<domain>/up
```

- [ ] `HTTPS GET /up` returns 200.

### 3.4 Verify initial disk sizes

```bash
# On web VM:
blockdev --getsize64 /dev/$(lsblk -rno NAME,MOUNTPOINT | grep '/data/$' | awk '{print $1}')
# On DB VM:
blockdev --getsize64 /dev/$(lsblk -rno NAME,MOUNTPOINT | grep '/data/$' | awk '{print $1}')
```

- [ ] SSH to web VM succeeds and `/data/` is mounted.
- [ ] Web disk is 26843545600 bytes (25 GB).
- [ ] SSH to DB VM succeeds and `/data/` is mounted.
- [ ] DB disk is 21474836480 bytes (20 GB).

### 3.5 Verify initial snapshot policies

- [ ] Snapshot policy exists on the web volume with correct tag and zone coverage.
- [ ] Snapshot policy exists on the DB volume with correct tag and zone coverage.

### 3.6 Verify initial worker

- [ ] Worker has an app container running.

### 3.7 Verify initial VM offerings are "small"

```bash
cmk list virtualmachines id=<web_vm_id> filter=serviceofferingname
cmk list virtualmachines id=<worker_vm_id> filter=serviceofferingname
cmk list virtualmachines id=<db_vm_id> filter=serviceofferingname
```

- [ ] Web VM offering is "small".
- [ ] Worker VM offering is "small".
- [ ] DB VM offering is "small".

### 3.8 Scale up: redeploy with 3 workers, medium plans, larger disks

Ask the cofounder to redeploy with:

- **Web plan:** medium
- **Web disk size:** 35 GB
- **Workers:** 3, plan: medium
- **Accessories:** db (medium plan, 30 GB disk)

### 3.9 Verify scaled workers

- [ ] Deploy now has exactly 3 workers.

For each of the 3 worker IPs:

- [ ] SSH succeeds.
- [ ] An app container is running.
- [ ] `MY_VAR` is set and non-empty inside the container.

### 3.10 Verify VM offerings changed to "medium"

```bash
cmk list virtualmachines id=<web_vm_id> filter=serviceofferingname
cmk list virtualmachines id=<worker_1_vm_id> filter=serviceofferingname
cmk list virtualmachines id=<db_vm_id> filter=serviceofferingname
```

- [ ] Web VM offering is "medium".
- [ ] Worker-1 VM offering is "medium".
- [ ] DB VM offering is "medium".

### 3.11 Verify disks grew

```bash
# On web VM:
blockdev --getsize64 /dev/$(lsblk -rno NAME,MOUNTPOINT | grep '/data/$' | awk '{print $1}')
# On DB VM:
blockdev --getsize64 /dev/$(lsblk -rno NAME,MOUNTPOINT | grep '/data/$' | awk '{print $1}')
```

- [ ] Web disk grew to 37580963840 bytes (35 GB).
- [ ] DB disk grew to 32212254720 bytes (30 GB).

### 3.12 Verify TLS certificate

```bash
echo | openssl s_client -connect <domain>:443 -servername <domain> 2>/dev/null | openssl x509 -noout -text
```

- [ ] Certificate was retrieved successfully.
- [ ] Subject Alternative Name (SAN) contains the custom domain.
- [ ] Certificate issuer organization is "Let's Encrypt".

### 3.13 Verify app still healthy after scale

```bash
curl -s -o /dev/null -w "%{http_code}" https://<domain>/up
```

- [ ] `HTTPS GET /up` returns 200.

### 3.14 Verify unattended upgrades on all VMs (default reboot time)

On each VM (web, 3 workers, db):

- [ ] `20auto-upgrades` contains auto-upgrade directives.
- [ ] `52-automatic-reboots` contains `Automatic-Reboot-Time "05:00"` (default).

### 3.15 Teardown

Ask the cofounder to teardown the e2etest environment. Clean up the DNS record.

---

## Scenario 4: Scale Down (3 workers → 1 worker)

**Goal:** Verify that reducing worker count correctly removes extra VMs while
keeping the remaining infrastructure healthy.

### 4.1 Initial deploy with 3 workers

Ask the cofounder to deploy with:

- **Environment:** preview
- **Zone:** ZP01
- **Workers:** 3
- **Accessories:** db (medium plan, 20 GB disk)

### 4.2 Verify initial deploy

- [ ] Deploy produced a `web_ip`.
- [ ] Deploy has exactly 3 workers.

### 4.3 Verify snapshot policies (initial)

- [ ] Snapshot policy exists on web volume.
- [ ] Snapshot policy exists on DB volume.

### 4.4 Verify initial health and workers

```bash
curl -s -o /dev/null -w "%{http_code}" http://<web_ip>.nip.io/up
```

- [ ] `GET /up` returns HTTP 200.

For each of the 3 worker IPs:

- [ ] An app container is running.

### 4.5 Scale down to 1 worker

Ask the cofounder to redeploy with:

- **Workers:** 1

### 4.6 Verify scaled-down output

- [ ] Deploy now has exactly 1 worker.

### 4.7 Verify remaining worker

```bash
ssh -i ~/.ssh/<repo-name> root@<remaining_worker_ip>
CONTAINER=$(docker ps --format '{{.Names}}' | grep -v kamal-proxy | head -1)
docker exec $CONTAINER printenv MY_VAR
```

- [ ] SSH to remaining worker succeeds.
- [ ] App container is running.
- [ ] `MY_VAR` is set and non-empty inside the container.

### 4.8 Verify app still healthy

```bash
curl -s -o /dev/null -w "%{http_code}" http://<web_ip>.nip.io/up
```

- [ ] `GET /up` returns HTTP 200 after scale down.

### 4.9 Teardown

Ask the cofounder to teardown the preview environment.

---

## Scenario 5: Disaster Recovery (cross-zone)

**Goal:** Verify that an application deployed in ZP01 can be recovered to ZP02
from snapshots, with all data intact. Also verify that same-zone recovery is
correctly rejected.

### 5.1 Deploy to ZP01 with web + db

Ask the cofounder to deploy with:

- **Environment:** preview
- **Zone:** ZP01
- **Workers:** 0
- **Accessories:** db (medium plan, 20 GB disk)

### 5.2 Verify deploy

- [ ] Deploy produced a `web_ip`.
- [ ] Deploy produced a `db_ip`.

### 5.3 Verify snapshot policies

- [ ] Snapshot policy exists on web volume with correct tag and zone coverage.
- [ ] Snapshot policy exists on DB volume with correct tag and zone coverage.

### 5.4 Insert sample data

```bash
curl -s -o /dev/null -w "%{http_code}" http://<web_ip>.nip.io/up
```

- [ ] `GET /up` returns HTTP 200.

```bash
curl -X POST -d "content=recovery-test-note-$(date +%s)" http://<web_ip>.nip.io/notes
curl -X POST -F "file=@/tmp/recovery-test.txt" http://<web_ip>.nip.io/upload
```

- [ ] `POST /notes` returns 200 or 302.
- [ ] `POST /upload` returns 200 or 302.

### 5.5 Verify data is visible

```bash
curl -s http://<web_ip>.nip.io/
```

- [ ] `GET /` returns HTTP 200.
- [ ] The test note text appears on the page.
- [ ] The uploaded filename appears on the page.

### 5.6 Wait and create manual snapshots

Wait at least 60 seconds for kernel disk flush, then create manual snapshots:

```bash
# Create snapshots via cmk (or ask the cofounder)
cmk create snapshot volumeid=<web_volume_id> zoneids=<zp01_id>,<zp02_id>
cmk create tags resourceids=<web_snap_id> resourcetype=Snapshot \
    tags[0].key=locaweb-cloud-provision-id tags[0].value=<network_name>

cmk create snapshot volumeid=<db_volume_id> zoneids=<zp01_id>,<zp02_id>
cmk create tags resourceids=<db_snap_id> resourcetype=Snapshot \
    tags[0].key=locaweb-cloud-provision-id tags[0].value=<network_name>
```

- [ ] Manual web snapshot created.
- [ ] Manual DB snapshot created.

### 5.7 Wait for snapshot replication

Poll with `cmk list snapshots` until snapshots appear in both zones:

- [ ] Web snapshot ready in ZP01 (state=BackedUp).
- [ ] DB snapshot ready in ZP01 (state=BackedUp).
- [ ] Web snapshot replicated to ZP02 (state=BackedUp).
- [ ] DB snapshot replicated to ZP02 (state=BackedUp).

### 5.8 Attempt recovery to same zone (should fail)

Ask the cofounder to deploy with `recover: true` to **ZP01** (the same zone
as the original).

- [ ] Recovery to ZP01 is correctly rejected (workflow fails).

### 5.9 Recover to ZP02

Ask the cofounder to deploy with `recover: true` to **ZP02**.

- [ ] Recovery produced a `web_ip`.
- [ ] Recovery produced a `db_ip`.

### 5.10 Verify recovered app works

```bash
curl -s -o /dev/null -w "%{http_code}" http://<recovered_web_ip>.nip.io/up
curl -s http://<recovered_web_ip>.nip.io/
```

- [ ] `GET /up` returns HTTP 200 (recovered app).
- [ ] `GET /` returns HTTP 200.

### 5.11 Verify data survived recovery

- [ ] The test note text appears on the recovered page.
- [ ] The uploaded filename appears on the recovered page.

### 5.12 Verify recovered snapshot policies

- [ ] Snapshot policy exists on the recovered web volume.
- [ ] Snapshot policy exists on the recovered DB volume.

### 5.13 Teardown both zones

Ask the cofounder to teardown:

1. Preview environment in **ZP02** (recovered).
2. Preview environment in **ZP01** (original).

---

## Assertion Summary

| Scenario | Assertions |
|----------|-----------|
| 1. Complete Deploy | 40+ (output, HTTP, DB, upload, SSH, mounts, disks, workers, upgrades, fail2ban, snapshots, nginx ports) |
| 2. Web-Only | 14 (output, HTTP, no-DB message, upload, SSH, mount, upgrades w/o reboot, fail2ban, snapshot) |
| 3. Scale Up | 30+ (initial deploy, HTTPS, disks, snapshots, offerings small→medium, disk growth, TLS cert, upgrades) |
| 4. Scale Down | 12 (initial 3 workers, snapshots, health, scale to 1, remaining worker, health after scale) |
| 5. Disaster Recovery | 22 (deploy, snapshots, insert data, same-zone rejection, cross-zone recovery, data survival, recovered snapshots) |

---

## Tips for Manual Testing

- **Finding IPs:** After each deploy, the cofounder will show the provision
  output. You can also download the `provision-output` artifact from the
  GitHub Actions run.
- **SSH key:** Use the key the cofounder generated at `~/.ssh/<repo-name>`.
  The SSH user is always `root`.
- **CloudMonkey:** Use `cmk` for CloudStack API queries (VM offerings, volumes,
  snapshots). The cofounder can also run these for you.
- **Disk sizes in bytes:** 20 GB = 21474836480, 25 GB = 26843545600,
  30 GB = 32212254720, 35 GB = 37580963840.
- **Network name pattern:** `<repo-name>-<repo-id>-<env-name>`.
- **Timeouts:** If a health check doesn't respond immediately after deploy,
  wait up to 5 minutes — the container may still be starting.
