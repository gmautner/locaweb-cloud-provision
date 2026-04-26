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

## Solution

### Principle

Force **all** DNS queries through the CloudStack virtual router by overriding the DHCP-provided DNS configuration at the `systemd-networkd` level. The virtual router resolves `.internal` hostnames directly and forwards external queries upstream. No public DNS server is ever consulted.

### How it works

A `systemd-networkd` drop-in overrides two settings on the primary network interface:

1. **`[DHCPv4] UseDNS=false`** — Prevents DHCP from injecting public DNS servers (`186.202.26.26`, `186.202.27.27`) into the link configuration.
2. **`[Network] DNS=<GATEWAY>`** — Sets the virtual router as the sole DNS server for the link.

After the change, `resolvectl status` shows:

```
Link 2 (eth0)
    DNS Servers: 10.1.1.1
     DNS Domain: preview.zp01.internal
```

Query routing:

| Query | Server | Result |
|-------|--------|--------|
| `db.preview.zp01.internal` | `10.1.1.1` (virtual router) | Resolves correctly |
| `google.com` | `10.1.1.1` (virtual router forwards upstream) | Resolves correctly |
| `10.1.1.1` timeout | Retries `10.1.1.1` (no alternative to fail over to) | Retries — never falls back to public |

### Implementation

Changes in `scripts/userdata/web_vm.sh`, `scripts/userdata/worker_vm.sh`, and `scripts/userdata/accessory_vm.sh`:

**1. Install `jq` (for structured JSON parsing of network data):**

```bash
apt-get install -y -qq fail2ban jq
```

**2. Configure DNS override (at the end of the script, after network is up):**

```bash
# --- Force all DNS queries through the CloudStack virtual router ---
GATEWAY=$(ip -4 -json route show default | jq -r '.[0].gateway')
IFACE=$(ip -4 -json route show default | jq -r '.[0].dev')

NETFILE=$(ls /etc/systemd/network/*.network 2>/dev/null | head -1)
if [ -n "$NETFILE" ]; then
  DROPIN_DIR="${NETFILE}.d"
  mkdir -p "$DROPIN_DIR"
  cat > "$DROPIN_DIR/dns-override.conf" << EOF
[DHCPv4]
UseDNS=false

[Network]
DNS=${GATEWAY}
EOF
  networkctl reload && networkctl reconfigure "$IFACE"
fi
```

### Why this works

- Public DNS servers are never configured on the link, so `systemd-resolved` cannot fail over to them. The root cause is eliminated rather than worked around.
- The virtual router handles both internal and external DNS. Internal names resolve directly; external names are forwarded upstream by the router.
- If the virtual router times out, `systemd-resolved` retries it (no alternative server to fail over to). This is correct — a public server can never resolve internal names.
- The gateway IP and interface name are extracted from `ip -json` (structured output), not from string parsing.
- The networkd drop-in survives reboots and DHCP renewals.
- `networkctl reload && networkctl reconfigure` applies the override immediately without a full service restart.

### Rollout

This change only takes effect on newly provisioned VMs (userdata runs at first boot). Existing VMs need a one-time manual fix or a redeploy that recreates the VM.

To apply to an existing VM without reprovisioning:

```bash
GATEWAY=$(ip -4 -json route show default | jq -r '.[0].gateway')
IFACE=$(ip -4 -json route show default | jq -r '.[0].dev')
NETFILE=$(ls /etc/systemd/network/*.network 2>/dev/null | head -1)
if [ -n "$NETFILE" ]; then
  DROPIN_DIR="${NETFILE}.d"
  mkdir -p "$DROPIN_DIR"
  cat > "$DROPIN_DIR/dns-override.conf" << EOF
[DHCPv4]
UseDNS=false

[Network]
DNS=${GATEWAY}
EOF
  networkctl reload && networkctl reconfigure "$IFACE"
fi
```

Also remove the old resolved drop-in if present:

```bash
rm -f /etc/systemd/resolved.conf.d/internal-dns.conf
systemctl restart systemd-resolved
```
