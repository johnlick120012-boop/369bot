# 🤖 Solana Alpha Bot — Project Documentation

> Last updated: 2026-07-26

---

## 📁 Project Structure

```
discordbot/
├── bot.py              # Main bot logic — all commands, embeds, alert systems
├── api_client.py       # All external API wrappers (DexScreener, RugCheck, Helius, pump.fun)
├── config.py           # Logger, constants, chain configs, color palette
├── kol.js              # KOL wallet registry (name, emoji, wallet address per KOL)
├── user_wallets.json   # Per-user custom wallet tracker subscriptions (Supabase-backed)
├── .env                # Secret keys (never commit this)
├── run.bat             # Windows start script
└── doc.md              # This file
```

---

## ⚙️ Environment Variables (`.env`)

| Variable | Purpose |
|---|---|
| `DISCORD_TOKEN` | Bot login token |
| `HELIUS_API_KEY` | WebSocket subscriptions (wallet tracking) |
| `HELIUS_API_KEY_2` | HTTP RPC calls (getTransaction, getTokenAccounts) |
| `SUPABASE_URL` | Database URL for user wallet subscriptions |
| `SUPABASE_KEY` | Supabase service role key |
| `KOL_TRACKER_CHANNEL_ID` | Discord channel ID for KOL alerts |
| `CUSTOM_TRACKER_CHANNEL_ID` | Discord channel ID for custom user tracker alerts |
| `SOLANA_TRACKER_API_KEY` | *(Optional)* Solana Tracker API — enables bundler/holder checks |

---

## 🧠 Existing Features

### Token Analysis (`/ca <address>`)
- Full token embed: price, liquidity, MC, FDV, volume, trade counts, price changes
- Security check via RugCheck (mint authority, freeze, top holders)
- Distribution analysis: bundler %, sniper %, insider cluster %
- Developer info: wallet balance, coins launched, migration rate, Twitter
- Pump.fun bonding curve progress + mayhem mode detection
- DEX Paid status (✅/❌) via DexScreener Orders API
- Fresh Wallets count (wallets with < 0.1 SOL) via Helius batch RPC
- Bot/bundle pump alerts, exit/rug detection, stale coin warnings
- Est. 24h fees paid

### KOL Wallet Tracker
- Monitors 200+ KOL wallets in real-time via Helius WebSocket pool
- Groups wallets in batches of 30 per WebSocket connection (staggered init)
- Detects buy events from postTokenBalances diff
- Aggregates multiple buys of the same token within a 5s window
- Sends a single rich embed to `#kol-tracker` with:
  - Buyer name + emoji + wallet address (truncated)
  - Token name, ticker, SOL spent, market cap
  - CA (copyable)
  - Buttons: Pump.fun / Padre / Axiom

### Custom Wallet Tracker (`/track`)
- Users can subscribe to any wallet address
- Alerts delivered to a private thread per user
- Same embed format as KOL tracker
- Backed by Supabase for persistence across restarts

### Trending Tokens (`/trending`)
- GeckoTerminal trending pools by chain

### Bundler / Cluster Check
- Cross-references RugCheck insider clusters
- Optionally fetches Solana Tracker bundler data if API key is set
- Shows tracked KOL wallets holding the token

---

## 📡 API Client (`api_client.py`) — Available Functions

| Function | Description |
|---|---|
| `get_token_by_ca(address)` | DexScreener — fetch pair data by token address |
| `get_rugcheck_report(address)` | RugCheck — security/insider report |
| `get_gecko_trending(chain)` | GeckoTerminal — trending pools |
| `get_dev_info(creator)` | pump.fun — dev coin history |
| `get_sol_balance(address, rpc)` | Helius RPC — native SOL balance |
| `get_solanatracker_bundlers(mint, key)` | Solana Tracker — bundler wallets |
| `get_solanatracker_holders(mint, key)` | Solana Tracker — paginated holders |
| `get_dex_paid_orders(chain, address)` | DexScreener — DEX paid/boost status (free) |
| `get_fresh_wallets_count(mint, rpc)` | Helius — count fresh wallets (< 0.1 SOL) |

