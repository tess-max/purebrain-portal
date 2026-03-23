# PureBrain Portal — MAKR Fund OS

AI-powered venture fund operating system. Single-file portal with deal flow, portfolio management, Fund Brain map, My Brain persona views, and an AI chat interface.

## What's inside

- **Deal Flow Hub** — pipeline swimlanes, sourcing intelligence, thesis coverage, agents & templates
- **Portfolio Hub** — portco KPIs, lifecycle tracking, health flags, pillar engagement
- **Fund Brain** — SVG network graph mapping deals → portcos → pillars → exits
- **My Brain** — 7 persona views (GP, Principal, Associate, Advisor, MVP/Alliance/Multiplier leads)
- **Ask Fund Brain** — keyword-aware AI chat pulling live data from portfolio & pipeline

## Install (one command)

```bash
curl -fsSL https://raw.githubusercontent.com/coreycottrell/purebrain-portal/main/install.sh | bash
```

This will:
1. Download the portal to `~/purebrain_portal/`
2. Start a local server on port 8097
3. Print the URL to open

## Manual install

```bash
git clone https://github.com/coreycottrell/purebrain-portal.git
cd purebrain-portal
python3 portal_server.py
```

Then open: `http://localhost:8097/makr-os`

## Deploy to Cloudflare Pages (recommended)

```bash
# Install wrangler
npm install -g wrangler

# Deploy
npx wrangler pages deploy . --project-name=makr-os
```

## For AI civilizations

If your AI is running Claude Code, tell it:

> "Clone https://github.com/coreycottrell/purebrain-portal and run install.sh to set up the portal"

The portal is a single HTML file (`makr-os.html`) — no dependencies, no build step, no database.
To customise it for your fund, edit the data constants at the top of the script block:
- `PH_PORTCOS` — your portfolio companies
- `DFH_PIPELINE_DATA` — your deal pipeline
- `MB_PERSONAS` — your team personas

## Stack

- Pure HTML/CSS/JS — zero dependencies
- Cloudflare Pages for hosting (free)
- All data is in-file (no backend required)
