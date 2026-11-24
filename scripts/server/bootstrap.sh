#!/usr/bin/env bash
# One-time setup of a fresh Ubuntu 24.04 server (Oracle Cloud Always Free, Arm or x86).
#
#   curl -fsSL https://raw.githubusercontent.com/BinuShefieldShifani/crewgate-mcp/main/scripts/server/bootstrap.sh -o bootstrap.sh
#   less bootstrap.sh          # read it first
#   sudo bash bootstrap.sh
#
# Installs Docker from Docker's own apt repository, opens ports 80 and 443 in the server's
# firewall (Oracle's Ubuntu images block everything but SSH), turns on automatic security
# updates, adds swap, and clones the repo to /opt/crewgate for the "ubuntu" user.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
user="${SUDO_USER:-ubuntu}"

export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get upgrade -yq
apt-get install -yq ca-certificates curl git openssl unattended-upgrades iptables-persistent

# Docker Engine and the compose plugin, from download.docker.com
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
codename=$(. /etc/os-release && echo "$VERSION_CODENAME")
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $codename stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update -q
apt-get install -yq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
usermod -aG docker "$user"
systemctl enable --now docker

# Oracle's images ship iptables rules that reject everything except SSH. Allow web traffic,
# before the reject rule, and keep it across reboots.
for rule in "-p tcp --dport 80" "-p tcp --dport 443" "-p udp --dport 443"; do
  # shellcheck disable=SC2086
  iptables -C INPUT $rule -m state --state NEW -j ACCEPT 2>/dev/null ||
    iptables -I INPUT 5 $rule -m state --state NEW -j ACCEPT
done
netfilter-persistent save

dpkg-reconfigure -f noninteractive unattended-upgrades

# Swap, so a memory spike (Keycloak starting, an image pull) doesn't kill a container.
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo "/swapfile none swap sw 0 0" >> /etc/fstab
fi

if [[ ! -d /opt/crewgate/.git ]]; then
  git clone https://github.com/BinuShefieldShifani/crewgate-mcp.git /opt/crewgate
fi
chown -R "$user:$user" /opt/crewgate

echo
echo "Done. Log out and back in (for the docker group), then:"
echo "  cd /opt/crewgate && bash scripts/server/make_env.sh <your domain>"
