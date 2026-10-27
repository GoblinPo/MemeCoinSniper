#!/usr/bin/env python3
"""
Memecoin Signal Bot v2
======================
Alert-only Discord bot (NO wallets, NO trading). Free APIs, no API keys.

What it does (the same building blocks the paid scanners use):
  * Multi-source discovery  : DexScreener (profiles/boosts) + GeckoTerminal (trending/new pools)
  * Security gate           : RugCheck (Solana) + GoPlus (Base/Ethereum/BSC) - honeypots, mint/freeze
                              authority, taxes, LP lock, holder risks
  * Signal engine (0-100)   : volume spike, acceleration, buy pressure, momentum, multi-scan trend,
                              liquidity health, socials, trending confluence, wash-trade penalties
  * Alert tiers             : EARLY / STRONG pump signals, fresh-launch feed, rug/liquidity warnings
  * Performance tracking    : milestone replies (1.5x, 2x, 3x ... 100x) + win-rate stats (SQLite)
  * Slash commands          : /check /top /status /stats /watch /unwatch /watchlist
                              /threshold /pause /resume /scan
  * Link buttons            : chart, security report, explorer, GMGN

Heuristics only - NOT financial advice. Memecoins are extremely risky.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
import statistics
import time
from collections import defaultdict, deque
from datetime import datetime
from dataclasses import dataclass, field
from typing import Any, Optional

import aiohttp
import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

# =============================================================================
# CONFIG
# =============================================================================
CHAIN_META: dict[str, dict[str, Any]] = {
    # dexscreener id -> metadata
    "solana":   {"gecko": "solana", "goplus": None, "gmgn": "sol",  "explorer": "https://solscan.io/token/{}"},
    "base":     {"gecko": "base",   "goplus": 8453, "gmgn": "base", "explorer": "https://basescan.org/token/{}"},
    "ethereum": {"gecko": "eth",    "goplus": 1,    "gmgn": "eth",  "explorer": "https://etherscan.io/token/{}"},
    "bsc":      {"gecko": "bsc",    "goplus": 56,   "gmgn": "bsc",  "explorer": "https://bscscan.com/token/{}"},
}

DEX_API = "https://api.dexscreener.com"
GECKO_API = "https://api.geckoterminal.com/api/v2"
RUGCHECK_API = "https://api.rugcheck.xyz/v1"
GOPLUS_API = "https://api.gopluslabs.io/api/v1/token_security"

MILESTONES = (1.5, 2, 3, 5, 10, 25, 50, 100)
# RugCheck risk names that always block an alert, whatever the score.
HARD_BLOCK_WORDS = ("freeze authority", "mint authority", "honeypot", "copycat", "rugged")


def _int(name: str, default: int = 0) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


class Config:
    def __init__(self) -> None:
        self.token = os.getenv("DISCORD_TOKEN", "")
        self.alert_channel = _int("ALERT_CHANNEL_ID")          # spike alerts + milestones
        self.warning_channel = _int("WARNING_CHANNEL_ID")      # optional: rug warnings (falls back to alerts)
        self.guild_id = _int("GUILD_ID")                       # optional: instant slash-command sync
        self.chains = [c.strip().lower() for c in os.getenv("CHAINS", "solana,base,ethereum,bsc").split(",")
                       if c.strip().lower() in CHAIN_META]
        self.data_dir = os.getenv("DATA_DIR", ".")

        # scanning
        self.scan_interval = _int("SCAN_INTERVAL_SECONDS", 20)
        self.gecko_pages_solana = _int("GECKO_PAGES_SOLANA", 2)   # Solana launches fast: read 2 pages
        self.max_tracked = _int("MAX_TRACKED_TOKENS", 250)

        # what counts as "new"
        self.max_age_minutes = _float("MAX_PAIR_AGE_MINUTES", 30)

        # spike detection
        self.spike_window = _int("SPIKE_WINDOW_SECONDS", 60)       # volume + buy/sell window
        self.price_window = _int("PRICE_WINDOW_SECONDS", 120)      # price-jump window
        self.min_spike_volume = _float("MIN_SPIKE_VOLUME_USD", 1_000)   # $ traded inside the window
        self.max_launch_gain = _float("MAX_GAIN_SINCE_LAUNCH_PCT", 400)  # skip if already up this much

        # basic quality filters (new pools are small, so these are low)
        self.min_liquidity = _float("MIN_LIQUIDITY_USD", 5_000)
        self.min_txns = _int("MIN_TOTAL_TXNS", 25)
        self.min_total_volume = _float("MIN_TOTAL_VOLUME_USD", 2_000)

        # alerting
        self.default_threshold = _int("ALERT_SCORE_THRESHOLD", 60)
        self.strong_threshold = _int("STRONG_SCORE_THRESHOLD", 80)
        self.cooldown = _int("ALERT_COOLDOWN_SECONDS", 20 * 60)
        self.max_alerts_per_scan = _int("MAX_ALERTS_PER_SCAN", 5)
        self.max_security_checks = _int("MAX_SECURITY_CHECKS_PER_SCAN", 12)
        self.strict_security = _bool("STRICT_SECURITY", False)    # True: ANY security "danger" blocks
        self.require_security = _bool("REQUIRE_SECURITY_CHECK", False)  # True: unverified coins are blocked


CFG = Config()

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("signal-bot")

# =============================================================================
# SMALL HELPERS
# =============================================================================
Key = tuple[str, str]
_EVM_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_SOL_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def norm_addr(chain: str, addr: str | None) -> Optional[str]:
    addr = (addr or "").strip()
    if chain == "solana":
        return addr if _SOL_RE.match(addr) else None
    return addr.lower() if _EVM_RE.match(addr) else None


def f(x: Any) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def fmt_usd(n: float) -> str:
    if n >= 1e9:
        return f"${n / 1e9:.2f}B"
    if n >= 1e6:
        return f"${n / 1e6:.2f}M"
    if n >= 1e3:
        return f"${n / 1e3:.1f}K"
    return f"${n:,.2f}"


def fmt_price(p: float) -> str:
    if p >= 1:
        return f"${p:,.4f}"
    if p >= 0.01:
        return f"${p:.5f}"
    return f"${p:.10f}".rstrip("0")


def fmt_age(minutes: Optional[float]) -> str:
    if minutes is None:
        return "?"
    if minutes < 60:
        return f"{minutes:.0f}m"
    if minutes < 1440:
        return f"{minutes / 60:.1f}h"
    return f"{minutes / 1440:.1f}d"


def score_bar(score: int) -> str:
    n = max(0, min(10, score // 10))
    return "█" * n + "░" * (10 - n)


# =============================================================================
# HTTP CLIENT (rate limiting + retries)
# =============================================================================
class RateLimiter:
    def __init__(self, per_minute: int) -> None:
        self.interval = 60.0 / per_minute
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            delay = self._next - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._next = max(time.monotonic(), self._next) + self.interval


class Http:
    def __init__(self) -> None:
        self.session: Optional[aiohttp.ClientSession] = None
        self.limiters = {
            "dex": RateLimiter(150), "gecko": RateLimiter(24),
            "rugcheck": RateLimiter(30), "goplus": RateLimiter(25),
        }

    async def start(self) -> None:
        self.session = aiohttp.ClientSession(headers={"User-Agent": "memecoin-signal-bot/2.0",
                                                      "Accept": "application/json"})

    async def close(self) -> None:
        if self.session:
            await self.session.close()

    async def get(self, api: str, url: str, params: dict | None = None, retries: int = 3) -> Any:
        assert self.session is not None
        for attempt in range(retries):
            await self.limiters[api].wait()
            try:
                async with self.session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
                    if r.status == 200:
                        return await r.json(content_type=None)
                    if r.status == 429:
                        try:
                            wait = float(r.headers.get("Retry-After", 2 ** (attempt + 1)))
                        except ValueError:
                            wait = 2 ** (attempt + 1)
                        log.warning("%s rate limited, sleeping %.0fs", api, wait)
                        await asyncio.sleep(min(wait, 30))
                        continue
                    if r.status >= 500:
                        await asyncio.sleep(2 ** attempt)
                        continue
                    log.debug("%s -> HTTP %s", url, r.status)
                    return None
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                log.debug("%s failed (%s), retry %d", url, e, attempt + 1)
                await asyncio.sleep(2 ** attempt)
        return None


# =============================================================================
# PERSISTENCE (SQLite)
# =============================================================================
class Store:
    def __init__(self, path: str) -> None:
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT);
            CREATE TABLE IF NOT EXISTS watchlist (chain TEXT, address TEXT, PRIMARY KEY (chain, address));
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chain TEXT, address TEXT, symbol TEXT, ts REAL, price REAL, score INTEGER, tier TEXT,
                peak_mult REAL DEFAULT 1, last_milestone REAL DEFAULT 1, rugged INTEGER DEFAULT 0,
                channel_id INTEGER, message_id INTEGER);
            CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(ts);
        """)
        self.db.commit()

    # settings
    def get(self, k: str, default: Any = None) -> Any:
        row = self.db.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
        return row["v"] if row else default

    def set(self, k: str, v: Any) -> None:
        self.db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (k, str(v)))
        self.db.commit()

    # watchlist
    def watch_add(self, chain: str, addr: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO watchlist VALUES (?,?)", (chain, addr))
        self.db.commit()

    def watch_remove(self, chain: str, addr: str) -> None:
        self.db.execute("DELETE FROM watchlist WHERE chain=? AND address=?", (chain, addr))
        self.db.commit()

    def watchlist(self) -> list[Key]:
        return [(r["chain"], r["address"]) for r in self.db.execute("SELECT * FROM watchlist")]

    # alerts
    def add_alert(self, **kw: Any) -> int:
        cur = self.db.execute(
            "INSERT INTO alerts (chain,address,symbol,ts,price,score,tier,channel_id,message_id) "
            "VALUES (:chain,:address,:symbol,:ts,:price,:score,:tier,:channel_id,:message_id)", kw)
        self.db.commit()
        return int(cur.lastrowid)

    def update_alert(self, alert_id: int, **kw: Any) -> None:
        cols = ", ".join(f"{k}=?" for k in kw)
        self.db.execute(f"UPDATE alerts SET {cols} WHERE id=?", (*kw.values(), alert_id))
        self.db.commit()

    def alerts_since(self, seconds: float) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM alerts WHERE ts>?", (time.time() - seconds,)).fetchall()


# =============================================================================
# SECURITY CHECKS
# =============================================================================
@dataclass
class Security:
    checked: bool = False
    blocked: Optional[str] = None
    danger: list[str] = field(default_factory=list)
    warn: list[str] = field(default_factory=list)
    lp_locked_pct: Optional[float] = None
    source: str = ""

    def lines(self) -> str:
        if not self.checked:
            return "❔ Not verified (API unavailable)"
        out = []
        if self.blocked:
            out.append(f"🚫 **{self.blocked}**")
        out += [f"🔴 {d}" for d in self.danger[:3]]
        out += [f"🟡 {w}" for w in self.warn[:3]]
        if self.lp_locked_pct is not None:
            out.append(f"🔒 LP locked/burned: {self.lp_locked_pct:.0f}%")
        if not out:
            out.append("✅ No red flags found")
        return "\n".join(out)[:1000] + f"\n*via {self.source}*"


async def check_solana(http: Http, addr: str) -> Security:
    data = await http.get("rugcheck", f"{RUGCHECK_API}/tokens/{addr}/report/summary")
    if not isinstance(data, dict):
        return Security()
    sec = Security(checked=True, source="RugCheck")
    for risk in data.get("risks") or []:
        name = str(risk.get("name") or "Unknown risk")
        level = str(risk.get("level") or "").lower()
        if level == "danger":
            sec.danger.append(name)
        elif level == "warn":
            sec.warn.append(name)
        if any(w in name.lower() for w in HARD_BLOCK_WORDS) and level in ("danger", "warn"):
            sec.blocked = name
    if CFG.strict_security and sec.danger and not sec.blocked:
        sec.blocked = sec.danger[0]
    lp = data.get("lpLockedPct")
    if isinstance(lp, (int, float)):
        sec.lp_locked_pct = float(lp)
    return sec


async def check_evm(http: Http, chain: str, addr: str) -> Security:
    chain_id = CHAIN_META[chain]["goplus"]
    data = await http.get("goplus", f"{GOPLUS_API}/{chain_id}", params={"contract_addresses": addr})
    info = ((data or {}).get("result") or {}).get(addr.lower()) if isinstance(data, dict) else None
    if not info:
        return Security()
    sec = Security(checked=True, source="GoPlus")
    flag = lambda k: str(info.get(k, "0")) == "1"  # noqa: E731

    if flag("is_honeypot"):
        sec.blocked = "Honeypot detected"
    if flag("cannot_sell_all"):
        sec.blocked = "Cannot sell all tokens"
    if flag("owner_change_balance"):
        sec.blocked = "Owner can change balances"
    buy_tax, sell_tax = f(info.get("buy_tax")), f(info.get("sell_tax"))
    if max(buy_tax, sell_tax) >= 0.15:
        sec.blocked = f"High tax (buy {buy_tax:.0%} / sell {sell_tax:.0%})"
    elif max(buy_tax, sell_tax) >= 0.05:
        sec.warn.append(f"Tax: buy {buy_tax:.0%} / sell {sell_tax:.0%}")
    for key, label in (("is_mintable", "Owner can mint more tokens"), ("hidden_owner", "Hidden owner"),
                       ("can_take_back_ownership", "Ownership can be reclaimed"),
                       ("transfer_pausable", "Transfers can be paused"), ("is_blacklisted", "Has blacklist function")):
        if flag(key):
            sec.warn.append(label)
    if str(info.get("is_open_source", "1")) == "0":
        sec.warn.append("Contract not verified/open source")
    if CFG.strict_security and sec.warn and not sec.blocked:
        sec.blocked = sec.warn[0]
    locked = 0.0
    for h in info.get("lp_holders") or []:
        if str(h.get("is_locked")) == "1" or str(h.get("address", "")).lower().endswith("dead"):
            locked += f(h.get("percent")) * 100
    if info.get("lp_holders"):
        sec.lp_locked_pct = min(locked, 100.0)
    return sec


# =============================================================================
# SIGNAL ENGINE
# =============================================================================
def parse_iso(value: Any) -> Optional[float]:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


@dataclass
class Snap:
    ts: float
    price: float
    liq: float
    vol: float      # lifetime volume (for pairs < 1h old DexScreener's "h1" == since launch)
    buys: int       # lifetime buys
    sells: int      # lifetime sells


def pick_window(hist: list[Snap], now: float, seconds: float) -> Optional[Snap]:
    """Snapshot whose age is closest to `seconds` (but at least half of it, at most 3x it)."""
    best: Optional[Snap] = None
    for snap in hist:
        age = now - snap.ts
        if age < seconds * 0.5 or age > seconds * 3:
            continue
        if best is None or abs(age - seconds) < abs((now - best.ts) - seconds):
            best = snap
    return best


@dataclass
class Signal:
    key: Key
    pair: dict
    mode: str                      # "pump" | "launch"
    score: int
    reasons: list[str]
    flags: list[str]
    metrics: dict[str, float]
    blocked: Optional[str] = None
    sec: Optional[Security] = None
    sources: set[str] = field(default_factory=set)
    hist: list[Snap] = field(default_factory=list)

    @property
    def symbol(self) -> str:
        return (self.pair.get("baseToken") or {}).get("symbol") or "?"


def evaluate(pair: dict, hist: list[Snap], sources: set[str], sec: Optional[Security],
             force: bool = False) -> Optional[Signal]:
    """Pure scoring function for NEW coins. `hist` = snapshots from PREVIOUS scans (oldest first).
    Returns None if the coin is filtered out (too old, too small, already pumped...)."""
    chain = pair.get("chainId", "")
    base = pair.get("baseToken") or {}
    addr = norm_addr(chain, base.get("address"))
    if not addr:
        return None
    key = (chain, addr)
    now = time.time()

    price = f(pair.get("priceUsd"))
    liq = f((pair.get("liquidity") or {}).get("usd"))
    mcap = f(pair.get("marketCap") or pair.get("fdv"))
    # Pairs younger than 1h: these "h1" figures are LIFETIME totals -> snapshot deltas are real recent activity.
    life_vol = f((pair.get("volume") or {}).get("h1"))
    chg = pair.get("priceChange") or {}
    c5, c1 = f(chg.get("m5")), f(chg.get("h1"))      # c1 ~ change since launch for new pairs
    h1tx = (pair.get("txns") or {}).get("h1") or {}
    buys, sells = int(h1tx.get("buys") or 0), int(h1tx.get("sells") or 0)
    txns = buys + sells
    created = pair.get("pairCreatedAt")
    age_min = (now * 1000 - created) / 60000 if created else None

    if price <= 0:
        return None
    if not force:
        if age_min is None or age_min > CFG.max_age_minutes:
            return None
        if liq < CFG.min_liquidity or txns < CFG.min_txns or life_vol < CFG.min_total_volume:
            return None
        if c1 > CFG.max_launch_gain:
            return None  # already ran too far - we want the EARLY spike

    score, reasons, flags = 0, [], []
    blocked: Optional[str] = None

    # ---- live window stats from our own snapshots ---------------------------------
    w = pick_window(hist, now, CFG.spike_window)
    win_secs = win_vol = vol_ratio = 0.0
    db = ds = 0
    if w:
        win_secs = now - w.ts
        win_vol = max(life_vol - w.vol, 0.0)
        db, ds = max(buys - w.buys, 0), max(sells - w.sells, 0)
        rate = win_vol / (win_secs / 60)                                  # $/min right now
        life_before = max((age_min or 1.0) - win_secs / 60, 1.0)
        base_rate = max(w.vol / life_before, 100.0)                       # its own earlier $/min pace
        vol_ratio = rate / base_rate

    # 1) Volume surge vs the coin's own earlier pace (max 25)
    if w and win_vol >= CFG.min_spike_volume:
        for lim, pts in ((8, 25), (5, 20), (3, 14), (2, 8)):
            if vol_ratio >= lim:
                score += pts
                reasons.append(f"📈 **{fmt_usd(win_vol)}** traded in ~{win_secs:.0f}s - **{vol_ratio:.1f}x** its earlier pace")
                break

    # 2) Price jump over the last ~2 minutes (max 20)
    pw = pick_window(hist, now, CFG.price_window)
    price_gain = price_secs = 0.0
    if pw and pw.price > 0:
        price_gain, price_secs = (price / pw.price - 1) * 100, now - pw.ts
        pts = 20 if price_gain >= 35 else 14 if price_gain >= 20 else 8 if price_gain >= 10 else 0
        if pts and w and db < ds:
            pts //= 2                              # price up but sellers winning -> weaker
        if pts:
            score += pts
            reasons.append(f"🚀 Price **{price_gain:+.1f}%** in the last ~{price_secs:.0f}s")

    # 3) Buy flow in the window (max 15, penalty -10)
    if w:
        if db >= 12 and db >= 2 * max(ds, 1):
            score += 15
            reasons.append(f"🟢 **{db} buys vs {ds} sells** in ~{win_secs:.0f}s")
        elif db >= 6 and db >= 1.5 * max(ds, 1):
            score += 8
            reasons.append(f"🟢 {db} buys vs {ds} sells in ~{win_secs:.0f}s")
        elif ds >= 8 and ds > db * 1.5:
            score -= 10
            flags.append(f"⚠️ Sellers dominating right now ({ds} sells vs {db} buys)")

    # 4) Liquidity trend (+5) / draining (block)
    lw = pw or w
    if lw and lw.liq > 0 and liq / lw.liq - 1 >= 0.10:
        score += 5
        reasons.append("💧 Liquidity growing")
    peak_liq = max((snap.liq for snap in hist), default=0)
    if peak_liq > 0 and liq < peak_liq * 0.7:
        blocked = f"Liquidity down {100 - liq / peak_liq * 100:.0f}% from its peak"

    # 5) Lifetime traction (max 10)
    score += 6 if txns >= 100 else 4 if txns >= 50 else 2 if txns >= 25 else 0
    score += 4 if life_vol >= 20_000 else 2 if life_vol >= 8_000 else 0

    # 6) Liquidity health vs market cap (max 5, min -5)
    if mcap > 0:
        r = liq / mcap
        score += 5 if r >= 0.08 else 2 if r >= 0.04 else 0
        if r < 0.015:
            score -= 5
            flags.append("⚠️ Thin liquidity vs market cap")

    # 7) Earliness (max 5)
    if age_min is not None:
        score += 5 if age_min <= 10 else 3 if age_min <= 20 else 0

    # 8) Socials / profile (max 5)
    info = pair.get("info") or {}
    socials = {str(x.get("type", "")).lower() for x in info.get("socials") or []}
    if socials & {"twitter", "telegram"}:
        score += 3
    if info.get("websites"):
        score += 2
    if not socials and not info.get("websites"):
        flags.append("ℹ️ No socials/website listed")

    # 9) Penalties: chasing, wash/bot volume
    if c1 > 150:
        score -= 8
        flags.append(f"⚠️ Already {c1:+.0f}% since launch - may be late")
    if txns >= 200 and life_vol / txns < 6:
        score -= 15
        flags.append(f"⚠️ Tiny average trade (${life_vol / txns:.1f}) - possible bot/wash volume")

    # 10) Security
    if sec is not None:
        if sec.blocked:
            blocked = blocked or sec.blocked
        if sec.checked:
            score -= min(30, 15 * len(sec.danger)) + min(9, 3 * len(sec.warn))
            if not sec.danger and not sec.warn:
                score += 5
            if sec.lp_locked_pct is not None and sec.lp_locked_pct >= 80:
                score += 3
        else:
            flags.append("❔ Security not verified yet")
            if CFG.require_security:
                blocked = blocked or "Security could not be verified"

    metrics = dict(price=price, liq=liq, mcap=mcap, vol=life_vol, c5=c5, c1=c1, buys=buys, sells=sells,
                   age=age_min if age_min is not None else -1, has_window=1.0 if w else 0.0,
                   win_secs=win_secs, win_vol=win_vol, win_buys=db, win_sells=ds, vol_ratio=vol_ratio,
                   price_gain=price_gain, price_secs=price_secs)
    return Signal(key=key, pair=pair, mode="launch", score=max(0, min(100, score)), reasons=reasons,
                  flags=flags, metrics=metrics, blocked=blocked, sec=sec, sources=set(sources), hist=list(hist))


# =============================================================================
# DISCORD UI
# =============================================================================
def build_links(chain: str, addr: str, pair_url: Optional[str]) -> discord.ui.View:
    meta = CHAIN_META[chain]
    view = discord.ui.View(timeout=None)
    links = []
    if pair_url:
        links.append(("📊 Chart", pair_url))
    if chain == "solana":
        links.append(("🛡️ RugCheck", f"https://rugcheck.xyz/tokens/{addr}"))
    else:
        links.append(("🛡️ GoPlus", f"https://gopluslabs.io/token-security/{meta['goplus']}/{addr}"))
    links.append(("🔍 Explorer", meta["explorer"].format(addr)))
    links.append(("🧠 GMGN", f"https://gmgn.ai/{meta['gmgn']}/token/{addr}"))
    for label, url in links:
        view.add_item(discord.ui.Button(label=label, url=url, style=discord.ButtonStyle.link))
    return view


def build_embed(sig: Signal, tier: str) -> discord.Embed:
    pair, m = sig.pair, sig.metrics
    base = pair.get("baseToken") or {}
    colors = {"STRONG": 0xFF5A1F, "SPIKE": 0x2ECC71, "CHECK": 0x95A5A6}
    titles = {"STRONG": "🔥 STRONG LAUNCH SPIKE", "SPIKE": "⚡ LAUNCH SPIKE", "CHECK": "🔎 TOKEN CHECK"}
    e = discord.Embed(title=f"{titles[tier]} - {base.get('name', '?')} (${sig.symbol})",
                      url=pair.get("url"), color=colors[tier],
                      description="\n".join(sig.reasons) or "No spike detected right now.")
    age = fmt_age(m["age"] if m["age"] >= 0 else None)
    e.add_field(name="Score", value=f"`{score_bar(sig.score)}` **{sig.score}/100**", inline=True)
    e.add_field(name="Age", value=f"**{age}** old · {sig.key[0].upper()}", inline=True)
    if m["has_window"]:
        e.add_field(name=f"Last ~{m['win_secs']:.0f}s",
                    value=f"Volume {fmt_usd(m['win_vol'])} ({m['vol_ratio']:.1f}x pace)\n"
                          f"Buys/Sells {int(m['win_buys'])}/{int(m['win_sells'])} · Price {m['price_gain']:+.1f}%",
                    inline=False)
    else:
        e.add_field(name="Live window", value="Not enough scan history yet", inline=False)
    e.add_field(name="Price", value=fmt_price(m["price"]), inline=True)
    e.add_field(name="Market Cap", value=fmt_usd(m["mcap"]), inline=True)
    e.add_field(name="Liquidity", value=fmt_usd(m["liq"]), inline=True)
    e.add_field(name="Total volume", value=fmt_usd(m["vol"]), inline=True)
    e.add_field(name="Buys / Sells", value=f"{int(m['buys'])} / {int(m['sells'])}", inline=True)
    e.add_field(name="Since launch", value=f"{m['c1']:+.1f}%", inline=True)
    if sig.sec is not None:
        e.add_field(name="Security", value=sig.sec.lines(), inline=False)
    if sig.flags:
        e.add_field(name="Flags", value="\n".join(sig.flags)[:1000], inline=False)
    e.add_field(name="Contract", value=f"`{sig.key[1]}`", inline=False)
    img = (pair.get("info") or {}).get("imageUrl")
    if img:
        e.set_thumbnail(url=img)
    e.set_footer(text="Not financial advice - DYOR. New coins rug constantly; never risk more than you can lose.")
    e.timestamp = discord.utils.utcnow()
    return e


# =============================================================================
# THE BOT
# =============================================================================
@dataclass
class Tracked:
    first_seen: float
    last_seen: float
    sources: set[str] = field(default_factory=set)


class SignalBot(discord.Client):
    def __init__(self) -> None:
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.http_c = Http()
        self.store = Store(os.path.join(CFG.data_dir, "launch_bot.db"))
        self.tracked: dict[Key, Tracked] = {}
        self.history: dict[Key, deque[Snap]] = defaultdict(lambda: deque(maxlen=130))
        self.too_old: dict[Key, float] = {}   # tokens we already know are older than the window
        self.sec_cache: dict[Key, tuple[float, Security]] = {}
        self.last_alert: dict[Key, float] = {}
        self.last_results: list[Signal] = []
        self.scan_lock = asyncio.Lock()
        self.scan_task: Optional[asyncio.Task] = None
        self.started = time.time()
        self.last_scan_secs = 0.0
        self.last_scan_at = 0.0
        self.scans = 0

    # ---- settings -----------------------------------------------------------
    @property
    def threshold(self) -> int:
        return int(self.store.get("threshold", CFG.default_threshold))

    @property
    def paused(self) -> bool:
        return self.store.get("paused", "0") == "1"

    # ---- lifecycle ----------------------------------------------------------
    async def setup_hook(self) -> None:
        await self.http_c.start()
        register_commands(self)
        if CFG.guild_id:
            guild = discord.Object(id=CFG.guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        for row in self.store.alerts_since(CFG.cooldown):
            self.last_alert[(row["chain"], row["address"])] = row["ts"]
        self.scan_task = asyncio.create_task(self.scan_forever())

    async def close(self) -> None:
        if self.scan_task:
            self.scan_task.cancel()
        await self.http_c.close()
        await super().close()

    async def on_ready(self) -> None:
        log.info("Logged in as %s | chains=%s | threshold=%d", self.user, CFG.chains, self.threshold)
        ch = await self.channel(CFG.alert_channel)
        if ch:
            await ch.send(f"✅ **Launch Spike Bot online** - hunting coins under {CFG.max_age_minutes:g} min old on "
                          f"{', '.join(c.upper() for c in CFG.chains)}. Use `/status` and `/check`.")

    async def channel(self, cid: int) -> Optional[discord.abc.Messageable]:
        if not cid:
            return None
        ch = self.get_channel(cid)
        if ch is None:
            try:
                ch = await self.fetch_channel(cid)
            except discord.HTTPException:
                log.error("Cannot access channel %s - check ID and bot permissions", cid)
        return ch  # type: ignore[return-value]

    # ---- discovery ----------------------------------------------------------
    def _track(self, chain: str, addr: Optional[str], source: str) -> None:
        addr = norm_addr(chain, addr)
        if not addr or chain not in CFG.chains or (chain, addr) in self.too_old:
            return
        now = time.time()
        t = self.tracked.get((chain, addr))
        if t:
            t.last_seen = now
            t.sources.add(source)
        else:
            self.tracked[(chain, addr)] = Tracked(now, now, {source})

    async def discover(self) -> None:
        h = self.http_c
        calls = [h.get("dex", f"{DEX_API}/token-profiles/latest/v1")]
        tags: list[Optional[str]] = [None]                       # None = DexScreener profiles
        for chain in CFG.chains:
            net = CHAIN_META[chain]["gecko"]
            for page in range(1, (CFG.gecko_pages_solana if chain == "solana" else 1) + 1):
                calls.append(h.get("gecko", f"{GECKO_API}/networks/{net}/new_pools", params={"page": page}))
                tags.append(chain)
        results = await asyncio.gather(*calls, return_exceptions=True)

        for data, chain in zip(results, tags):
            if chain is None:
                if isinstance(data, list):
                    for it in data:
                        self._track(it.get("chainId", ""), it.get("tokenAddress"), "profile")
                continue
            if not isinstance(data, dict):
                continue
            for it in data.get("data") or []:
                try:
                    created = parse_iso((it.get("attributes") or {}).get("pool_created_at"))
                    if created and (time.time() - created) / 60 > CFG.max_age_minutes + 10:
                        continue
                    token_id = it["relationships"]["base_token"]["data"]["id"]   # e.g. "solana_ADDR"
                    self._track(chain, token_id.split("_", 1)[1], "new")
                except (KeyError, IndexError, TypeError, AttributeError):
                    continue
        for chain, addr in self.store.watchlist():
            self._track(chain, addr, "watch")
        self._prune()

    def pinned(self) -> set[Key]:
        keys = set(self.store.watchlist())
        for row in self.store.alerts_since(48 * 3600):
            keys.add((row["chain"], row["address"]))
        return keys

    def _prune(self) -> None:
        pinned = self.pinned()
        for chain, addr in pinned:               # alerted/watched tokens are always tracked
            self._track(chain, addr, "pinned")
        if len(self.tracked) > CFG.max_tracked:
            extra = sorted((k for k in self.tracked if k not in pinned), key=lambda k: self.tracked[k].first_seen)
            for key in extra[: len(self.tracked) - CFG.max_tracked]:
                self.tracked.pop(key, None)
                self.history.pop(key, None)

    def mark_old(self, key: Key) -> None:
        """Stop tracking a coin that has aged out of the 'new launch' window (and never re-add it)."""
        self.tracked.pop(key, None)
        self.history.pop(key, None)
        self.too_old[key] = time.time()
        if len(self.too_old) > 5000:
            for k, _ in sorted(self.too_old.items(), key=lambda kv: kv[1])[:2500]:
                self.too_old.pop(k, None)

    async def fetch_pairs(self, keys: list[Key]) -> dict[Key, dict]:
        by_chain: dict[str, list[str]] = defaultdict(list)
        for chain, addr in keys:
            by_chain[chain].append(addr)
        calls = [self.http_c.get("dex", f"{DEX_API}/tokens/v1/{chain}/{','.join(addrs[i:i + 30])}")
                 for chain, addrs in by_chain.items() for i in range(0, len(addrs), 30)]
        best: dict[Key, dict] = {}
        for data in await asyncio.gather(*calls, return_exceptions=True):
            if not isinstance(data, list):
                continue
            for pair in data:
                chain = pair.get("chainId", "")
                addr = norm_addr(chain, (pair.get("baseToken") or {}).get("address"))
                if not addr:
                    continue
                key = (chain, addr)
                if key not in self.tracked and key not in keys:
                    continue
                liq = f((pair.get("liquidity") or {}).get("usd"))
                if key not in best or liq > f((best[key].get("liquidity") or {}).get("usd")):
                    best[key] = pair
        return best

    async def security(self, chain: str, addr: str) -> Security:
        hit = self.sec_cache.get((chain, addr))
        if hit and time.time() - hit[0] < (300 if hit[1].checked else 30):
            return hit[1]
        try:
            sec = await (check_solana(self.http_c, addr) if chain == "solana" else check_evm(self.http_c, chain, addr))
        except Exception as e:  # never let a security API crash the scan
            log.warning("Security check failed for %s: %s", addr, e)
            sec = Security()
        self.sec_cache[(chain, addr)] = (time.time(), sec)
        return sec

    # ---- main scan ----------------------------------------------------------
    async def scan_forever(self) -> None:
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Scan failed")
            await asyncio.sleep(CFG.scan_interval)

    async def scan_once(self) -> int:
        async with self.scan_lock:
            t0 = time.time()
            await self.discover()
            pairs = await self.fetch_pairs(list(self.tracked))
            pinned = self.pinned()
            pre: list[Signal] = []
            for key, pair in pairs.items():
                created = pair.get("pairCreatedAt")
                age_min = (time.time() * 1000 - created) / 60000 if created else None
                if key not in pinned and age_min is not None and age_min > CFG.max_age_minutes + 10:
                    self.mark_old(key)
                    continue
                hist = self.history[key]
                sig = evaluate(pair, list(hist), self.tracked[key].sources, None)
                hist.append(Snap(time.time(), f(pair.get("priceUsd")),
                                 f((pair.get("liquidity") or {}).get("usd")),
                                 f((pair.get("volume") or {}).get("h1")),
                                 int(((pair.get("txns") or {}).get("h1") or {}).get("buys") or 0),
                                 int(((pair.get("txns") or {}).get("h1") or {}).get("sells") or 0)))
                if sig:
                    pre.append(sig)
            # tokens that never got a DexScreener pair within the window: forget them
            for key in [k for k, t in self.tracked.items()
                        if k not in pairs and k not in pinned
                        and time.time() - t.first_seen > (CFG.max_age_minutes + 10) * 60]:
                self.mark_old(key)

            pre.sort(key=lambda s_: s_.score, reverse=True)
            self.last_results = pre
            await self.track_alerts(pairs)

            sent = checks = 0
            if not self.paused:
                thr = self.threshold
                for sig in pre:
                    if sig.score + 8 < thr or time.time() - self.last_alert.get(sig.key, 0) < CFG.cooldown:
                        continue
                    if checks >= CFG.max_security_checks:
                        break
                    sec = await self.security(*sig.key)
                    checks += 1
                    final = evaluate(sig.pair, sig.hist, sig.sources, sec)
                    if not final or final.blocked or final.score < thr:
                        if final and final.blocked:
                            log.info("Blocked %s: %s", final.symbol, final.blocked)
                        continue
                    if await self.post_signal(final):
                        sent += 1
                        if sent >= CFG.max_alerts_per_scan:
                            break

            self.scans += 1
            self.last_scan_at, self.last_scan_secs = time.time(), time.time() - t0
            log.info("Scan #%d: %d tracked, %d priced, %d new-coin candidates, %d sec checks, %d alerts (%.1fs)",
                     self.scans, len(self.tracked), len(pairs), len(pre), checks, sent, self.last_scan_secs)
            return sent

    async def post_signal(self, sig: Signal) -> bool:
        tier = "STRONG" if sig.score >= CFG.strong_threshold else "SPIKE"
        ch = await self.channel(CFG.alert_channel)
        if ch is None:
            return False
        try:
            msg = await ch.send(embed=build_embed(sig, tier),
                                view=build_links(sig.key[0], sig.key[1], sig.pair.get("url")))
        except discord.HTTPException as e:
            log.error("Could not send alert: %s", e)
            return False
        self.last_alert[sig.key] = time.time()
        self.store.add_alert(chain=sig.key[0], address=sig.key[1], symbol=sig.symbol, ts=time.time(),
                             price=sig.metrics["price"], score=sig.score, tier=tier,
                             channel_id=msg.channel.id, message_id=msg.id)
        log.info("ALERT %s %s score=%d age=%.0fm", tier, sig.symbol, sig.score, sig.metrics["age"])
        await asyncio.sleep(1)  # be gentle with Discord rate limits
        return True

    # ---- milestone + rug monitoring ----------------------------------------
    async def track_alerts(self, pairs: dict[Key, dict]) -> None:
        for row in self.store.alerts_since(48 * 3600):
            key = (row["chain"], row["address"])
            pair = pairs.get(key)
            price = f((pair or {}).get("priceUsd"))
            if not pair or price <= 0 or row["price"] <= 0:
                continue
            mult = price / row["price"]
            peak = max(row["peak_mult"], mult)
            ref = discord.MessageReference(message_id=row["message_id"], channel_id=row["channel_id"],
                                           fail_if_not_exists=False) if row["message_id"] else None
            ch = await self.channel(row["channel_id"]) if row["channel_id"] else None

            hit = max((m for m in MILESTONES if peak >= m > row["last_milestone"]), default=None)
            if hit and ch:
                e = discord.Embed(title=f"🏆 ${row['symbol']} hit {hit:g}x since alert!", color=0xF1C40F,
                                  description=f"Alert price {fmt_price(row['price'])} → now {fmt_price(price)} "
                                              f"(**{mult:.2f}x**, peak {peak:.2f}x)")
                try:
                    await ch.send(embed=e, reference=ref)  # type: ignore[arg-type]
                except discord.HTTPException:
                    pass
                self.store.update_alert(row["id"], peak_mult=peak, last_milestone=hit)
            elif peak > row["peak_mult"]:
                self.store.update_alert(row["id"], peak_mult=peak)

            if not row["rugged"]:
                peak_liq = max((s.liq for s in self.history[key]), default=0)
                liq = f((pair.get("liquidity") or {}).get("usd"))
                if (peak_liq >= CFG.min_liquidity and liq < peak_liq * 0.5) or mult < 0.25:
                    self.store.update_alert(row["id"], rugged=1)
                    wch = await self.channel(CFG.warning_channel or CFG.alert_channel)
                    if wch:
                        e = discord.Embed(title=f"🚨 WARNING: ${row['symbol']} collapsing", color=0xE74C3C,
                                          description=f"Liquidity {fmt_usd(liq)} (peak {fmt_usd(peak_liq)}), price "
                                                      f"**{mult:.2f}x** of alert price. Possible rug / liquidity pull. "
                                                      f"Do not buy.\n`{row['address']}`")
                        try:
                            await wch.send(embed=e, reference=ref)  # type: ignore[arg-type]
                        except discord.HTTPException:
                            pass

    # ---- on-demand analysis (/check) ----------------------------------------
    async def analyze_token(self, chain: str, addr: str) -> Optional[Signal]:
        data = await self.http_c.get("dex", f"{DEX_API}/tokens/v1/{chain}/{addr}")
        pairs = [p for p in (data or []) if norm_addr(chain, (p.get("baseToken") or {}).get("address")) == addr]
        if not pairs:
            return None
        pair = max(pairs, key=lambda p: f((p.get("liquidity") or {}).get("usd")))
        sec = await self.security(chain, addr)
        t = self.tracked.get((chain, addr))
        return evaluate(pair, list(self.history.get((chain, addr), [])), t.sources if t else set(), sec, force=True)


# =============================================================================
# SLASH COMMANDS
# =============================================================================
CHAIN_CHOICES = [app_commands.Choice(name=c.capitalize(), value=c) for c in CHAIN_META]


def register_commands(bot: SignalBot) -> None:
    tree = bot.tree
    admin = app_commands.checks.has_permissions(manage_guild=True)

    @tree.command(name="status", description="Bot health, settings and scan stats")
    async def status(i: discord.Interaction) -> None:
        up = (time.time() - bot.started) / 3600
        e = discord.Embed(title="📡 Signal Bot Status", color=0x3498DB)
        e.add_field(name="State", value="⏸️ Paused" if bot.paused else "▶️ Running", inline=True)
        e.add_field(name="Threshold", value=f"{bot.threshold}/100", inline=True)
        e.add_field(name="Uptime", value=f"{up:.1f}h", inline=True)
        e.add_field(name="Tracked tokens", value=str(len(bot.tracked)), inline=True)
        e.add_field(name="Scans", value=f"{bot.scans} (last {bot.last_scan_secs:.1f}s)", inline=True)
        e.add_field(name="Chains", value=", ".join(CFG.chains), inline=True)
        await i.response.send_message(embed=e, ephemeral=True)

    @tree.command(name="top", description="Highest-scoring NEW coins right now (even below the alert threshold)")
    @app_commands.describe(count="How many to show (max 15)")
    async def top(i: discord.Interaction, count: app_commands.Range[int, 1, 15] = 8) -> None:
        rows = bot.last_results[:count]
        if not rows:
            return await i.response.send_message("No scored coins yet - wait for the first scan.", ephemeral=True)
        lines = [f"`{n:>2}.` **${s.symbol}** · {s.key[0]} · score **{s.score}** · age {fmt_age(s.metrics['age'] if s.metrics['age'] >= 0 else None)} · "
                 f"liq {fmt_usd(s.metrics['liq'])}" for n, s in enumerate(rows, 1)]
        await i.response.send_message(embed=discord.Embed(title="🏁 Top new-coin scores right now (pre-security)", color=0x2ECC71,
                                                          description="\n".join(lines)), ephemeral=True)

    @tree.command(name="check", description="Full analysis + security check of any token")
    @app_commands.describe(chain="Blockchain", address="Token contract address")
    @app_commands.choices(chain=CHAIN_CHOICES)
    async def check(i: discord.Interaction, chain: app_commands.Choice[str], address: str) -> None:
        addr = norm_addr(chain.value, address)
        if not addr:
            return await i.response.send_message("❌ That doesn't look like a valid address for that chain.", ephemeral=True)
        await i.response.defer(thinking=True)
        sig = await bot.analyze_token(chain.value, addr)
        if not sig:
            return await i.followup.send("No trading pairs found for that token.")
        e = build_embed(sig, "CHECK")
        if sig.blocked:
            e.add_field(name="🚫 Blocked from alerts", value=sig.blocked, inline=False)
        await i.followup.send(embed=e, view=build_links(chain.value, addr, sig.pair.get("url")))

    @tree.command(name="stats", description="Performance of past alerts (last 7 days)")
    async def stats(i: discord.Interaction) -> None:
        rows = bot.store.alerts_since(7 * 86400)
        if not rows:
            return await i.response.send_message("No alerts in the last 7 days yet.", ephemeral=True)
        peaks = [r["peak_mult"] for r in rows]
        n = len(rows)
        e = discord.Embed(title="📈 Alert performance - last 7 days", color=0xF1C40F)
        e.add_field(name="Alerts", value=str(n), inline=True)
        e.add_field(name="Hit ≥1.5x", value=f"{sum(p >= 1.5 for p in peaks)} ({sum(p >= 1.5 for p in peaks) / n:.0%})", inline=True)
        e.add_field(name="Hit ≥2x", value=f"{sum(p >= 2 for p in peaks)} ({sum(p >= 2 for p in peaks) / n:.0%})", inline=True)
        e.add_field(name="Median peak", value=f"{statistics.median(peaks):.2f}x", inline=True)
        e.add_field(name="Best peak", value=f"{max(peaks):.2f}x", inline=True)
        e.add_field(name="Collapsed/rugged", value=str(sum(r["rugged"] for r in rows)), inline=True)
        e.set_footer(text="Peak = highest price seen after alert (measured each scan). Past results ≠ future results.")
        await i.response.send_message(embed=e)

    @tree.command(name="watch", description="Always track a specific token")
    @app_commands.choices(chain=CHAIN_CHOICES)
    @admin
    async def watch(i: discord.Interaction, chain: app_commands.Choice[str], address: str) -> None:
        addr = norm_addr(chain.value, address)
        if not addr:
            return await i.response.send_message("❌ Invalid address.", ephemeral=True)
        bot.store.watch_add(chain.value, addr)
        await i.response.send_message(f"👀 Watching `{addr}` on {chain.value}.", ephemeral=True)

    @tree.command(name="unwatch", description="Stop tracking a watched token")
    @app_commands.choices(chain=CHAIN_CHOICES)
    @admin
    async def unwatch(i: discord.Interaction, chain: app_commands.Choice[str], address: str) -> None:
        addr = norm_addr(chain.value, address)
        if addr:
            bot.store.watch_remove(chain.value, addr)
        await i.response.send_message("✅ Removed from watchlist.", ephemeral=True)

    @tree.command(name="watchlist", description="Show watched tokens")
    async def watchlist(i: discord.Interaction) -> None:
        wl = bot.store.watchlist()
        text = "\n".join(f"`{c}` `{a}`" for c, a in wl) or "Watchlist is empty."
        await i.response.send_message(text[:1900], ephemeral=True)

    @tree.command(name="threshold", description="Set the minimum score for pump alerts (1-100)")
    @admin
    async def threshold(i: discord.Interaction, value: app_commands.Range[int, 1, 100]) -> None:
        bot.store.set("threshold", value)
        await i.response.send_message(f"✅ Alert threshold set to **{value}**.")

    @tree.command(name="pause", description="Pause alerts (tracking continues)")
    @admin
    async def pause(i: discord.Interaction) -> None:
        bot.store.set("paused", "1")
        await i.response.send_message("⏸️ Alerts paused.")

    @tree.command(name="resume", description="Resume alerts")
    @admin
    async def resume(i: discord.Interaction) -> None:
        bot.store.set("paused", "0")
        await i.response.send_message("▶️ Alerts resumed.")

    @tree.command(name="scan", description="Run a scan right now")
    @admin
    async def scan(i: discord.Interaction) -> None:
        await i.response.defer(thinking=True, ephemeral=True)
        sent = await bot.scan_once()
        await i.followup.send(f"Scan finished in {bot.last_scan_secs:.1f}s · {sent} alert(s) sent.")

    @tree.error
    async def on_error(i: discord.Interaction, err: app_commands.AppCommandError) -> None:
        msg = "🔒 You need the **Manage Server** permission for that." if isinstance(
            err, app_commands.MissingPermissions) else "⚠️ Something went wrong."
        if not isinstance(err, app_commands.MissingPermissions):
            log.exception("Command error", exc_info=err)
        if i.response.is_done():
            await i.followup.send(msg, ephemeral=True)
        else:
            await i.response.send_message(msg, ephemeral=True)


# =============================================================================
if __name__ == "__main__":
    if not CFG.token or not CFG.alert_channel:
        raise SystemExit("Set DISCORD_TOKEN and ALERT_CHANNEL_ID in your .env file (see .env.example).")
    SignalBot().run(CFG.token, log_handler=None)
