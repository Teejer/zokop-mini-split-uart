#!/usr/bin/env bash
set -euo pipefail

echo "==> Blacklisting unused kernel modules"
cat > /etc/modprobe.d/blacklist-lowpower.conf <<'EOF'
blacklist snd_bcm2835
blacklist brcmbluetooth
blacklist i2c-bcm2835
blacklist spi-bcm2835
blacklist bcm2835-v4l2
EOF
# NOTE: brcmfmac (WiFi) is intentionally NOT blacklisted.
# NOTE: usbcore autosuspend is intentionally NOT touched.

echo "==> Disabling unneeded systemd services"
for svc in \
  bluetooth \
  bluetooth-obexd \
  avahi-daemon \
  avahi-dnsconfd \
  ModemManager \
  cups
do
  if systemctl list-unit-files "${svc}.service" 2>/dev/null | grep -q "${svc}\.service"; then
    systemctl disable --now "$svc" 2>/dev/null || true
  fi
done

echo "==> Setting CPU governor to ondemand (already default, just confirming)"
echo "ondemand" | tee /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor

# Make governor survive reboot
cat > /etc/systemd/system/cpu-governor.service <<'EOF'
[Unit]
Description=Set CPU governor to ondemand
After=multi-user.target

[Service]
Type=oneshot
ExecStart=/bin/sh -c 'echo ondemand > /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor'
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
systemctl enable cpu-governor.service

echo "==> Applying fstab noatime + commit=60 to root partition"
# Find the root mount line and update it
ROOT_DEV=$(findmnt -n -o SOURCE /)
if ! grep -qE "^\S+\s+/\s+.*noatime" /etc/fstab; then
  sed -i "s|^\(\S*\s*/\s*\S*\s*\(ext4\|ext3\)\s*\)\([^ ]*\)|\1 noatime,commit=60|" /etc/fstab
  echo "    Updated fstab for $ROOT_DEV"
else
  echo "    fstab already has noatime — skipping"
fi

echo "==> Done."
echo ""
echo "Reboot to pick up config.txt changes:"
echo "  sudo reboot"