---

## 🚀 Planned Features

---

## 1. 🧠 Alpha Call Tracker — Dev Wallet Radar

### Concept
Track 200–300 historically successful dev wallets. When they **launch a new token** or **post a CA on X**, fire an instant Discord alert with their full track record — before the crowd knows.

### Example Alert
```
🧠 ALPHA CALL — Known Dev Just Launched!

👤 Dev: CupcakeDev | @cupcakedev
🏆 Best Runner: $PENGU (ATH: $1.5B 🚀)
📊 Track Record: 34/493 migrated (6.9%) | 2 rugs flagged
💼 Wallet: Abc1...xyz9 | Balance: 12.4 SOL

🆕 New Token: $NEWCOIN
📝 CA: Abc123...xyz789
🐦 X Post: "just dropped → [Abc123...xyz789]" [View Tweet →]

⚡ Detected 12s after creation!

[Pump.fun] [Padre] [Axiom] [RugCheck]
```

### Signal Sources

| Trigger | Method | Latency |
|---|---|---|
| Dev creates a new token | WebSocket subscribe to pump.fun program, filter `create` ix by creator | ~1–3s |
| Dev posts CA on X | Nitter RSS poll every 30s for posts matching Solana address regex | ~30–60s |

### Dev Registry Schema (`dev_wallets.json`)
```json
[
  {
    "wallet_address": "AbC123...",
    "display_name": "CupcakeDev",
    "twitter_handle": "cupcakedev",
    "notable_projects": [
      { "name": "PENGU", "ticker": "PENGU", "ath_usd": 1500000000, "migrated": true },
      { "name": "BOME", "ticker": "BOME", "ath_usd": 800000000, "migrated": true }
    ],
    "migration_rate": 0.069,
    "total_launched": 493,
    "rug_count": 2,
    "added_at": "2026-07-26"
  }
]
```

### Additional Alpha Signals

| Signal | Description | Why It Matters |
|---|---|---|
| **KOL cross-buy** | A tracked KOL buys the dev's new coin within 5 min | Insider confirmation |
| **Multiple devs coordinate** | 2+ tracked devs launch or buy same token | Coordinated pump signal |
| **Dev gets funded** | Tracked dev wallet receives SOL from fresh/CEX wallet | About to launch |
| **Old token graduates** | A dev's previous pump.fun token hits 100% bonding | Revival signal |
| **Repeat buyer wallets** | Same wallets that bought their last runner are buying this one | Insider follow-through |
| **Social velocity** | X post gets 100+ likes in 5 min | Community momentum |
| **Dev score** | Weighted score: migration_rate × avg_ATH × rug_penalty | Quality filter |

### Implementation Phases

#### Phase 1 — On-Chain Creation Monitor (1–2 days)
- [ ] Create `dev_wallets.json` with registry structure
- [ ] Add `DEV_TRACKED_WALLETS` set to existing WebSocket worker
- [ ] Subscribe to pump.fun program logs (`logsSubscribe`)
- [ ] Parse `create` instruction — extract creator pubkey
- [ ] If creator is in registry → trigger alert
- [ ] Fetch new token metadata (name, ticker, CA)
- [ ] Format and send Discord embed to `#alpha-calls` channel

#### Phase 2 — X/Twitter Monitor + Enrichment (3–5 days)
- [ ] Poll Nitter RSS for each dev's Twitter feed every 30s
- [ ] Regex scan for Solana CA patterns in tweet text
- [ ] Enrich alert with dev's historical tokens + ATH via DexScreener
- [ ] Add KOL cross-buy signal: if a KOL buys within 5 min, fire a follow-up embed

