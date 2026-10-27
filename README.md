Memecoin Launch Signal Bot

An alert-only Discord bot that hunts for newly launched memecoins showing early momentum, runs them through security checks, and posts scored signals to your Discord channel.

No wallets. No trading. No API keys. The bot only reads public data and sends alerts. You make every decision yourself.

Features
Multi-source discovery: DexScreener (token profiles) and GeckoTerminal (new pools)
Multi-chain: Solana, Base, Ethereum, BSC
Security gate: RugCheck (Solana) and GoPlus (EVM chains) check for honeypots, mint/freeze authority, high taxes, LP lock status and more
Signal engine (0-100 score) based on:
Volume surge vs. the coin's own earlier pace
Short-term price jump
Buy/sell pressure
Liquidity growth and liquidity health vs. market cap
Lifetime traction and earliness
Socials / website presence
Penalties for wash-trading, bot-like volume, and chasing coins that already ran
Alert tiers: SPIKE and STRONG
Performance tracking: replies to the original alert at 1.5x, 2x, 3x, 5x, 10x, 25x, 50x and 100x
Rug warnings: flags alerted coins whose liquidity or price collapses
Persistent storage: SQLite for settings, watchlist and alert history
Link buttons: chart, security report, block explorer, GMGN
Requirements
Python 3.10+
A Discord bot token
Installation
bash
git clone https://github.com/YOUR_USERNAME/YOUR_REPO.git
cd YOUR_REPO

python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

pip install discord.py aiohttp python-dotenv
Discord setup
Create an application and bot in the Discord Developer Portal.
Copy the bot token.
Invite the bot with the bot and applications.commands scopes.
Give it permission to Send Messages and Embed Links in your alert channel.
No privileged intents are required.
Configuration

Create a .env file next to the script:

env
DISCORD_TOKEN=your_bot_token
ALERT_CHANNEL_ID=123456789012345678

Both values are required. Everything else is optional.

Optional settings
Variable	Default	Description
WARNING_CHANNEL_ID	alert channel	Separate channel for rug warnings
GUILD_ID	none	Your server ID, for instant slash-command sync
CHAINS	solana,base,ethereum,bsc	Chains to scan
DATA_DIR	.	Where the SQLite database is stored
LOG_LEVEL	INFO	Logging verbosity
SCAN_INTERVAL_SECONDS	20	Delay between scans
GECKO_PAGES_SOLANA	2	GeckoTerminal pages read for Solana
MAX_TRACKED_TOKENS	250	Max coins tracked at once
MAX_PAIR_AGE_MINUTES	30	What counts as a "new" coin
SPIKE_WINDOW_SECONDS	60	Volume and buy/sell window
PRICE_WINDOW_SECONDS	120	Price-jump window
MIN_SPIKE_VOLUME_USD	1000	Minimum volume inside the spike window
MAX_GAIN_SINCE_LAUNCH_PCT	400	Skip coins already up this much
MIN_LIQUIDITY_USD	5000	Minimum liquidity
MIN_TOTAL_TXNS	25	Minimum lifetime transactions
MIN_TOTAL_VOLUME_USD	2000	Minimum lifetime volume
ALERT_SCORE_THRESHOLD	60	Minimum score to alert
STRONG_SCORE_THRESHOLD	80	Score for the STRONG tier
ALERT_COOLDOWN_SECONDS	1200	Cooldown per token between alerts
MAX_ALERTS_PER_SCAN	5	Alert cap per scan
MAX_SECURITY_CHECKS_PER_SCAN	12	Security API calls per scan
STRICT_SECURITY	false	Any security "danger" or warning blocks the alert
REQUIRE_SECURITY_CHECK	false	Block coins whose security couldn't be verified
Running
bash
python bot.py

Replace bot.py with your script's filename. On startup the bot posts an "online" message in your alert channel and begins scanning.

Slash commands
Command	Access	Description
/check <chain> <address>	Everyone	Full analysis and security check of any token
/top [count]	Everyone	Highest-scoring new coins right now
/status	Everyone	Bot health, settings and scan stats
/stats	Everyone	Alert performance over the last 7 days
/watchlist	Everyone	Show watched tokens
/watch <chain> <address>	Manage Server	Always track a specific token
/unwatch <chain> <address>	Manage Server	Stop tracking a token
/threshold <1-100>	Manage Server	Set the minimum alert score
/pause / /resume	Manage Server	Pause or resume alerts (tracking continues)
/scan	Manage Server	Run a scan immediately
How it works
Discover: pulls fresh tokens from DexScreener and GeckoTerminal every scan.
Snapshot: records price, liquidity, volume and buy/sell counts each scan, so it can measure real recent activity rather than lifetime totals.
Score: rates each new coin from 0 to 100 on momentum and quality signals.
Verify: coins near the threshold go through RugCheck or GoPlus. Hard red flags (honeypot, mint/freeze authority, extreme taxes, draining liquidity) block the alert regardless of score.
Alert: qualifying coins are posted as an embed with link buttons.
Track: the bot follows each alerted coin for 48 hours, posting milestone replies and rug warnings.
Project structure
.
├── bot.py            # the bot
├── .env              # your secrets (never commit this)
└── launch_bot.db     # SQLite database (created automatically)

Add this to your .gitignore:

.env
*.db
venv/
__pycache__/
Limitations
Relies on free public APIs, so rate limits and occasional outages are expected.
Security checks can fail or lag on very new tokens. Unverified coins are flagged, not guaranteed safe.
Peak performance is measured once per scan, so short spikes between scans can be missed.
Scores are heuristics, not predictions.
Disclaimer

This project is for educational and informational purposes only. It is not financial advice. Memecoins are extremely volatile and high-risk. Most new tokens go to zero, and rug pulls and scams are common. A high score or a clean security report does not mean a token is safe or will go up. Do your own research and never risk more than you can afford to lose. The authors accept no liability for any losses.

Contributing

Issues and pull requests are welcome.

License

Add your preferred license here (for example, MIT).
