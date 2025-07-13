# 🌀 Warp Plus Docker

A self-hosted Docker container for running [warp-plus](https://github.com/bepass-org/warp-plus) with flexible configuration via environment variables. Supports dynamic generation of `config.json`, routing through a local SOCKS5 proxy, country selection (with exclusions), gool/psiphon modes, endpoint scanning, and more.

---

## 📦 Quick Start

```yaml
services:
  warp:
    container_name: warp
    image: bigbugcc/warp-plus:latest
    restart: unless-stopped

    ports:
      - "1080:1080"     # Expose SOCKS5 proxy

    environment:
      KEY: ""                    # Optional Warp+ license key
      ENDPOINT: ""              # Specific Warp endpoint
      BIND: "0.0.0.0:1080"      # SOCKS5 listen address
      COUNTRY: ""
      EXCLUDE_COUNTRY: "RU CN"

      VERBOSE: "false"
      GOOL: "false"
      CFON: "false"
      SCAN: "true"
      RTT: "1s"
      DNS: "1.1.1.1"
      CACHE_DIR: "/etc/warp/cache/"
      FWMARK: "0x1375"
      WGCONF: ""
      TEST_URL: ""

      IPV4: "true"
      IPV6: "false"

      # 🔍 Optional internal healthcheck config
      HEALTHCHECK_INTERVAL: "300"
      HEALTHCHECK_TIMEOUT: "30"
      HEALTHCHECK_INITIAL_DELAY: "60"
      HEALTHCHECK_MAX_FAILURES: "3"
      HEALTHCHECK_URL: "https://ifconfig.me"

    volumes:
      - ./warp-data:/etc/warp

    healthcheck:
      test: ["CMD", "curl", "--socks5", "localhost:1080", "--max-time", "5", "https://ifconfig.me"]
      interval: 30s
      timeout: 10s
      retries: 3
```

Once launched, the SOCKS5 proxy will be available at `127.0.0.1:1080`.

---

## 🔍 Verify Your Proxy IP

To check which IP is being used via the proxy:

```bash
curl --socks5 127.0.0.1:1080 https://ifconfig.me
```

---

## ⚙️ Environment Variables

> ℹ️ If `COUNTRY` is not set, a random one will be selected from the supported list, excluding any countries specified in `EXCLUDE_COUNTRY`.

| Variable          | Type   | Default            | Description                                                          |
| ----------------- | ------ | ------------------ | -------------------------------------------------------------------- |
| `KEY`             | string | *(empty)*          | 🔑 Warp+ license key *(optional but required for actual traffic)*    |
| `ENDPOINT`        | string | *(empty)*          | Custom Warp endpoint (e.g., `162.159.192.1:2408`)                    |
| `BIND`            | string | `127.0.0.1:1080`   | SOCKS5 proxy listen address                                          |
| `VERBOSE`         | bool   | `false`            | Enable verbose logs                                                  |
| `DNS`             | string | `1.1.1.1`          | DNS resolver                                                         |
| `GOOL`            | bool   | `false`            | Warp-in-Warp mode                                                    |
| `CFON`            | bool   | `false`            | Psiphon mode (must set `COUNTRY`)                                    |
| `COUNTRY`         | string | random or `"AT"`   | Country ISO code (from a supported list)                             |
| `EXCLUDE_COUNTRY` | string | *(empty)*          | Space/Comma-separated list of ISO codes to exclude (e.g. `US CN RU`) |
| `SCAN`            | bool   | `true`             | Enable endpoint scanning                                             |
| `RTT`             | string | `1s`               | RTT threshold for scanner                                            |
| `CACHE_DIR`       | string | `/etc/warp/cache/` | Directory to store endpoint cache                                    |
| `FWMARK`          | string | `0x1375`           | Linux fwmark for routing in tun-mode                                 |
| `WGCONF`          | string | *(empty)*          | Path to a WireGuard config file                                      |
| `IPV4`            | bool   | `true`             | Use only IPv4 (mutually exclusive with `IPV6`)                       |
| `IPV6`            | bool   | `false`            | Use only IPv6 (mutually exclusive with `IPV4`)                       |

### ❤️ Internal Healthcheck Options

These control the built-in background check that monitors if the container remains connected and valid.

| Variable                    | Default               | Description                                                       |
| --------------------------- | --------------------- | ----------------------------------------------------------------- |
| `HEALTHCHECK_INTERVAL`      | `300`                 | Interval between health checks (in seconds)                       |
| `HEALTHCHECK_TIMEOUT`       | `30`                  | Timeout for each check (in seconds)                               |
| `HEALTHCHECK_INITIAL_DELAY` | `60`                  | Delay before the first check after container startup (in seconds) |
| `HEALTHCHECK_MAX_FAILURES`  | `3`                   | Restart the container if any upstream reports `fails >= N`        |
| `HEALTHCHECK_URL`           | `https://ifconfig.me` | The URL used to test proxy connectivity                           |

ℹ️ This is **independent** of Docker's `healthcheck:` stanza — it runs inside the container and can terminate the main process to trigger restart.