#### Phase 3 — Management & Scoring (Advanced)
- [ ] Supabase table for dev registry (CRUD via slash commands)
- [ ] `/adddev <wallet> <twitter>` command for mods
- [ ] `/devstats <wallet>` to view dev's full history
- [ ] Dev score algorithm: `score = (migration_rate * 40) + (avg_ath_log * 40) - (rug_count * 20)`
- [ ] Daily leaderboard of top-performing tracked devs

### New Files Needed
```
dev_wallets.json        # Dev registry
alpha_tracker.py        # WebSocket worker for pump.fun creation events
twitter_monitor.py      # Nitter RSS polling loop
```

---

## 2. 🔍 Social & Domain Due Diligence Tool

### Concept
A `/check` command that takes either an X handle or website domain and returns a full rug-risk audit — account age, username change history, domain registration date, privacy protection, and more.

### Commands
```
/check @handle              → Full X account audit
/check example.xyz          → Full domain WHOIS + archive check
/check @handle example.xyz  → Both at once
```

### Example Embed
```
🔎 Social Check: @devhandle

👤 TWITTER / X ACCOUNT
━━━━━━━━━━━━━━━━━━━━━
📅 Created: Mar 14, 2024 (134 days ago) ⚠️
👥 Followers: 1,240 | Following: 4,800 🚨
🐦 Tweets: 23 (very low activity) ⚠️
📸 First Archive Snapshot: May 2, 2025 🚨
🔄 Username History: Was @oldname123 in archived snapshot 🚨
📝 Bio: "Crypto | Web3 | Building the future"

🌐 DOMAIN: example.xyz
━━━━━━━━━━━━━━━━━━━━━
📅 Registered: 2 days ago 🚨
⏳ Expires: 2026-07-25
🔒 Owner Privacy: Protected / Hidden ⚠️
🏢 Registrar: Namecheap Inc.
🕸️ First Web Archive: Never indexed 🚨

━━━━━━━━━━━━━━━━━━━━━
🧠 RISK SCORE: 8 / 10 — ⛔ HIGH RISK
Red flags: New account, username changed, domain just registered, hidden owner

[View X Archive] [Full WHOIS] [Wayback Machine]
```

### APIs Used (All Free, No API Key Required)

| API | Endpoint | Data |
|---|---|---|
| **RDAP** (domain info) | `https://rdap.org/domain/{domain}` | Registration date, registrar, expiry, privacy status |
| **Wayback Machine CDX** | `http://web.archive.org/cdx/search/cdx?url=twitter.com/{handle}*` | First snapshot date, historical username detection |
| **Wayback Availability** | `https://archive.org/wayback/available?url={domain}` | Earliest domain snapshot |
| **Nitter** (X scraper) | `https://nitter.net/{handle}` | Followers, tweets, creation date, bio |

### Risk Scoring Logic

| Check | Points |
|---|---|
| Account age < 30 days | +3 |
| Account age 30–90 days | +1 |
| Following >> Followers (ratio > 3×) | +2 |
| Tweet count < 20 | +1 |
| Username changed (Wayback diff) | +2 |
| Domain registered < 7 days ago | +3 |
| Domain registered < 30 days ago | +1 |
| Owner privacy / hidden | +1 |
| Domain never archived | +1 |

**Score 0–3:** 🟢 Low Risk  
**Score 4–5:** 🟡 Medium Risk  
**Score 6–7:** 🟠 High Risk  
**Score 8–10:** 🔴 Extreme Risk

### Username Change Detection (Wayback Method)
```python
# Query CDX API for all archived snapshots of the Twitter profile
url = (
    "http://web.archive.org/cdx/search/cdx"
    f"?url=twitter.com/{handle}&output=json"
    "&limit=5&fl=timestamp,original&from=20100101"
)
# If earliest snapshot URL redirected to a different handle → username changed
```

