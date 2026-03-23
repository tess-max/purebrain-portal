#!/bin/bash
set -e

echo "Installing PureBrain Portal..."

# Clone or update
if [ -d "$HOME/purebrain_portal/.git" ]; then
  echo "Updating existing install..."
  cd "$HOME/purebrain_portal" && git pull
else
  git clone https://github.com/coreycottrell/purebrain-portal.git "$HOME/purebrain_portal"
  cd "$HOME/purebrain_portal"
fi

# Kill any existing portal server
pkill -f portal_server.py 2>/dev/null || true

# Start server in background
nohup python3 portal_server.py > /tmp/portal.log 2>&1 &

sleep 1

echo ""
echo "✅ PureBrain Portal is running!"
echo ""
echo "   Local:  http://localhost:8097/makr-os"
echo "   Server: http://$(curl -s ifconfig.me 2>/dev/null || echo 'YOUR_IP'):8097/makr-os"
echo ""
echo "To deploy to Cloudflare Pages (free, public URL):"
echo "   npx wrangler pages deploy $HOME/purebrain_portal --project-name=makr-os"
