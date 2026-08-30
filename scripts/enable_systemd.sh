#!/bin/bash
# Enable systemd in WSL2
sudo sed -i 's/systemd=false/systemd=true/' /etc/wsl.conf
echo "Updated /etc/wsl.conf:"
cat /etc/wsl.conf
echo ""
echo "WSL needs to be restarted for systemd to take effect."
echo "Run: wsl.exe --shutdown"
echo "Then restart WSL."