### Implementation Plan
- [ ] `get_twitter_stats(handle)` in `api_client.py` — Nitter scrape (creation date, followers, tweets, bio)
- [ ] `get_wayback_history(url)` in `api_client.py` — CDX API for first snapshot + redirect diff
- [ ] `get_domain_info(domain)` in `api_client.py` — RDAP lookup (free, no key)
- [ ] `calculate_risk_score(twitter_data, domain_data)` helper
- [ ] `/check` slash command in `bot.py` with full embed + risk score + buttons

---

## 3. 📢 CT (Crypto Twitter) Call Tracker

### Concept
Monitor a curated list of influential Crypto Twitter accounts. Whenever they **post or mention a token CA**, auto-detect it, enrich it with on-chain data, and fire a Discord alert. Over time, track each caller's **accuracy** (did their calls actually pump?) to build a credibility score — so users know whose calls are worth following.

> **Key difference from Alpha Call Tracker:**  
> - **Alpha Call Tracker** = tracks DEVs who *create* tokens  
> - **CT Tracker** = tracks CALLERS/INFLUENCERS who *shill* tokens they didn't create

### Example Alert
```
📢 CT CALL DETECTED — @KOLhandle just called a coin!

🐦 Caller: @KOLhandle (124K followers)
🏆 Call Accuracy: 67% win rate | Avg 4.2× return on calls
📊 Recent: $BONK +800% ✅ | $SILLY +120% ✅ | $SCAM -95% ❌

🆕 Called Token: $NEWCOIN
📝 CA: Abc123...xyz789
💬 Tweet: "this one is going to be insane, low cap gem →
         Abc123...xyz789" [View Tweet →]

💰 Current MC: $320K | Liq: $45K | Age: 4h
🔒 RugCheck: LOW RISK (no mint, no freeze)

[Pump.fun] [Padre] [Axiom] [RugCheck]
```

### Signal Detection

| Method | How | Latency |
|---|---|---|
| **Nitter RSS poll** | Poll each tracked account's RSS feed every 30–60s, scan for Solana CA regex | ~30–60s |
| **Nitter keyword search** | Search for CA pattern across all CT at once | ~60s |
| **Manual add** | Mod posts CA in a designated Discord channel, bot auto-enriches it | Instant |

### CT Registry Schema (`ct_callers.json`)
```json
[
  {
    "twitter_handle": "influential_ct",
    "display_name": "InfluencerName",
    "followers": 124000,
    "tier": "A",
    "calls": [
      {
        "ticker": "BONK",
        "ca": "DezX...",
        "called_at": "2025-11-01T10:00:00Z",
        "mc_at_call": 50000,
        "peak_mc": 450000,
        "result": "win",
        "return_x": 9.0
      }
    ],
    "win_rate": 0.67,
    "avg_return_x": 4.2,
    "added_at": "2026-07-26"
  }
]
```

### Caller Tier System

| Tier | Criteria | Alert Style |
|---|---|---|
| 🥇 **S-Tier** | > 75% win rate, > 10× avg return | Ping `@everyone` |
| 🥈 **A-Tier** | > 60% win rate, > 5× avg return | Ping `@here` |
| 🥉 **B-Tier** | > 45% win rate, > 2× avg return | No ping, just embed |
| ⚠️ **C-Tier** | < 45% win rate | Labelled as low-accuracy caller |

### Accuracy Tracking
- When a call is detected, store `{ca, mc_at_call, called_at}` in Supabase
- A background job checks the token's MC every hour for 48 hours
- If peak MC > 2× call MC → mark as **win**; else → **loss**
- Recalculate `win_rate` and `avg_return_x` after each resolved call
- `/callerstats @handle` command to view a caller's full history

### Additional Signals

