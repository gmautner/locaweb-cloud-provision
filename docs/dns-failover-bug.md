# DNS Failover Bug — Internal hostname resolution breaks silently

## Symptom

Applications deployed via Kamal return "internal error" when trying to reach accessories on other VMs (e.g., the database). The Go backend logs show:

```
ERROR failed to resolve role err="failed to connect to `user=postgres database=postgres`:
    hostname resolving error: lookup db on 127.0.0.11:53: server misbehaving"
```

The app works for hours or days after deploy, then stops without any code change or deploy.

## Diagnosis

### DNS architecture on CloudStack isolated networks

Each isolated network has a virtual router (`10.1.1.1` — always the gateway) that provides:

- DHCP (IP, gateway, DNS servers, search domain)
- Internal DNS (resolves VM hostnames within the network)
- NAT for internet access

The VMs receive three DNS servers via DHCP:

| Server | Type | Resolves internal names? |
|--------|------|--------------------------|
| `10.1.1.1` | Virtual router (internal) | Yes |
| `186.202.26.26` | Locaweb public | No |
| `186.202.27.27` | Locaweb public | No |

All three are placed on the same `systemd-resolved` link (eth0). The search domain (e.g., `preview.zp01.internal`) is configured as a **search domain** (no `~` prefix), not a routing domain.

### The failover problem

`systemd-resolved` treats all three DNS servers as equivalent. It maintains a "Current DNS Server" per link and sends all queries to it. If the current server times out, it switches to the next one **and never switches back** (as long as the new server responds to other queries).

Timeline of a typical failure:

1. VM boots. `systemd-resolved` starts with Current DNS Server = `10.1.1.1`
2. App deploys, connects to `db` successfully (`10.1.1.1` resolves `db.preview.zp01.internal` → `10.1.1.185`)
3. A DNS query to `10.1.1.1` times out (transient — `resolvectl statistics` shows `Total Timeouts: 1`)
4. `systemd-resolved` switches Current DNS Server to `186.202.26.26`
5. All subsequent queries go to `186.202.26.26`, including `db.preview.zp01.internal`
6. Public DNS returns **NXDOMAIN** (authoritative "does not exist") — resolved accepts this as final, does not try other servers
7. App can no longer reach the database

### Why NXDOMAIN prevents self-recovery

When the current server returns a **timeout**, `systemd-resolved` tries the next server. But when it returns **NXDOMAIN**, that's a valid DNS response meaning "this name does not exist." The resolver accepts it and stops — it does not retry on another server. Since the public DNS will always return NXDOMAIN for `.internal` names, the situation never self-corrects.

### Evidence collected (2026-04-13)

| Check | Result |
|-------|--------|
| `resolvectl status` → Current DNS Server | `186.202.26.26` (public) instead of `10.1.1.1` (internal) |
| `resolvectl statistics` → Total Timeouts | 1 (the failover trigger) |
| `dig @10.1.1.1 db.preview.zp01.internal` | `10.1.1.185` — works |
| `dig @186.202.26.26 db.preview.zp01.internal` | NXDOMAIN — expected |
| Container `/etc/hosts` | No entry for `db` |
| Container `ExtraHosts` | `null` (Kamal does not add `--add-host` for remote accessories) |
| `resolvectl domain eth0` | `preview.zp01.internal` (search domain, no `~` prefix) |

### Temporary fix applied

Restarting `systemd-resolved` resets the Current DNS Server back to `10.1.1.1`:

```bash
systemctl restart systemd-resolved
```

This is not a permanent fix — the next transient timeout will trigger the same failover.

## Proposed solution

### Principle

Route all `.internal` DNS queries to a dedicated global scope that only has the CloudStack virtual router as its DNS server. Leave the DHCP-provided configuration on eth0 untouched for all other queries.

### How it works

`systemd-resolved` supports a **global** DNS scope configured via `/etc/systemd/resolved.conf.d/`. When a routing domain (prefixed with `~`) is set in the global scope, matching queries go to the global scope's DNS servers — bypassing eth0 entirely.

After the change, `resolvectl status` shows:

```
Global
    DNS Servers: 10.1.1.1
     DNS Domain: ~internal

Link 2 (eth0)                          ← unchanged, DHCP-provided
    DNS Servers: 10.1.1.1 186.202.26.26 186.202.27.27
     DNS Domain: preview.zp01.internal
```

Query routing:

| Query | Scope | Server | Result |
|-------|-------|--------|--------|
| `db.preview.zp01.internal` | Global (`~internal` matches) | `10.1.1.1` | Resolves correctly |
| `google.com` | eth0 (DefaultRoute, no routing match) | Current on eth0 | Resolves correctly |
| `10.1.1.1` timeout for `.internal` | Global (retries, no fallback to public) | `10.1.1.1` | Retries — never falls back to public |

### Implementation

Changes in `scripts/userdata/web_vm.sh` and `scripts/userdata/worker_vm.sh`:

**1. Install `jq` (for structured JSON parsing of network data):**

```bash
apt-get install -y -qq fail2ban jq
```

**2. Configure DNS routing (at the end of the script, after network is up):**

```bash
# --- Route .internal DNS queries to the CloudStack virtual router ---
# The gateway of the isolated network is always the virtual router,
# which is the only DNS server that resolves internal VM hostnames.
# Without this, systemd-resolved may failover to a public DNS server
# that returns NXDOMAIN for internal names, breaking inter-VM connectivity.
GATEWAY=$(ip -4 -json route show default | jq -r '.[0].gateway')

mkdir -p /etc/systemd/resolved.conf.d
cat > /etc/systemd/resolved.conf.d/internal-dns.conf << EOF
[Resolve]
DNS=${GATEWAY}
Domains=~internal
EOF

systemctl restart systemd-resolved
```

### Why this works

- `.internal` queries go to the global scope, which has **only** the virtual router. No public DNS server can intercept them.
- If the virtual router times out, `systemd-resolved` retries it (no alternative server in the global scope to fail over to). This is correct — a public server can never resolve internal names.
- The DHCP configuration on eth0 is untouched. Public DNS behavior, failover between public servers, and DHCP renewals all work as before.
- The gateway IP is extracted from `ip -json` (structured output), not from string parsing.
- `~internal` matches all CloudStack network domains (`*.preview.zp01.internal`, `*.production.zp01.internal`, etc.) regardless of zone or environment.
- The drop-in in `/etc/systemd/resolved.conf.d/` survives reboots and DHCP renewals.

### Rollout

This change only takes effect on newly provisioned VMs (userdata runs at first boot). Existing VMs need a one-time manual fix or a redeploy that recreates the VM.

To apply to an existing VM without reprovisioning:

```bash
GATEWAY=$(ip -4 -json route show default | jq -r '.[0].gateway')
mkdir -p /etc/systemd/resolved.conf.d
cat > /etc/systemd/resolved.conf.d/internal-dns.conf << EOF
[Resolve]
DNS=${GATEWAY}
Domains=~internal
EOF
systemctl restart systemd-resolved
```
