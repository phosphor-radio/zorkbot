# Raspberry Pi deployment

Deploy the full **game** + **zorkbot** stack on a 64-bit Raspberry Pi with Docker Compose.

## Prerequisites

- Raspberry Pi OS (64-bit) or another `linux/arm64` distro
- Docker Engine and Docker Compose plugin
- MeshCore radio on USB serial
- `zork1.z3` from [historicalsource/zork1](https://github.com/historicalsource/zork1)

## 1. Install Docker

On Raspberry Pi OS:

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"
```

Log out and back in so the `docker` group applies.

## 2. Clone and configure

```bash
git clone https://github.com/phosphor-radio/zorkbot.git
cd zorkbot

cp /path/to/zork1.z3 games/zork1.z3
cp zorkbot/zorkbot.toml.example zorkbot/zorkbot.toml
cp .env.example .env
```

Edit `.env`:

- Set `ADMIN_TOKEN` to a long random secret.
- Set `MESHCORE_DEVICE` if your radio is not at `/dev/meshcore`.

Edit `zorkbot/zorkbot.toml`:

- Set `[admin].names` to your mesh name(s).
- Confirm `[channel]` index/name match your `#zork` channel.

`game_url` in TOML should stay `http://game:8080` for Compose (the Docker service name).

## 3. Stable serial device (udev)

USB serial ports often move between `/dev/ttyUSB0` and `/dev/ttyACM0` across reboots. Use a udev symlink:

```bash
# Find the device (with radio plugged in)
ls -l /dev/ttyUSB* /dev/ttyACM* 2>/dev/null

# Get vendor/product IDs
udevadm info -a -n /dev/ttyUSB0 | grep -E '{idVendor}|{idProduct}|{serial}'
```

Edit `deploy/udev/99-meshcore.rules` with your device's `idVendor` and `idProduct`, then install:

```bash
sudo cp deploy/udev/99-meshcore.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger
```

Unplug and replug the radio. Confirm:

```bash
ls -l /dev/meshcore
```

If you skip udev, set `MESHCORE_DEVICE` and `MESHCORE_CONTAINER_DEVICE` in `.env` to the actual path (e.g. `/dev/ttyACM0`).

## 4. Serial permissions

The zorkbot container is added to the host **dialout** group (`MESHCORE_GROUP_GID`, default `20`). On Debian/Raspberry Pi OS:

```bash
getent group dialout
```

If your dialout GID differs, update `MESHCORE_GROUP_GID` in `.env`.

## 5. Start the stack

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f zorkbot
```

The **game** service is only reachable on the Docker network (not published to the LAN). The bot connects to `http://game:8080` internally.

On first start, zorkbot waits for the game health check, applies mesh settings, and listens on the configured `#zork` channel.

## 6. Verify

```bash
# Game health (from the Pi host, via docker exec)
docker compose exec game wget -q -O- http://localhost:8080/health

# Bot logs should show channel subscription
docker compose logs zorkbot | tail
```

On mesh, send `!zork look` on `#zork`.

## Volumes

| Host path | Container | Purpose |
|-----------|-----------|---------|
| `./data/saves` | `/data` (game) | encrusted save files |
| `./games/zork1.z3` | `/game/zork1.z3` (game) | Zork I story file (read-only) |
| `./zorkbot/zorkbot.toml` | `/app/zorkbot.toml` (zorkbot) | bot config (read-only) |

Protect save data on the Pi:

```bash
chmod 700 data/saves
```

## Updates

```bash
git pull
docker compose up -d --build
```

## Local development (publish game port)

To expose the game API on `localhost:8080` for curl or `--simulate`:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build game
```

## Troubleshooting

| Symptom | Check |
|---------|--------|
| `set ADMIN_TOKEN in .env` | Copy `.env.example` to `.env` and set a token |
| zorkbot can't open serial | `ls -l $MESHCORE_DEVICE`, udev symlink, dialout GID |
| game unhealthy | `docker compose logs game`, confirm `games/zork1.z3` exists |
| bot ignores commands | channel index in TOML vs mesh channel slot |