| Signal | Description |
|---|---|
| **Multiple callers, same coin** | If 3+ tracked CT accounts call same CA within 1 hour → fire **CONSENSUS CALL** mega-alert |
| **Caller + KOL wallet buy** | CT calls a coin AND a tracked KOL wallet buys it → double confirmation |
| **Caller's own wallet buys first** | If caller's wallet bought the token before tweeting → insider pump signal 🚨 |
| **Call velocity** | Tweet gets 500+ likes in 10 min → social momentum alert |
| **Repeat call** | Caller mentions same CA again hours later → conviction signal |

### Implementation Phases

#### Phase 1 — CT Registry + Nitter Polling (2–3 days)
- [ ] Create `ct_callers.json` with initial list of 50–100 accounts
- [ ] Build `twitter_monitor.py` Nitter RSS poller (shared with Alpha Tracker)
- [ ] Scan each tweet for Solana CA regex pattern `[1-9A-HJ-NP-Za-km-z]{32,44}`
- [ ] On match: fetch token data (DexScreener + RugCheck), fire Discord embed
- [ ] Store call in Supabase for accuracy tracking

#### Phase 2 — Accuracy Engine (3–5 days)
- [ ] Background task: check called token MC every 1h for 48h
- [ ] Auto-resolve calls as win/loss, update caller stats in Supabase
- [ ] `/callerstats @handle` slash command to view full call history embed
- [ ] `/topcallers` command — leaderboard of most accurate CT accounts

#### Phase 3 — Advanced Signals (1 week)
- [ ] Consensus call detection (3+ callers, same CA, < 1 hour window)
- [ ] Cross-reference caller's known wallets against on-chain buy events
- [ ] Tiered ping system (S-Tier → `@everyone`, B-Tier → silent embed)
- [ ] Daily CT performance digest posted automatically each morning

### New Files Needed
```
ct_callers.json         # CT caller registry with stats
twitter_monitor.py      # Nitter RSS poller (shared with Alpha Tracker)
```

---

## 🗺️ Feature Roadmap

| Priority | Feature | Status |
|---|---|---|
| ✅ Done | Token Analyser (`/ca`) | Shipped |
| ✅ Done | KOL Wallet Tracker | Shipped |
| ✅ Done | Custom Wallet Tracker | Shipped |
| ✅ Done | DEX Paid status in coin card | Shipped |
| ✅ Done | Fresh Wallets count in coin card | Shipped |
| ✅ Done | Wallet tracker rich embed (buyer, CA, wallet address, links) | Shipped |
| 🔜 Next | Alpha Call Tracker — Phase 1 (on-chain launch detection) | Planned |
| 🔜 Next | Social/Domain Due Diligence Tool (`/check`) | Planned |
| 🔜 Next | CT Tracker — Phase 1 (Nitter polling + CA detection) | Planned |
| ⏳ Later | CT Tracker — Phase 2 (accuracy engine + leaderboard) | Planned |
| ⏳ Later | Alpha Call Tracker — Phase 2 (X monitor + enrichment) | Planned |
| ⏳ Later | Consensus call detection (3+ CT on same coin) | Planned |
| ⏳ Later | Dev score + leaderboard | Planned |
| ⏳ Later | KOL cross-buy confirmation signal | Planned |

---

## 🛠️ Running the Bot

```bat
REM Windows
run.bat

REM Or manually
cd "e:\main data\Desktop\discordbot"
venv\Scripts\python.exe bot.py
```

---

## 📝 Technical Notes

- The bot uses **two Helius API keys** to split load: Key 1 for WebSockets, Key 2 for HTTP RPC.
- `TOKEN_CACHE` prevents hitting DexScreener rate limits — 5-minute TTL per token mint address.
- `RPC_SEMAPHORE(5)` limits concurrent `getTransaction` calls to avoid Helius throttling.
- KOL wallets are defined in `kol.js` (not Python) — parsed by the bot at startup.
- User tracker subscriptions are stored in **Supabase** for persistence across restarts.
- `pending_alerts` dict aggregates multiple buys of the same token within a 5s window before sending — prevents alert spam.
