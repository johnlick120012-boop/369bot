import discord
from discord.ext import commands
from discord import app_commands
import re
import time
from typing import Optional, List, Dict, Any
import os
import json
import asyncio
import aiohttp
import websockets
from telethon import TelegramClient, events

from config import logger, DISCORD_TOKEN, COMMAND_PREFIX, get_chain_config, DEFAULT_COLOR
import api_client
import datetime
import premium_db
import insider_analyzer


# Define regex patterns for contract addresses
EVM_REGEX = re.compile(r"\b(0x[a-fA-F0-9]{40})\b")
SOL_REGEX = re.compile(r"\b([1-9A-HJ-NP-Za-km-z]{32,44})\b")

def extract_ca(text: str) -> Optional[str]:
    """Helper to extract a contract address (Solana or EVM) from a string/URL."""
    evm_matches = EVM_REGEX.findall(text)
    sol_matches = SOL_REGEX.findall(text)
    all_matches = evm_matches + sol_matches
    return all_matches[0] if all_matches else None

def get_solana_rpc_urls():
    """Resolves Solana RPC URLs. Uses split keys: Key 1 for WebSocket, Key 2 for HTTP RPC."""
    key1 = (os.getenv("HELIUS_API_KEY") or "").strip()
    key2 = (os.getenv("HELIUS_API_KEY_2") or "").strip()
    
    # HTTP RPC uses Key 2 if available, otherwise Key 1
    rpc_key = key2 or key1
    # WebSocket always uses Key 1
    wss_key = key1
    
    if rpc_key:
        http_url = f"https://mainnet.helius-rpc.com/?api-key={rpc_key}"
    else:
        http_url = os.getenv("SOLANA_RPC_HTTP", "https://api.mainnet-beta.solana.com")
    
    if wss_key:
        wss_url = f"wss://mainnet.helius-rpc.com/?api-key={wss_key}"
    else:
        wss_url = os.getenv("SOLANA_RPC_WSS", "wss://api.mainnet-beta.solana.com")
    
    return http_url, wss_url

def get_solana_wss_urls() -> List[str]:
    """Returns a list of WebSocket URLs to try, in order of priority."""
    key1 = (os.getenv("HELIUS_API_KEY") or "").strip()
    key2 = (os.getenv("HELIUS_API_KEY_2") or "").strip()
    
    urls = []
    if key1:
        urls.append(f"wss://mainnet.helius-rpc.com/?api-key={key1}")
    if key2:
        urls.append(f"wss://mainnet.helius-rpc.com/?api-key={key2}")
    
    fallback = os.getenv("SOLANA_RPC_WSS")
    if fallback:
        urls.append(fallback)
    else:
        urls.append("wss://api.mainnet-beta.solana.com")
        
    return urls

# Discord intents setup
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.messages = True

# Bot initialization
bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents, help_command=None)

# ----------------- FORMATTING HELPERS -----------------

def format_price(price: Any) -> str:
    """Formats price into a highly readable format, supporting subscript notation for small values."""
    if price is None:
        return "N/A"
    try:
        val = float(price)
        if val == 0:
            return "$0.00"
        if val >= 1.0:
            return f"${val:,.4f}"
        
        # Format tiny numbers with subscript zeros
        s = f"{val:.12f}"
        parts = s.split('.')
        if len(parts) == 2:
            decimals = parts[1]
            zeros = len(decimals) - len(decimals.lstrip('0'))
            if zeros >= 4:
                remaining = decimals.lstrip('0')[:4]
                subscripts = {"0": "₀", "1": "₁", "2": "₂", "3": "₃", "4": "₄", "5": "₅", "6": "₆", "7": "₇", "8": "₈", "9": "₉"}
                sub_str = "".join(subscripts[c] for c in str(zeros))
                return f"$0.0{sub_str}{remaining}"
            else:
                return f"${val:.6f}"
        return f"${val:.6f}"
    except Exception:
        return f"${price}"

def format_large_number(num: Any) -> str:
    """Formats large currency or supply values (e.g. millions, billions)."""
    if num is None:
        return "N/A"
    try:
        val = float(num)
        if val >= 1_000_000_000:
            return f"${val / 1_000_000_000:,.2f}B"
        if val >= 1_000_000:
            return f"${val / 1_000_000:,.2f}M"
        if val >= 1_000:
            return f"${val / 1_000:,.2f}K"
        return f"${val:,.2f}"
    except Exception:
        return f"${num}"

def format_percentage(val: Any) -> str:
    """Formats percentage changes with trending emojis."""
    if val is None:
        return "0.00% ➡️"
    try:
        f_val = float(val)
        sign = "+" if f_val > 0 else ""
        emoji = "📈" if f_val > 0 else ("📉" if f_val < 0 else "➡️")
        return f"{sign}{f_val:.2f}% {emoji}"
    except Exception:
        return f"{val}%"

def format_age(created_at_ms: Any) -> str:
    """Formats a Unix ms timestamp into a human-readable age string (e.g. '3h 42m ago')."""
    if not created_at_ms:
        return "Unknown"
    try:
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
        created = datetime.datetime.fromtimestamp(int(created_at_ms) / 1000, tz=datetime.timezone.utc)
        total_seconds = max(0, int((now - created).total_seconds()))
        if total_seconds < 60:
            return f"{total_seconds}s ago"
        elif total_seconds < 3600:
            m, s = divmod(total_seconds, 60)
            return f"{m}m {s}s ago"
        elif total_seconds < 86400:
            h, rem = divmod(total_seconds, 3600)
            m = rem // 60
            return f"{h}h {m}m ago"
        else:
            d, rem = divmod(total_seconds, 86400)
            h = rem // 3600
            return f"{d}d {h}h ago"
    except Exception:
        return "Unknown"

def get_bubblemaps_url(chain_id: str, address: str) -> Optional[str]:
    """Generates the URL to visualize the token on Bubblemaps V2."""
    if not address:
        return None
    chain_lower = chain_id.lower()
    if chain_lower in ("solana", "sol"):
        return f"https://app.bubblemaps.io/solana/token/{address}"
    elif chain_lower in ("ethereum", "eth"):
        return f"https://app.bubblemaps.io/eth/token/{address}"
    elif chain_lower == "base":
        return f"https://app.bubblemaps.io/base/token/{address}"
    elif chain_lower in ("bsc", "binance"):
        return f"https://app.bubblemaps.io/bsc/token/{address}"
    return None

def get_distribution_stats(rug_report: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Extracts sniper, insider, and bundler distribution percentages from a RugCheck report.
    
    Uses insiderNetworks tokenAmount / token.supply for accurate insider and bundler %,
    and risks[] array for explicit flags.
    """
    stats: Dict[str, Any] = {
        "bundler_pct": None,
        "bundler_wallets": 0,
        "bundler_clusters": 0,
        "sniper_pct": None,
        "insider_pct": 0.0,
        "insider_count": 0,
        "insider_networks": 0,
        "total_holders": 0,
    }
    if not rug_report:
        return stats

    stats["total_holders"] = rug_report.get("totalHolders", 0) or 0

    # Parse bundler and sniper % from the risks array
    for risk in rug_report.get("risks", []):
        name_lower = risk.get("name", "").lower()
        val = risk.get("value", "")
        if "bundle" in name_lower:
            stats["bundler_pct"] = val if val else "Detected"
        elif "sniper" in name_lower or "bot" in name_lower:
            stats["sniper_pct"] = val if val else "Detected"

    # --- Insider & Bundler % from insiderNetworks (the REAL source) ---
    # graphInsidersDetected = total wallets flagged as insiders
    # insiderNetworks = list of clusters, each with tokenAmount (raw, same decimals as supply)
    insider_detected = rug_report.get("graphInsidersDetected", 0) or 0
    insider_networks = rug_report.get("insiderNetworks") or []
    token_data = rug_report.get("token", {})
    total_supply = float(token_data.get("supply", 0)) if token_data else 0

    # Separate transfer-type networks (= bundler wallets) from others
    transfer_networks = [n for n in insider_networks if n.get("type") == "transfer"]
    total_linked_wallets = sum(int(n.get("size", 0)) for n in insider_networks)

    if insider_networks and total_supply > 0:
        total_insider_amount = sum(float(net.get("tokenAmount", 0)) for net in insider_networks)
        cluster_pct = (total_insider_amount / total_supply) * 100.0
        
        stats["insider_pct"] = cluster_pct
        stats["insider_count"] = insider_detected or total_linked_wallets
        stats["insider_networks"] = len(insider_networks)

        # If risks[] didn't flag bundlers but we have transfer networks, calculate bundler % from those
        if not stats["bundler_pct"] and transfer_networks:
            transfer_amount = sum(float(n.get("tokenAmount", 0)) for n in transfer_networks)
            transfer_pct = (transfer_amount / total_supply) * 100.0
            transfer_wallets = sum(int(n.get("size", 0)) for n in transfer_networks)
            stats["bundler_pct"] = f"{transfer_pct:.1f}%"
            stats["bundler_wallets"] = transfer_wallets
            stats["bundler_clusters"] = len(transfer_networks)
        else:
            # We treat all linked networks as bundlers/clusters
            stats["bundler_pct"] = f"{cluster_pct:.1f}%"
            stats["bundler_wallets"] = total_linked_wallets
            stats["bundler_clusters"] = len(insider_networks)

    elif insider_detected > 0:
        # Fallback: we know insiders exist but can't calculate exact %
        stats["insider_count"] = insider_detected
        stats["insider_networks"] = len(insider_networks)
        # Try topHolders insider flag as last resort
        top_holders = rug_report.get("topHolders", [])
        insider_wallets = [h for h in top_holders if h.get("insider") is True]
        if insider_wallets:
            stats["insider_pct"] = sum(h.get("pct", 0.0) for h in insider_wallets)

    stats["total_linked_wallets"] = total_linked_wallets
    return stats

# ----------------- EMBED GENERATORS -----------------

# ----------------- SECURITY -----------------

def analyze_security(pair: Dict[str, Any], rug_report: Optional[Dict[str, Any]], gmgn_security: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Evaluates security flags, developer centralization, and trading activity metrics."""
    chain_id = pair.get("chainId", "")
    base_token = pair.get("baseToken", {})
    mcap = pair.get("marketCap")
    liq = pair.get("liquidity", {}).get("usd")
    volume = pair.get("volume", {}).get("h24")
    txns = pair.get("txns", {}).get("h24", {})
    price_change = pair.get("priceChange", {})
    
    buys = txns.get("buys", 0)
    sells = txns.get("sells", 0)
    total_tx = buys + sells
    
    warnings = []
    danger_flags = []
    
    # 1. General Metrics Check (Liquidity, Fake Vol, Big Pumps)
    try:
        mcap_val = float(mcap) if mcap else 0
        liq_val = float(liq) if liq else 0
        vol_val = float(volume) if volume else 0
    except Exception:
        mcap_val, liq_val, vol_val = 0, 0, 0
        
    if mcap_val > 50000 and liq_val > 0:
        if liq_val < 3000:
            danger_flags.append("🚨 **Scam Red Alert**: Extremely low liquidity compared to Market Cap!")
        elif liq_val < 10000:
            warnings.append("⚠️ **Low Liquidity**: High risk of price impact.")
            
    # Ratio Check (e.g. if MC is extremely disproportionate to liquidity)
    if mcap_val > 10000 and liq_val > 0:
        ratio = mcap_val / liq_val
        if ratio > 15.0:
            danger_flags.append(f"🚨 **Extremely Low Liquidity Ratio**: Market Cap is {ratio:.1f}x higher than liquidity!")
        elif ratio > 8.0:
            warnings.append(f"⚠️ **Low Liquidity Ratio**: Market Cap is {ratio:.1f}x higher than liquidity.")
            
    if vol_val > 50000 and total_tx < 30:
        danger_flags.append("\U0001f6a8 **Fake Volume**: High volume with very few transactions (Wash Trading bot detected!).")
        
    if mcap_val > 150000 and total_tx < 15:
        danger_flags.append("\U0001f6a8 **Fake Market Cap**: High MC with almost zero trading activity.")
        
    # Estimated 24h Fees Paid check (volume * ~1% pool fee)
    est_fees_24h = vol_val * 0.01
    if mcap_val > 50000 and est_fees_24h < 50:
        danger_flags.append(f"\U0001f6a8 **Suspiciously Low Fees**: MC is {format_large_number(mcap_val)} but est. 24h fees are only ${est_fees_24h:.2f}. Fake/dead market cap!")
    elif mcap_val > 20000 and est_fees_24h < 20:
        warnings.append(f"\u26a0\ufe0f **Low Fee Generation**: MC is {format_large_number(mcap_val)} but est. 24h fees are only ${est_fees_24h:.2f}.")

    try:
        change_5m = float(price_change.get("m5", 0))
    except Exception:
        change_5m = 0.0
        
    if change_5m > 50.0:
        warnings.append(f"\U0001f680 **Big Candle Pump**: +{change_5m:.1f}% in last 5m. Watch for sniper/bundle dumps!")
        
    # 2. Solana specific (RugCheck / GMGN API analysis)
    score = 0
    dev_holdings = 0.0
    top_10_pct = 0.0
    top_holders = []
    
    # Initialize Solana specifics
    mint_status = "Unknown"
    freeze_status = "Unknown"
    lp_status = "Unknown"
    
    if chain_id == "solana":
        # 2a. Check GMGN Security if available
        if gmgn_security:
            if gmgn_security.get("is_honeypot") is True or gmgn_security.get("is_honeypot") == 1:
                danger_flags.append("🚨 **Honeypot Danger**: GMGN detects that token sells are disabled / honeypot!")
                
            if gmgn_security.get("cannot_mint") is True or gmgn_security.get("cannot_mint") == 1:
                mint_status = "✅ Revoked"
            elif gmgn_security.get("cannot_mint") is False or gmgn_security.get("cannot_mint") == 0:
                mint_status = "🚨 Active (Dev can mint)"
                danger_flags.append("🚨 **Mint Authority Enabled**: Developer can print new tokens (detected by GMGN)!")
                
            if gmgn_security.get("cannot_freeze") is True or gmgn_security.get("cannot_freeze") == 1:
                freeze_status = "✅ Revoked"
            elif gmgn_security.get("cannot_freeze") is False or gmgn_security.get("cannot_freeze") == 0:
                freeze_status = "🚨 Active (Dev can freeze)"
                danger_flags.append("🚨 **Freeze Authority Enabled**: Developer can freeze your tokens (detected by GMGN)!")
                
            burn_ratio_str = gmgn_security.get("burn_ratio") or "0"
            try:
                burn_pct = float(burn_ratio_str) * 100.0
                if burn_pct >= 95.0:
                    lp_status = f"✅ Burnt/Locked ({burn_pct:.1f}%)"
                elif burn_pct > 0.0:
                    lp_status = f"⚠️ Partially Locked ({burn_pct:.1f}%)"
                    warnings.append(f"⚠️ **Liquidity Pool Only Partially Locked**: {100 - burn_pct:.1f}% remains unlocked (detected by GMGN)!")
                else:
                    if pair.get("dexId") == "pumpfun":
                        lp_status = "💊 pump.fun Bonding Curve"
                    else:
                        lp_status = "🚨 Unlocked (0% Burnt/Locked)"
                        danger_flags.append("🚨 **Liquidity Unlocked**: Creator can pull the liquidity pool at any time!")
            except Exception:
                pass

            top_10_rate = gmgn_security.get("top_10_holder_rate")
            if top_10_rate and not rug_report:
                try:
                    top_10_pct = float(top_10_rate) * 100.0
                    if top_10_pct > 50.0:
                        danger_flags.append(f"🚨 **Extreme Concentration**: Top 10 wallets control {top_10_pct:.1f}% of supply.")
                    elif top_10_pct > 21.0:
                        warnings.append(f"⚠️ **High Concentration**: Top 10 wallets control {top_10_pct:.1f}% of supply.")
                except ValueError:
                    pass

        # 2b. Check RugCheck if available
        if rug_report:
            score = rug_report.get("score", 0)
            creator = rug_report.get("creator", "")
            
            top_holders = rug_report.get("topHolders", [])
            known_accounts = rug_report.get("knownAccounts", {})
            if not isinstance(known_accounts, dict):
                known_accounts = {}
            
            for holder in top_holders:
                owner = holder.get("owner", "")
                if owner == creator or holder.get("address") == creator:
                    dev_holdings += holder.get("pct", 0.0)
                    
            if dev_holdings > 20.0:
                if not any("Dev Centralization" in f for f in danger_flags):
                    danger_flags.append(f"🚨 **Dev Centralization**: Creator holds {dev_holdings:.1f}% of supply (Potential Rug!).")
            elif dev_holdings > 5.0:
                if not any("Dev Holdings" in f for f in warnings):
                    warnings.append(f"⚠️ **Dev Holdings**: Creator holds {dev_holdings:.1f}% of supply.")
                
            non_pool_holders = []
            for holder in top_holders:
                owner = holder.get("owner", "")
                is_known = False
                if owner in known_accounts:
                    acc_info = known_accounts[owner]
                    tag = acc_info.get("type", "").lower() if isinstance(acc_info, dict) else ""
                    if "dex" in tag or "pool" in tag or "liquidity" in tag or "amm" in tag:
                        is_known = True
                if not is_known:
                    non_pool_holders.append(holder)
                    
            for holder in non_pool_holders[:10]:
                pct = holder.get("pct", 0.0)
                if pct > 10.0:
                    danger_flags.append(f"🚨 **Whale/Insider Danger**: Wallet `{holder.get('owner')[:6]}...` holds {pct:.1f}% of supply!")
                elif pct > 5.0:
                    danger_flags.append(f"🚨 **High Risk Holder**: Wallet `{holder.get('owner')[:6]}...` holds {pct:.1f}% of supply.")
                elif pct > 3.0:
                    warnings.append(f"⚠️ **Medium Risk Holder**: Wallet `{holder.get('owner')[:6]}...` holds {pct:.1f}% of supply.")
                    
            top_10_pct = sum(h.get("pct", 0.0) for h in non_pool_holders[:10])
            if top_10_pct > 50.0:
                if not any("Extreme Concentration" in f for f in danger_flags):
                    danger_flags.append(f"🚨 **Extreme Concentration**: Top 10 wallets control {top_10_pct:.1f}% of supply (Very high dump risk!).")
            elif top_10_pct > 21.0:
                if not any("High Concentration" in f for f in warnings):
                    warnings.append(f"⚠️ **High Concentration**: Top 10 wallets control {top_10_pct:.1f}% of supply (Potential insider/sniper accumulation).")
            elif top_10_pct > 12.0:
                if not any("Medium Concentration" in f for f in warnings):
                    warnings.append(f"⚠️ **Medium Concentration**: Top 10 wallets control {top_10_pct:.1f}% of supply.")
                
            insider_wallets = [h for h in top_holders if h.get("insider") is True]
            insider_pct = sum(h.get("pct", 0.0) for h in insider_wallets)
            if insider_wallets:
                warnings.append(f"⚠️ **Connected Insider Wallets**: {len(insider_wallets)} wallets with transactional links to deployer control {insider_pct:.1f}% of supply.")
                
            for risk in rug_report.get("risks", []):
                name = risk.get("name", "")
                level = risk.get("level", "")
                val = risk.get("value", "")
                val_str = f" ({val})" if val else ""
                
                if level == "danger":
                    if not any(name in f for f in danger_flags):
                        danger_flags.append(f"🚨 **{name}**{val_str}")
                elif level == "warn":
                    if not any(name in f for f in warnings):
                        warnings.append(f"⚠️ **{name}**{val_str}")
                    
            if mint_status == "Unknown":
                mint_auth = rug_report.get("mintAuthority")
                if mint_auth is None:
                    mint_status = "✅ Revoked"
                else:
                    mint_status = "🚨 Active (Dev can mint)"
                    danger_flags.append("🚨 **Mint Authority Enabled**: Developer can print new tokens!")
                
            if freeze_status == "Unknown":
                freeze_auth = rug_report.get("freezeAuthority")
                if freeze_auth is None:
                    freeze_status = "✅ Revoked"
                else:
                    freeze_status = "🚨 Active (Dev can freeze)"
                    danger_flags.append("🚨 **Freeze Authority Enabled**: Developer can freeze your tokens!")
                
            if lp_status == "Unknown":
                lp_locked_pct = 0.0
                markets = rug_report.get("markets", [])
                if markets:
                    primary_market = markets[0]
                    lp_info = primary_market.get("lp", {})
                    if lp_info:
                        lp_locked_pct = lp_info.get("lpLockedPct", 0.0)
                        
                if lp_locked_pct >= 95.0:
                    lp_status = f"✅ Burnt/Locked ({lp_locked_pct:.1f}%)"
                elif lp_locked_pct > 0.0:
                    lp_status = f"⚠️ Partially Locked ({lp_locked_pct:.1f}%)"
                    warnings.append(f"⚠️ **Liquidity Pool Only Partially Locked**: {100 - lp_locked_pct:.1f}% remains unlocked!")
                else:
                    if pair.get("dexId") != "pumpfun":
                        lp_status = "🚨 Unlocked (0% Burnt/Locked)"
                        danger_flags.append("🚨 **Liquidity Unlocked**: Creator can pull the liquidity pool at any time!")
                    else:
                        lp_status = "💊 pump.fun Bonding Curve"
            
    # Build structured stats header
    stats_lines = []
    if chain_id == "solana" and rug_report:
        stats_lines.append(f"• **Mint Authority:** {mint_status}")
        stats_lines.append(f"• **Freeze Authority:** {freeze_status}")
        stats_lines.append(f"• **Liquidity Burnt/Locked:** {lp_status}")
        stats_lines.append(f"• **Creator Holdings:** `{dev_holdings:.2f}%` of supply")
        stats_lines.append(f"• **Top 10 Wallets:** `{top_10_pct:.2f}%` of supply")
        
        # Insiders & Sniper wallets
        insider_wallets = [h for h in top_holders if h.get("insider") is True]
        insider_pct = sum(h.get("pct", 0.0) for h in insider_wallets)
        if insider_wallets:
            stats_lines.append(f"• **Insiders/Snipers:** ⚠️ `{len(insider_wallets)}` wallets control `{insider_pct:.2f}%` of supply")
        else:
            insiders_flag = rug_report.get("graphInsidersDetected", False)
            if insiders_flag:
                stats_lines.append("• **Insiders/Snipers:** ⚠️ Insider network detected via Graph")
            else:
                stats_lines.append("• **Insiders/Snipers:** ✅ Clean (No snipers/insiders detected)")
                
        stats_lines.append(f"• **RugScore:** `{score}/1000` (lower is safer)")
    else:
        stats_lines.append(f"• **Liquidity/MC Ratio:** `{((liq_val / mcap_val * 100) if mcap_val > 0 else 0):.1f}%`")
        stats_lines.append(f"• **24h Transactions:** `{total_tx}` trades")
        
    stats_text = "\n".join(stats_lines)
    
    # Combine findings — danger flags first, cap to avoid embed overflow
    MAX_FLAGS = 5
    combined = danger_flags[:MAX_FLAGS] + warnings[:max(0, MAX_FLAGS - len(danger_flags))]
    findings = combined
    if findings:
        findings_text = "\n\n**\u26a0\ufe0f Risks & Warnings:**\n" + "\n".join(findings)
        # Note if more were truncated
        total_all = len(danger_flags) + len(warnings)
        if total_all > MAX_FLAGS:
            findings_text += f"\n_...and {total_all - MAX_FLAGS} more risk(s) — use RugCheck for full report._"
    else:
        findings_text = "\n\n**\u2705 No risks flagged.**"
        
    desc = stats_text + findings_text
    
    if danger_flags:
        status = "🔴 SCAM RED ALERT" if any("Scam Red Alert" in d or "Fake Volume" in d or "Fake Market Cap" in d or "Freeze Authority" in d or "Unlocked" in d or "Enabled" in d for d in danger_flags) else "⚠️ HIGH RISK"
        color = 0xFF0000 if "SCAM" in status else 0xFF5500
    elif warnings:
        status = "🟡 MEDIUM RISK"
        color = 0xFFCC00
    else:
        status = "🟢 LOW RISK"
        color = 0x00FF00
        
    return {
        "status": status,
        "color": color,
        "description": desc[:1020] + "…" if len(desc) > 1020 else desc,
        "score": score
    }

async def get_active_holders_count(mint_address: str, market_cap: float, total_supply: float) -> int:
    """Queries Helius RPC to count token accounts with a balance >= $1 worth of tokens."""
    if not mint_address:
        return 0
    
    http_url, _ = get_solana_rpc_urls()
    if "helius-rpc.com" not in http_url:
        return 0
        
    payload = {
        "jsonrpc": "2.0",
        "id": "get-holders",
        "method": "getTokenAccounts",
        "params": {
            "mint": mint_address,
            "page": 1,
            "limit": 1000
        }
    }
    headers = {"Content-Type": "application/json"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(http_url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=5)) as r:
                if r.status == 200:
                    data = await r.json()
                    result = data.get("result", {})
                    accounts = result.get("token_accounts", [])
                    if not accounts:
                        return 0
                    
                    active_count = 0
                    for acc in accounts:
                        amount = float(acc.get("amount", 0))
                        if total_supply > 0 and market_cap > 0:
                            usd_val = (amount / total_supply) * market_cap
                            if usd_val >= 1.0:
                                active_count += 1
                        else:
                            if amount > 0:
                                active_count += 1
                                
                    if len(accounts) == 1000:
                        return -1000  # sentinel to indicate 1000+
                    return active_count
    except Exception as e:
        logger.error(f"Error in get_active_holders_count: {e}")
    return 0

# ----------------- EMBED GENERATORS -----------------

async def create_token_embed(pair: Dict[str, Any], rug_report: Optional[Dict[str, Any]] = None) -> discord.Embed:
    """Creates a beautiful, media-rich Embed from a DexScreener token pair dictionary."""
    import datetime
    base_token = pair.get("baseToken", {})
    quote_token = pair.get("quoteToken", {})
    chain_id = pair.get("chainId", "")
    dex_id = pair.get("dexId", "")
    ca_address = base_token.get("address", "")
    
    # Re-fetch rug report if Solana and not provided
    if chain_id == "solana" and rug_report is None:
        try:
            rug_report = await api_client.get_rugcheck_report(ca_address)
        except Exception as e:
            logger.error(f"Error fetching rug report for embed: {e}")
            
    ticker = base_token.get('symbol', 'Unknown').upper()

    # --- Token age (from DexScreener pairCreatedAt ms timestamp) ---
    pair_created_at = pair.get("pairCreatedAt")
    age_str = format_age(pair_created_at)
    age_seconds = 0
    if pair_created_at:
        try:
            age_seconds = max(0, int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000 - int(pair_created_at)) // 1000)
        except Exception:
            pass

    # --- Pump.fun coin data & dev info fetched in parallel for speed ---
    pump_coin_data: Optional[Dict[str, Any]] = None
    dev_info: Dict[str, Any] = {"total_coins": 0, "migrated_coins": 0, "twitter": None}
    dev_sol_balance: float = 0.0
    creator_address: str = ""
    dex_paid_info: Dict[str, Any] = {"has_paid": False, "order_types": [], "boost_active": 0}
    fresh_wallets_info: Dict[str, Any] = {"fresh_count": 0, "total_sampled": 0, "fresh_pct": 0.0}
    
    # GMGN specifics
    gmgn_security: Optional[Dict[str, Any]] = None
    gmgn_info: Optional[Dict[str, Any]] = None
    gmgn_holders: Optional[List[Dict[str, Any]]] = None
    
    # Check boosts from pair data (available for all chains)
    _boosts = pair.get("boosts", {})
    if _boosts and isinstance(_boosts, dict):
        dex_paid_info["boost_active"] = _boosts.get("active", 0)
    
    if chain_id == "solana":
        if rug_report:
            creator_address = rug_report.get("creator", "") or ""
        
        async def _fetch_pump_coin():
            try:
                _h = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
                async with aiohttp.ClientSession() as _s:
                    async with _s.get(f"https://frontend-api-v3.pump.fun/coins/{ca_address}", headers=_h, timeout=aiohttp.ClientTimeout(total=5)) as _r:
                        if _r.status == 200:
                            return await _r.json()
            except Exception as _e:
                logger.error(f"pump.fun fetch error in embed: {_e}")
            return None
        
        async def _fetch_dev_info():
            if creator_address:
                return await api_client.get_dev_info(creator_address)
            return {"total_coins": 0, "migrated_coins": 0, "twitter": None}
        
        async def _fetch_dev_balance():
            if creator_address:
                http_url, _ = get_solana_rpc_urls()
                return await api_client.get_sol_balance(creator_address, http_url)
            return 0.0
        
        async def _fetch_dex_paid():
            return await api_client.get_dex_paid_orders(chain_id, ca_address)
        
        async def _fetch_fresh_wallets():
            http_url, _ = get_solana_rpc_urls()
            return await api_client.get_fresh_wallets_count(ca_address, http_url)
            
        async def _fetch_gmgn_security():
            return await api_client.get_gmgn_token_security("solana", ca_address)
            
        async def _fetch_gmgn_info():
            return await api_client.get_gmgn_token_info("solana", ca_address)
            
        async def _fetch_gmgn_holders():
            return await api_client.get_gmgn_token_holders("solana", ca_address, limit=100)
        
        _results = await asyncio.gather(
            _fetch_pump_coin(), _fetch_dev_info(), _fetch_dev_balance(),
            _fetch_dex_paid(), _fetch_fresh_wallets(),
            _fetch_gmgn_security(), _fetch_gmgn_info(), _fetch_gmgn_holders(),
            return_exceptions=True
        )
        if not isinstance(_results[0], Exception):
            pump_coin_data = _results[0]
        if not isinstance(_results[1], Exception):
            dev_info = _results[1]
        if not isinstance(_results[2], Exception):
            dev_sol_balance = _results[2]
        if not isinstance(_results[3], Exception) and _results[3]:
            dex_paid_info.update(_results[3])
        if not isinstance(_results[4], Exception) and _results[4]:
            fresh_wallets_info = _results[4]
        if not isinstance(_results[5], Exception):
            gmgn_security = _results[5]
        if not isinstance(_results[6], Exception):
            gmgn_info = _results[6]
        if not isinstance(_results[7], Exception) and _results[7]:
            gmgn_holders = _results[7].get("list") if isinstance(_results[7], dict) else _results[7]
            
    else:
        # For non-Solana chains, still check DEX paid
        try:
            dex_paid_info.update(await api_client.get_dex_paid_orders(chain_id, ca_address))
        except Exception:
            pass
            
    # Run security evaluation after fetching all data
    sec = analyze_security(pair, rug_report, gmgn_security)
    
    # Use security color if it has warnings/dangers, otherwise use default chain color
    chain_cfg = get_chain_config(chain_id)
    embed_color = sec["color"] if sec["status"] != "🟢 LOW RISK" else chain_cfg["color"]

    # --- Distribution stats from RugCheck and GMGN ---
    dist = get_distribution_stats(rug_report) if chain_id == "solana" else {}
    if chain_id == "solana" and gmgn_info:
        gmgn_summary = insider_analyzer.summarize_gmgn(gmgn_security, gmgn_info, gmgn_holders)
        if gmgn_summary["source"]:
            dist["bundler_pct"] = f"{gmgn_summary['bundler_pct']:.1f}%"
            dist["sniper_pct"] = f"{gmgn_summary['sniper_pct']:.1f}%"
            dist["insider_pct"] = gmgn_summary["insider_pct"]
            
            # Find wallet counts from clusters
            for c in gmgn_summary["clusters"]:
                if c["type"] == "bundler":
                    dist["bundler_wallets"] = c["wallet_count"]
                    dist["bundler_clusters"] = 1
                elif c["type"] == "insider":
                    dist["insider_count"] = c["wallet_count"]
                    dist["insider_networks"] = 1
                    
            if gmgn_info.get("stat", {}).get("holder_count"):
                try:
                    dist["total_holders"] = int(gmgn_info["stat"]["holder_count"])
                except Exception:
                    pass

    # Fetch active holders count (value >= $1)
    active_holders = 0
    if chain_id == "solana":
        try:
            mcap_val = float(pair.get("marketCap") or 0)
            token_data = rug_report.get("token", {}) if rug_report else {}
            supply_val = float(token_data.get("supply", 0)) if token_data else 0
            if supply_val <= 0:
                supply_val = 1_000_000_000
            active_holders = await get_active_holders_count(ca_address, mcap_val, supply_val)
        except Exception as e:
            logger.error(f"Error calling get_active_holders_count in embed: {e}")
            
    if active_holders == -1000:
        holders_str = f" \u2022 \U0001f465 **Holders:** 1,000+"
    elif active_holders > 0:
        holders_str = f" \u2022 \U0001f465 **Holders:** {active_holders:,}"
    else:
        total_holders = dist.get("total_holders", 0) if dist else 0
        holders_str = f" \u2022 \U0001f465 **Holders:** {int(total_holders):,}" if total_holders > 0 else ""

    info = pair.get("info", {})
    image_url = info.get("imageUrl") if info else None
    
    # Try pump.fun fallback for newly launched coins
    if not image_url and pump_coin_data:
        image_url = pump_coin_data.get("image_uri")
        
    # Always reserve space for the coin logo by using a placeholder if still empty
    if not image_url:
        image_url = "https://i.imgur.com/f94QG4v.png"
        
    # Save resolved image URL in pair for the view buttons
    pair["resolved_image_url"] = image_url
    
    # Ensure ticker is always visible in the title
    token_name = base_token.get('name', 'Unknown Token')
    embed_title = f"\U0001f680 {token_name} ({ticker})"
    
    # Add Image Search option for the coin image/sticker
    lens_link = f" \u2022 [🔍 Image Search](https://lens.google.com/uploadbyurl?url={image_url})"

    embed = discord.Embed(
        title=embed_title,
        description=(
            f"**Ticker:** ${ticker}{lens_link}\n"
            f"**Chain:** {chain_cfg['name']} {chain_cfg['icon']} \u2022 **DEX:** {dex_id.upper()}\n"
            f"\U0001f4c5 **Created:** {age_str}{holders_str}"
        ),
        color=embed_color
    )
    
    # Image thumbnail is always set to ensure space is reserved
    embed.set_thumbnail(url=image_url)
        
    # Get statistics
    price_usd = pair.get("priceUsd")
    price_formatted = format_price(price_usd)
    price_native = pair.get("priceNative", "N/A")
    
    volume = pair.get("volume", {})
    vol_m5 = format_large_number(volume.get("m5"))
    vol_h1 = format_large_number(volume.get("h1"))
    vol_h24 = format_large_number(volume.get("h24"))
    
    # Fallback: grab twitter from DexScreener socials if not found on pump.fun
    if not dev_info.get("twitter"):
        _info = pair.get("info", {})
        if _info:
            for _soc in _info.get("socials", []):
                if _soc.get("type", "").lower() == "twitter":
                    dev_info["twitter"] = _soc.get("url")
                    break
    
    liquidity = pair.get("liquidity", {})
    if not liquidity or liquidity.get("usd") is None:
        if dex_id == "pumpfun":
            liq_usd = "\U0001f48a Bonding Curve"
            _cd = pump_coin_data
            if _cd:
                if _cd.get("complete"):
                    liq_usd = "\U0001f48a Bonding Curve (100% - Graduated)"
                else:
                    _real_sol = _cd.get("real_sol_reserves", 0)
                    _sv = _real_sol / 1e9 if _real_sol > 1000 else _real_sol
                    _prog = min(100.0, (_sv / 85.0) * 100.0)
                    liq_usd = f"\U0001f48a Bonding Curve ({_prog:.1f}%)"
        else:
            liq_usd = "N/A"
    else:
        liq_usd = format_large_number(liquidity.get("usd"))
        
    mcap = format_large_number(pair.get("marketCap"))
    fdv = format_large_number(pair.get("fdv"))
    
    # Price changes
    price_changes = pair.get("priceChange", {})
    change_5m = format_percentage(price_changes.get("m5"))
    change_1h = format_percentage(price_changes.get("h1"))
    change_6h = format_percentage(price_changes.get("h6"))
    change_24h = format_percentage(price_changes.get("h24"))
    
    # Txns
    txns = pair.get("txns", {})
    tx_m5 = txns.get("m5", {})
    tx_h1 = txns.get("h1", {})
    tx_h24 = txns.get("h24", {})
    
    # --- Exit / Rug / Mayhem / Stale Detection ---
    alerts: List[str] = []
    try:
        _vol_h1_val  = float(volume.get("h1") or 0)
        _vol_h24_val = float(volume.get("h24") or 0)
        _vol_m5_val  = float(volume.get("m5") or 0)
        _total_tx_h1 = tx_h1.get("buys", 0) + tx_h1.get("sells", 0)
        _change_24h_val = float(price_changes.get("h24") or 0)
        _change_1h_val  = float(price_changes.get("h1") or 0)
        _change_5m_val  = float(price_changes.get("m5") or 0)
        _mcap_val = float(pair.get("marketCap") or 0)

        # 1. Dead / Exit detected
        if _vol_h1_val < 50 and _total_tx_h1 < 5 and _change_24h_val < -50:
            alerts.append("\U0001f480 **DEAD / ABANDONED** \u2014 Volume & trades have completely stopped.")
        elif _vol_h1_val < 200 and _total_tx_h1 < 10 and _change_24h_val < -80:
            alerts.append("\U0001f6a8 **EXIT DETECTED** \u2014 Extreme dump + zero buy vol. Dev likely rugged.")
        elif _vol_h24_val > 5000 and _vol_h1_val < 100 and _change_24h_val < -60:
            alerts.append("\u26a0\ufe0f **SUSPICIOUS EXIT** \u2014 Vol suddenly stopped after active trading.")

        # 2. Stale low-MC coin (been around long but still tiny)
        age_hours = age_seconds / 3600
        if age_hours >= 2 and _mcap_val > 0 and _mcap_val < 5000 and _vol_h1_val < 100:
            alerts.append(
                f"\U0001f9fb **STALE LOW-CAP** \u2014 Coin is {age_str} old but MC is still "
                f"{format_large_number(_mcap_val)}. Likely dead or illiquid."
            )

        # 3. Pump.fun Mayhem Mode check
        if pump_coin_data:
            _mayhem = pump_coin_data.get("mayhem_state")
            if _mayhem == "active":
                alerts.append(
                    "\U0001f525 **MAYHEM MODE ACTIVE** \u2014 pump.fun AI trading agent is actively trading on this coin! "
                    "The agent is executing automated/randomized trades. Highly risky, avoid entering!"
                )
            elif _mayhem == "paused":
                alerts.append(
                    "\u26a0\ufe0f **MAYHEM MODE PAUSED** \u2014 pump.fun AI trading agent is currently paused on this coin."
                )

        # 4. Bot / Bundle pump detection (straight candle with no organic trading)
        _buys_m5 = tx_m5.get("buys", 0)
        _sells_m5 = tx_m5.get("sells", 0)
        _buys_h1 = tx_h1.get("buys", 0)
        _sells_h1 = tx_h1.get("sells", 0)
        _total_m5 = _buys_m5 + _sells_m5
        _total_h1 = _buys_h1 + _sells_h1

        # Straight candle in 5m: huge pump, almost no trades, near-zero sells
        if _change_5m_val > 80 and _total_m5 < 10 and _sells_m5 < 3:
            alerts.append(
                f"\U0001f916 **BOT/BUNDLE PUMP** \u2014 Price surged +{_change_5m_val:.0f}% in 5m with only "
                f"{_total_m5} trades and {_sells_m5} sells. Likely manipulated by bundler wallets."
            )
        # Sustained straight candle over 1h
        elif _change_1h_val > 150 and _total_h1 < 30 and _sells_h1 < 5:
            alerts.append(
                f"\U0001f916 **SUSTAINED BOT PUMP** \u2014 Price up +{_change_1h_val:.0f}% in 1h with only "
                f"{_total_h1} trades. Straight-line chart = likely bundled. Extreme dump risk!"
            )

        # 5. One-sided buying: overwhelming buys with almost no sells = coordinated bots
        if _buys_h1 > 20 and _sells_h1 <= 2 and _change_1h_val > 50:
            alerts.append(
                f"\u26a0\ufe0f **ONE-SIDED BUYING** \u2014 {_buys_h1} buys vs {_sells_h1} sells in 1h. "
                "No organic selling = likely coordinated bot buying. Dev controls the chart."
            )

        # 6. Suspicious avg trade size for small MC coins (whale/bundle accumulation)
        if _total_h1 > 0 and _mcap_val > 0 and _mcap_val < 50000:
            _avg_trade = _vol_h1_val / _total_h1 if _total_h1 > 0 else 0
            if _avg_trade > 500 and _total_h1 < 20:
                alerts.append(
                    f"\U0001f40b **SUSPICIOUS TRADE SIZE** \u2014 Avg trade is ${_avg_trade:.0f} for a "
                    f"{format_large_number(_mcap_val)} MC coin with only {_total_h1} trades. "
                    "Large buys in small coins = bundle/insider accumulation."
                )

        # 7. Coordinated Bundler Accumulation check
        if dist:
            _bund_str = dist.get("bundler_pct") or ""
            try:
                _bund_val = float(_bund_str.split("%")[0].strip())
            except Exception:
                _bund_val = 0.0
            
            _bund_wallets = dist.get("bundler_wallets", 0)
            _bund_clusters = dist.get("bundler_clusters", 0)
            
            if _bund_val > 15.0:
                alerts.append(
                    f"🚨 **HIGH BUNDLE RISK** \u2014 Coordinated bundler wallets control `{_bund_val:.1f}%` "
                    f"of supply (across `{_bund_wallets}` wallets in `{_bund_clusters}` clusters)!"
                )
            elif _bund_val >= 5.0 and _bund_wallets >= 3:
                alerts.append(
                    f"⚠️ **COORDINATED BUNDLE RISK** \u2014 Coordinated sniper/bundler accumulation detected! "
                    f"Wallets control `{_bund_val:.1f}%` of supply (across `{_bund_wallets}` wallets in `{_bund_clusters}` clusters), "
                    f"even though individual holdings are low."
                )
    except Exception:
        pass

    # --- Estimated 24h Fees Paid ---
    _vol_24h_raw = float(volume.get("h24") or 0)
    _est_fees = _vol_24h_raw * 0.01  # ~1% standard pool fee
    _fees_str = format_large_number(_est_fees) if _est_fees > 0 else "$0"
    _mcap_raw = float(pair.get("marketCap") or 0)
    _fee_ratio = (_est_fees / _mcap_raw * 100) if _mcap_raw > 0 else 0.0
    _fee_emoji = "\u2705" if _fee_ratio > 0.5 else ("\u26a0\ufe0f" if _fee_ratio > 0.05 else "\U0001f6a8")
    if _mcap_raw < 1000:
        _fee_emoji = "\u2796"  # neutral for micro caps

    # Fill embed fields (forms a perfect 3x2 grid)
    embed.add_field(name="\U0001f4b5 Price (USD)", value=f"**{price_formatted}**\n({price_native} {quote_token.get('symbol', '')})", inline=True)
    embed.add_field(name="\U0001f4a7 Liquidity", value=f"**{liq_usd}**", inline=True)
    embed.add_field(name="\U0001f48e Market Cap", value=f"**{mcap}**\n(FDV: {fdv})", inline=True)
    
    embed.add_field(name="\U0001f4ca Volume", value=(
        f"**5m:** {vol_m5}\n"
        f"**1h:** {vol_h1}\n"
        f"**24h:** {vol_h24}"
    ), inline=True)
    embed.add_field(name="\U0001f504 Trades (Buy/Sell)", value=(
        f"**5m:** \U0001f7e2 {tx_m5.get('buys', 0)} / \U0001f534 {tx_m5.get('sells', 0)}\n"
        f"**1h:** \U0001f7e2 {tx_h1.get('buys', 0)} / \U0001f534 {tx_h1.get('sells', 0)}\n"
        f"**24h:** \U0001f7e2 {tx_h24.get('buys', 0)} / \U0001f534 {tx_h24.get('sells', 0)}"
    ), inline=True)
    embed.add_field(name="\U0001f4c8 Price Changes", value=(
        f"**5m:** {change_5m}\n"
        f"**1h:** {change_1h}\n"
        f"**24h:** {change_24h}"
    ), inline=True)

    # Fees Paid field
    embed.add_field(name="\U0001f4b8 Est. 24h Fees Paid", value=(
        f"{_fee_emoji} **{_fees_str}**\n"
        f"Fee/MC: `{_fee_ratio:.2f}%`"
    ), inline=True)
    
    # --- DEX Paid Status ---
    _dex_has_paid = dex_paid_info.get("has_paid", False)
    _dex_boost_active = dex_paid_info.get("boost_active", 0)
    _dex_order_types = dex_paid_info.get("order_types", [])
    
    if _dex_has_paid:
        _paid_icon = "\u2705"  # Green checkmark
        _paid_label = "**PAID**"
        _paid_details = []
        _type_labels = {
            "tokenProfile": "Profile",
            "communityTakeover": "CTO",
            "tokenAd": "Ad",
            "trendingBarAd": "Trending Ad"
        }
        for ot in _dex_order_types:
            _paid_details.append(_type_labels.get(ot, ot))
        _details_str = ", ".join(_paid_details) if _paid_details else ""
        if _dex_boost_active > 0:
            _details_str += f"\n\u26a1 `{_dex_boost_active}` active boosts"
        _dex_value = f"{_paid_icon} {_paid_label}\n{_details_str}" if _details_str else f"{_paid_icon} {_paid_label}"
    else:
        if _dex_boost_active > 0:
            _dex_value = f"\u26a1 Not paid but `{_dex_boost_active}` boosts active"
        else:
            _dex_value = "\u274c **NOT PAID**"
    
    embed.add_field(name="\U0001f4b0 DEX Paid", value=_dex_value, inline=True)
    
    # --- Fresh Wallets ---
    _fresh_count = fresh_wallets_info.get("fresh_count", 0)
    _fresh_total = fresh_wallets_info.get("total_sampled", 0)
    _fresh_pct = fresh_wallets_info.get("fresh_pct", 0.0)
    
    if _fresh_total > 0:
        if _fresh_pct > 50:
            _fresh_emoji = "\U0001f6a8"  # Red siren — majority fresh wallets
        elif _fresh_pct > 25:
            _fresh_emoji = "\u26a0\ufe0f"  # Warning
        else:
            _fresh_emoji = "\u2705"  # Green — healthy
        _fresh_value = f"{_fresh_emoji} **{_fresh_count}/{_fresh_total}** (`{_fresh_pct:.0f}%`)\nwallets with < 0.1 SOL"
    else:
        _fresh_value = "\u2796 N/A"
    
    embed.add_field(name="\U0001f195 Fresh Wallets", value=_fresh_value, inline=True)
    
    # Alert banners (before security section)
    for _alert in alerts:
        embed.add_field(name="\U0001f534 ALERT", value=_alert, inline=False)
    
    embed.add_field(name=f"\U0001f6e1\ufe0f Security Check ({sec['status']})", value=sec['description'], inline=False)

    # --- Distribution Analysis (Solana / RugCheck only) ---
    if chain_id == "solana" and rug_report and dist:
        _dist_lines = []
        _bund = dist.get("bundler_pct")
        _snip = dist.get("sniper_pct")
        _ins_pct = dist.get("insider_pct", 0.0)
        _ins_cnt = dist.get("insider_count", 0)
        _ins_nets = dist.get("insider_networks", 0)

        if _bund:
            try:
                _bund_val = float(str(_bund).split("%")[0].strip())
            except Exception:
                _bund_val = 0.0
            _bund_wallets = dist.get("bundler_wallets", 0)
            _bund_clusters = dist.get("bundler_clusters", 0)
            _bund_emoji = "\U0001f6a8" if _bund_val > 15.0 else "\u26a0\ufe0f"
            _cluster_info = f" across `{_bund_wallets}` wallets (`{_bund_clusters}` clusters)" if _bund_wallets > 0 else ""
            _dist_lines.append(f"\u2022 **Bundler:** {_bund_emoji} `{_bund}`{_cluster_info}")
        else:
            _dist_lines.append("\u2022 **Bundler:** \u2705 None detected")

        if _snip:
            _dist_lines.append(f"\u2022 **Sniper/Bot:** \U0001f3af `{_snip}` held by snipers")
        else:
            _dist_lines.append("\u2022 **Sniper/Bot:** \u2705 None detected")

        if _ins_cnt > 0:
            _ins_emoji = "\U0001f6a8" if _ins_pct > 15.0 else "\u26a0\ufe0f"
            _dist_lines.append(f"\u2022 **Insider:** {_ins_emoji} `{_ins_pct:.1f}%` across `{_ins_cnt}` linked wallets")
        else:
            _dist_lines.append("\u2022 **Insider:** \u2705 No insider clusters detected")

        embed.add_field(name="\U0001f4ca Distribution Analysis", value="\n".join(_dist_lines), inline=False)

    # Developer Info (Solana only)
    if chain_id == "solana" and creator_address:
        _dev_lines = []
        _short = f"`{creator_address[:6]}...{creator_address[-4:]}`"
        _sol_str = f"**{dev_sol_balance:.2f} SOL**" if dev_sol_balance > 0 else "N/A"
        _dev_lines.append(f"\u2022 **Wallet:** {_short} | **Balance:** {_sol_str}")
        
        _total = dev_info.get("total_coins", 0)
        _migrated = dev_info.get("migrated_coins", 0)
        if _total > 0:
            _dev_lines.append(f"\u2022 **Coins Made:** \U0001f451 `{_migrated}/{_total}` graduated to Raydium")
        else:
            _dev_lines.append("\u2022 **Coins Made:** N/A (no pump.fun history)")
        
        _tw = dev_info.get("twitter")
        if _tw:
            _dev_lines.append(f"\u2022 **Dev X:** [View Profile]({_tw})")
        else:
            _dev_lines.append("\u2022 **Dev X:** Not found")
        
        embed.add_field(name="\U0001f464 Developer Info", value="\n".join(_dev_lines), inline=False)
    
    ca_val = base_token.get('address', '')
    bm_url = get_bubblemaps_url(chain_id, ca_val)
    ca_field_value = f"`{ca_val}`"
    if bm_url:
        ca_field_value += f"\n\n[📊 View Bubblemap]({bm_url})"
    embed.add_field(name="\U0001f4dd Contract Address", value=ca_field_value, inline=False)
    
    # Live timestamp so user knows exactly how fresh the data is
    _now_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S UTC")
    embed.set_footer(text=f"Network: {chain_id.upper()} \u2022 DexScreener & RugCheck \u2022 Fetched at {_now_utc} \u2014 hit \U0001f504 Refresh Stats if MC looks stale")
    
    return embed


def create_trending_embed(pools: List[Dict[str, Any]], chain_name: str) -> discord.Embed:
    """Creates a beautiful list Embed of trending tokens."""
    chain_cfg = get_chain_config(chain_name)
    embed = discord.Embed(
        title=f"🔥 Top Trending Pools: {chain_cfg['name']}",
        description=f"Displaying the hottest token pools on **{chain_cfg['name']}** {chain_cfg['icon']} right now.",
        color=chain_cfg["color"]
    )
    
    if not pools:
        embed.description = "No trending pools found at the moment."
        return embed
        
    for idx, pool in enumerate(pools[:10]):
        name = pool.get("pool_name", "Unknown Pool")
        symbol = pool.get("base_token_symbol", "N/A")
        price = format_price(pool.get("price_usd"))
        volume = format_large_number(pool.get("volume_24h"))
        liq = format_large_number(pool.get("liquidity_usd"))
        
        price_change = pool.get("price_change", {})
        h24_change = format_percentage(price_change.get("h24"))
        
        ca = pool.get("base_token_address", "N/A")
        network = pool.get("network_id", "unknown")
        
        # Display index and detail line
        field_name = f"{idx+1}. {name} ({symbol}) on {network.upper()}"
        field_value = (
            f"**Price:** {price} | **24h Vol:** {volume} | **Liq:** {liq}\n"
            f"**24h Change:** {h24_change}\n"
            f"**CA:** `{ca}`"
        )
        embed.add_field(name=field_name, value=field_value, inline=False)
        
    embed.set_footer(text="Data provided by GeckoTerminal • Use /ca <address> to view full token info.")
    return embed

def create_tracker_alert_embed(alert_data: Dict[str, Any], token_name: str) -> discord.Embed:
    ticker = alert_data["ticker"].upper()
    sol_spent = alert_data["sol_spent"]
    market_cap_str = alert_data["market_cap"]
    mint = alert_data["mint"]
    buys = alert_data["buys"]
    
    # Format the buyers list
    buyers_text = []
    for buy in buys:
        b_emoji = buy["emoji"]
        b_name = buy["name"]
        b_addr = buy["wallet_address"]
        b_sol = buy["sol_spent"]
        buyers_text.append(f"{b_emoji} **{b_name}** ({b_addr[:6]}...{b_addr[-4:]}) bought **{b_sol:.2f} SOL**")
    
    buyers_list_str = "\n".join(buyers_text)
    
    main_buy = buys[0]
    main_name = main_buy["name"]
    main_emoji = main_buy["emoji"]
    
    title = f"{main_emoji} {main_name} bought ${ticker}"
    if len(buys) > 1:
        title = f"🔔 Multiple Tracked Wallets bought ${ticker}"
        
    embed = discord.Embed(
        title=title,
        color=0x00FFA3
    )
    
    embed.add_field(name="Transaction Details", value=buyers_list_str, inline=False)
    
    if len(buys) == 1:
        embed.add_field(name="Wallet Address", value=f"{buys[0]['wallet_address']}", inline=False)
    else:
        for idx, buy in enumerate(buys, 1):
            embed.add_field(name=f"Wallet {idx} Address ({buy['name']})", value=f"{buy['wallet_address']}", inline=False)
            
    embed.add_field(name="Token Name", value=token_name or "Unknown Token", inline=True)
    embed.add_field(name="Ticker", value=f"${ticker}", inline=True)
    embed.add_field(name="Market Cap", value=market_cap_str, inline=True)
    embed.add_field(name="Contract Address (CA)", value=f"{mint}", inline=False)
    
    embed.set_footer(text="Wallet Tracker • Real-time alerts")
    return embed

class CheckXProfileButton(discord.ui.Button):
    """Button to check the token's Twitter/X profile due diligence."""
    def __init__(self, twitter_url: str, row: Optional[int] = None):
        super().__init__(label="Check X Profile", style=discord.ButtonStyle.secondary, emoji="🕵️", row=row)
        self.twitter_url = twitter_url
        
    async def callback(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            return
            
        try:
            embed = await _build_checkuser_embed(self.twitter_url)
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            logger.error(f"Error in CheckXProfileButton callback: {e}")
            try:
                await interaction.followup.send("An error occurred while performing the check.", ephemeral=True)
            except Exception:
                pass


# ----------------- INTERACTIVE UI COMPONENTS -----------------

class UpgradeButton(discord.ui.Button):
    def __init__(self, row: int = 2):
        super().__init__(label="💎 Upgrade", style=discord.ButtonStyle.success, row=row)

    async def callback(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="💎 Upgrade to MemecoinBot Premium",
            description=(
                "Supercharge your trading with our next-generation due diligence and tracking tools. "
                "Get access to high-speed features and lift daily query limits."
            ),
            color=0xFFD700
        )
        embed.add_field(
            name="🚀 Premium Perks",
            value=(
                "• **Unlimited `/ca` queries** — Run contract address security scans without daily limits.\n"
                "• **Track up to 250 custom wallets** — Add and track wallets with `/addwallet`.\n"
                "• **Add X accounts for tracking** — Monitor and track X (Twitter) accounts.\n"
                "• **Alpha Trader Calls** — Get real-time calls from alpha traders across FOMO and Pump.\n"
                "• **Advanced Coin Analysis** — Deep security scans, RugCheck, and X-account checks."
            ),
            inline=False
        )
        embed.add_field(
            name="💰 Pricing Options",
            value="**$49 USD** / month OR **$300 USD** / year (Save 50%!)",
            inline=False
        )
        embed.set_footer(text="Click a plan below to proceed with Solana payment.")
        
        await interaction.response.send_message(
            embed=embed, 
            view=UpgradeView(SOLANA_PAYMENT_ADDRESS, show_monthly=True), 
            ephemeral=True
        )


class TokenInfoView(discord.ui.View):
    """Interactive buttons for a single token info embed."""
    def __init__(self, pair: Dict[str, Any], user_id: Optional[int] = None):
        super().__init__(timeout=300)
        self.pair = pair
        self.add_action_buttons()
        # Add dynamic Refresh Stats button
        self.add_item(RefreshButton(self.pair))
        
        # Add Tracked / Bundler Check button for Solana tokens
        chain_id = self.pair.get("chainId", "")
        if chain_id == "solana":
            self.add_item(TrackedHoldersButton(self.pair))
            
        # Add Check X Profile button if Twitter URL is available
        twitter_url = None
        info = self.pair.get("info", {})
        if info:
            for social in info.get("socials", []):
                if social.get("type", "").lower() == "twitter":
                    twitter_url = social.get("url")
                    break
        if twitter_url:
            self.add_item(CheckXProfileButton(twitter_url, row=2 if chain_id == "solana" else 1))
            
        # Add Upgrade Button if user is not premium
        if user_id and not premium_db.is_premium(str(user_id)):
            self.add_item(UpgradeButton(row=2 if chain_id == "solana" else 1))
        
    def add_action_buttons(self):
        chain_id = self.pair.get("chainId", "")
        base_token = self.pair.get("baseToken", {})
        ca_address = base_token.get("address", "")
        pair_address = self.pair.get("pairAddress", "")
        
        if chain_id == "solana":
            # Add premium Solana terminal buttons instead of DexScreener/Jupiter
            self.add_item(discord.ui.Button(label="Axiom", url=f"https://axiom.trade/meme/{pair_address}?chain=sol&pulseChains=sol&trackerChains=sol,robinhood,bnb,eth", style=discord.ButtonStyle.link, emoji="🎯"))
            self.add_item(discord.ui.Button(label="Padre", url=f"https://trade.padre.gg/token/{ca_address}", style=discord.ButtonStyle.link, emoji="🦅"))
            self.add_item(discord.ui.Button(label="GMGN", url=f"https://gmgn.ai/sol/token/{ca_address}", style=discord.ButtonStyle.link, emoji="🐸"))
            self.add_item(discord.ui.Button(label="Pump.fun", url=f"https://pump.fun/coin/{ca_address}", style=discord.ButtonStyle.link, emoji="💊"))
        else:
            # DexScreener Link Button
            pair_url = self.pair.get("url")
            if pair_url:
                self.add_item(discord.ui.Button(label="View on DexScreener", url=pair_url, style=discord.ButtonStyle.link, emoji="📊"))
                
            # Quick Buy Button
            chain_cfg = get_chain_config(chain_id)
            buy_url = chain_cfg.get("buy_url")
            if buy_url and ca_address:
                full_buy_url = f"{buy_url}{ca_address}"
                self.add_item(discord.ui.Button(label=f"Buy on {chain_cfg['name']}", url=full_buy_url, style=discord.ButtonStyle.link, emoji="💳"))
            
        # Add Socials/Websites dynamically if available in info
        info = self.pair.get("info", {})
        if info:
            # Add Website
            websites = info.get("websites", [])
            if websites and len(websites) > 0:
                self.add_item(discord.ui.Button(label="Website", url=websites[0].get("url"), style=discord.ButtonStyle.link, emoji="🌐", row=1 if chain_id == "solana" else None))
                
            # Add Socials (Twitter, Telegram)
            socials = info.get("socials", [])
            for social in socials[:2]:  # Limit to 2 socials to avoid clutter
                soc_type = social.get("type", "").lower()
                soc_url = social.get("url")
                if soc_url:
                    emoji = "🐦" if soc_type == "twitter" else ("💬" if soc_type == "telegram" else "🔗")
                    label = soc_type.capitalize()
                    self.add_item(discord.ui.Button(label=label, url=soc_url, style=discord.ButtonStyle.link, emoji=emoji, row=1 if chain_id == "solana" else None))

        # Add Image Search button (uses Google Lens for reverse image search)
        image_url = self.pair.get("resolved_image_url")
        if image_url and image_url != "https://i.imgur.com/f94QG4v.png":
            self.add_item(discord.ui.Button(
                label="Image Search",
                url=f"https://lens.google.com/uploadbyurl?url={image_url}",
                style=discord.ButtonStyle.link,
                emoji="🔍",
                row=1 if chain_id == "solana" else None
            ))


class RefreshButton(discord.ui.Button):
    """Refreshes the stats and security checks on demand."""
    def __init__(self, pair: Dict[str, Any]):
        chain_id = pair.get("chainId", "")
        # Place on row 1 for Solana to avoid crowding the 5 terminal buttons on row 0
        super().__init__(label="Refresh Stats", style=discord.ButtonStyle.secondary, emoji="🔄", row=1 if chain_id == "solana" else 0)
        self.pair = pair
        
    async def callback(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer()
        except discord.NotFound:
            logger.warning("Refresh button interaction expired before defer completed.")
            return
        except Exception as e:
            logger.error(f"Error deferring refresh: {e}")
            return
            
        base_token = self.pair.get("baseToken", {})
        ca_address = base_token.get("address", "")
        if not ca_address:
            try:
                await interaction.followup.send("Failed to refresh: Contract address not found.", ephemeral=True)
            except Exception:
                pass
            return
            
        try:
            pairs = await api_client.get_token_by_ca(ca_address)
            if not pairs:
                await interaction.followup.send("Failed to refresh: Trading pair not found on DexScreener.", ephemeral=True)
                return
                
            primary_pair = pairs[0]
            
            # Fetch fresh RugCheck report if Solana
            rug_report = None
            if primary_pair.get("chainId") == "solana":
                try:
                    rug_report = await api_client.get_rugcheck_report(ca_address)
                except Exception as e:
                    logger.error(f"Error fetching rug report on refresh: {e}")
                    
            # Generate fresh embed and view
            embed = await create_token_embed(primary_pair, rug_report)
            view = TokenInfoView(primary_pair, user_id=interaction.user.id)
            
            await interaction.message.edit(embed=embed, view=view)
        except discord.NotFound:
            logger.warning("Refresh interaction expired during message update.")
        except Exception as e:
            logger.error(f"Error updating message in refresh callback: {e}")


class TrackedHoldersButton(discord.ui.Button):
    """Checks for tracked holders and multi-walling / bundlers."""
    def __init__(self, pair: Dict[str, Any]):
        chain_id = pair.get("chainId", "")
        # Place on row 1 for Solana to avoid crowding the 5 terminal buttons on row 0
        super().__init__(label="Tracked / Bundler Check", style=discord.ButtonStyle.secondary, emoji="🗺️", row=1 if chain_id == "solana" else 0)
        self.pair = pair

    async def callback(self, interaction: discord.Interaction):
        base_token = self.pair.get("baseToken", {})
        mint = base_token.get("address", "")
        ticker = base_token.get("symbol", "Unknown")
        await process_tracked_holders_check(interaction, mint, ticker)


async def process_tracked_holders_check(interaction: discord.Interaction, mint: str, ticker: str):
    """Checks if any tracked wallets (User or KOL) hold the token, and lists bundlers."""
    uid = str(interaction.user.id)
    if not premium_db.is_premium(uid):
        try:
            await interaction.response.send_message(
                "❌ This feature (Tracked / Bundler Check) is only available for paid premium users. Upgrade to premium to run token scans!", 
                view=UpgradeView(SOLANA_PAYMENT_ADDRESS),
                ephemeral=True
            )
        except Exception as e:
            logger.error(f"Error sending premium notice in process_tracked_holders_check: {e}")
        return

    try:
        await interaction.response.defer(ephemeral=True)
    except Exception:
        return
        
    api_key = os.getenv("SOLANA_TRACKER_API_KEY")
    gmgn_api_key = os.getenv("GMGN_API_KEY")
    
    # Fetch RugCheck report (always)
    try:
        rug_report = await api_client.get_rugcheck_report(mint)
    except Exception as e:
        logger.error(f"Error fetching RugCheck report in tracked check: {e}")
        rug_report = None
        
    # Fetch from Solana Tracker if key is configured
    st_bundlers = None
    st_holders = None
    if api_key:
        try:
            st_bundlers = await api_client.get_solanatracker_bundlers(mint, api_key)
            st_holders = await api_client.get_solanatracker_holders(mint, api_key)
        except Exception as e:
            logger.error(f"Error calling Solana Tracker API: {e}")

    # Fetch from GMGN OpenAPI if key is configured
    gmgn_security = None
    gmgn_info = None
    gmgn_holders = None
    if gmgn_api_key:
        try:
            gmgn_res = await asyncio.gather(
                api_client.get_gmgn_token_security("solana", mint),
                api_client.get_gmgn_token_info("solana", mint),
                api_client.get_gmgn_token_holders("solana", mint, limit=100),
                return_exceptions=True
            )
            if not isinstance(gmgn_res[0], Exception):
                gmgn_security = gmgn_res[0]
            if not isinstance(gmgn_res[1], Exception):
                gmgn_info = gmgn_res[1]
            if not isinstance(gmgn_res[2], Exception) and gmgn_res[2]:
                gmgn_holders = gmgn_res[2].get("list") if isinstance(gmgn_res[2], dict) else gmgn_res[2]
        except Exception as e:
            logger.error(f"Error calling GMGN API in tracked check: {e}")
            
    # Rebuild all tracked wallets
    tracked_wallets = rebuild_all_tracked_wallets()
    if not tracked_wallets:
        tracked_wallets = {}
    
    # Search for tracked wallets in holders
    found_tracked = []
    seen_addresses = set()
    
    def check_address(address, pct, amount_str=""):
        if not address:
            return
        address = address.strip()
        if address in seen_addresses:
            return
        seen_addresses.add(address)
        
        if address in tracked_wallets:
            for w in tracked_wallets[address]:
                found_tracked.append({
                    "address": address,
                    "name": w.get("name"),
                    "emoji": w.get("emoji", "👤"),
                    "type": w.get("type"),
                    "pct": pct,
                    "amount": amount_str
                })
                
    # Search GMGN holders
    if gmgn_holders:
        for h in gmgn_holders:
            addr = h.get("address") or h.get("wallet")
            pct = (h.get("amount_percentage") or 0.0) * 100.0
            amount_str = f"{h.get('balance', 0):,}"
            check_address(addr, pct, amount_str)

    # Search RugCheck holders
    if rug_report:
        top_holders = rug_report.get("topHolders", []) or []
        for h in top_holders:
            owner = h.get("owner")
            pct = h.get("pct", 0.0)
            ui_amount = h.get("uiAmountString") or f"{h.get('uiAmount', 0):,}"
            check_address(owner, pct, ui_amount)
            check_address(h.get("address"), pct, ui_amount)
            
    # Search Solana Tracker holders
    if st_holders:
        holders_list = []
        if isinstance(st_holders, dict):
            holders_list = st_holders.get("holders", []) or []
        elif isinstance(st_holders, list):
            holders_list = st_holders
            
        for h in holders_list:
            addr = h.get("address") or h.get("wallet")
            pct = h.get("percentage") or h.get("pct") or 0.0
            amount_str = h.get("amount") or h.get("uiAmount") or ""
            check_address(addr, pct, str(amount_str))
            
    tracked_total_pct = sum(t["pct"] for t in found_tracked if isinstance(t["pct"], (int, float)))
    http_url, _ = get_solana_rpc_urls()
    insider_report = await insider_analyzer.build_insider_report(
        mint=mint,
        rpc_url=http_url,
        rug_report=rug_report,
        st_bundlers=st_bundlers,
        st_holders=st_holders,
        gmgn_security=gmgn_security,
        gmgn_info=gmgn_info,
        gmgn_holders=gmgn_holders,
        tracked_pct=tracked_total_pct,
    )
    insider_lines = insider_analyzer.format_report_lines(insider_report)
    cluster_lines = insider_analyzer.format_cluster_lines(insider_report)
                
    # Build embed
    bm_url = f"https://v2.bubblemaps.io/token/solana/{mint}"
    embed = discord.Embed(
        title=f"🗺️ Bundler & Tracked Holders: ${ticker.upper()}",
        description=f"**Contract Address:** `{mint}`\n\n[📊 View Bubblemap]({bm_url})",
        color=0x9945FF
    )
    
    if found_tracked:
        tracked_text = []
        for t in found_tracked:
            t_type = "KOL" if t["type"] == "kol" else "Custom User"
            t_short = f"`{t['address'][:6]}...{t['address'][-6:]}`"
            pct_val = t["pct"]
            pct_str = f"`{pct_val:.2f}%`" if isinstance(pct_val, (int, float)) else f"`{pct_val}`"
            tracked_text.append(f"{t['emoji']} **{t['name']}** ({t_type}) | {t_short} holds {pct_str} of supply")
        embed.add_field(name="🚨 Tracked Holders Detected", value="\n".join(tracked_text), inline=False)
    else:
        embed.add_field(name="👤 Tracked Holders", value="✅ None of your tracked wallets are holding this token.", inline=False)
        
    embed.add_field(name="🧠 Insider / Sniper / Bundler Distribution", value="\n".join(insider_lines), inline=False)
    embed.add_field(name="📦 Cluster Distribution", value="\n\n".join(cluster_lines)[:1024], inline=False)
    
    sources_list = []
    if gmgn_api_key:
        sources_list.append("GMGN.ai")
    if api_key:
        sources_list.append("Solana Tracker")
    sources_list.extend(["RugCheck", "Solana RPC"])
    embed.set_footer(text=f"Sources: {', '.join(sources_list)} • Insider threshold: 10% supply")
        
    await interaction.followup.send(embed=embed, ephemeral=True)


class UserAlertView(discord.ui.View):
    """Interactive buttons attached to personal custom wallet tracker alerts."""
    def __init__(self, mint: str, ticker: str, pair_address: Optional[str] = None):
        super().__init__(timeout=None)
        self.mint = mint
        self.ticker = ticker
        self.pair_address = pair_address or mint
        
        # Action/Trade links
        self.add_item(discord.ui.Button(label="Pump.fun", url=f"https://pump.fun/coin/{mint}", style=discord.ButtonStyle.link, emoji="💊"))
        self.add_item(discord.ui.Button(label="Padre", url=f"https://trade.padre.gg/token/{mint}", style=discord.ButtonStyle.link, emoji="🦅"))
        self.add_item(discord.ui.Button(label="Axiom", url=f"https://axiom.trade/meme/{self.pair_address}?chain=sol&pulseChains=sol&trackerChains=sol,robinhood,bnb,eth", style=discord.ButtonStyle.link, emoji="🎯"))

    @discord.ui.button(label="Token Stats", style=discord.ButtonStyle.primary, emoji="📊")
    async def btn_token_stats(self, interaction: discord.Interaction, button: discord.ui.Button):
        import asyncio
        uid = str(interaction.user.id)
        allowed, count = await asyncio.get_event_loop().run_in_executor(
            None, premium_db.check_and_increment_usage, uid, "token_stats", 10
        )
        if not allowed:
            embed = discord.Embed(
                title="🔒 Daily Limit Reached",
                description=(
                    "Non-paid users can only click **Token Stats** **10 times per day**.\n"
                    "You've used all 10 clicks today.\n\n"
                    "Upgrade to **Premium** for just **$49/month** for unlimited access!"
                ),
                color=0xFF3B30
            )
            await interaction.response.send_message(embed=embed, view=UpgradeView(SOLANA_PAYMENT_ADDRESS, show_monthly=True), ephemeral=True)
            return

        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            return
            
        pairs = await api_client.get_token_by_ca(self.mint)
        if not pairs:
            await interaction.followup.send(f"❌ Could not fetch stats for `{self.mint}` on DexScreener.", ephemeral=True)
            return
            
        primary_pair = pairs[0]
        embed = await create_token_embed(primary_pair)
        view = TokenInfoView(primary_pair, user_id=interaction.user.id)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(label="Token Analyser", style=discord.ButtonStyle.success, emoji="🔍")
    async def btn_token_analyser(self, interaction: discord.Interaction, button: discord.ui.Button):
        import asyncio
        uid = str(interaction.user.id)
        allowed, count = await asyncio.get_event_loop().run_in_executor(
            None, premium_db.check_and_increment_usage, uid, "token_analyser", 10
        )
        if not allowed:
            embed = discord.Embed(
                title="🔒 Daily Limit Reached",
                description=(
                    "Non-paid users can only click **Token Analyser** **10 times per day**.\n"
                    "You've used all 10 clicks today.\n\n"
                    "Upgrade to **Premium** for just **$49/month** for unlimited access!"
                ),
                color=0xFF3B30
            )
            await interaction.response.send_message(embed=embed, view=UpgradeView(SOLANA_PAYMENT_ADDRESS, show_monthly=True), ephemeral=True)
            return

        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            return
            
        gmgn_api_key = os.getenv("GMGN_API_KEY")
        
        # Parallel fetch for GMGN and Rugcheck / Pairs
        async def _fetch_rugcheck():
            try:
                return await api_client.get_rugcheck_report(self.mint)
            except Exception:
                return None
                
        async def _fetch_pairs():
            try:
                return await api_client.get_token_by_ca(self.mint)
            except Exception:
                return None
                
        async def _fetch_gmgn_security():
            if gmgn_api_key:
                return await api_client.get_gmgn_token_security("solana", self.mint)
            return None
            
        async def _fetch_gmgn_info():
            if gmgn_api_key:
                return await api_client.get_gmgn_token_info("solana", self.mint)
            return None
            
        async def _fetch_gmgn_holders():
            if gmgn_api_key:
                return await api_client.get_gmgn_token_holders("solana", self.mint, limit=100)
            return None
            
        results = await asyncio.gather(
            _fetch_rugcheck(),
            _fetch_pairs(),
            _fetch_gmgn_security(),
            _fetch_gmgn_info(),
            _fetch_gmgn_holders(),
            return_exceptions=True
        )
        
        rug_report = results[0] if not isinstance(results[0], Exception) else None
        pairs = results[1] if not isinstance(results[1], Exception) else None
        gmgn_security = results[2] if not isinstance(results[2], Exception) else None
        gmgn_info = results[3] if not isinstance(results[3], Exception) else None
        gmgn_holders = None
        if not isinstance(results[4], Exception) and results[4]:
            gmgn_holders = results[4].get("list") if isinstance(results[4], dict) else results[4]
            
        embed = discord.Embed(
            title=f"🔍 High-Quality Token Analysis: ${self.ticker.upper()}",
            description=f"**Contract Address:** `{self.mint}`",
            color=0x00FFA3
        )
        
        if pairs:
            p = pairs[0]
            mcap = p.get("marketCap")
            liq = p.get("liquidity", {}).get("usd")
            vol24 = p.get("volume", {}).get("h24")
            embed.add_field(name="💰 Market Cap", value=f"${mcap:,.0f}" if mcap else "N/A", inline=True)
            embed.add_field(name="💧 Liquidity", value=f"${liq:,.0f}" if liq else "N/A", inline=True)
            embed.add_field(name="📈 24h Volume", value=f"${vol24:,.0f}" if vol24 else "N/A", inline=True)
            
        # Rebuild tracked wallets to cross-reference
        tracked_wallets = rebuild_all_tracked_wallets() or {}
        found_tracked = []
        seen_addresses = set()
        
        # Search GMGN holders
        if gmgn_holders:
            for h in gmgn_holders:
                addr = h.get("address") or h.get("wallet")
                if addr and addr not in seen_addresses:
                    seen_addresses.add(addr)
                    if addr in tracked_wallets:
                        for w in tracked_wallets[addr]:
                            pct = (h.get("amount_percentage") or 0.0) * 100.0
                            found_tracked.append(f"{w.get('emoji', '👤')} **{w.get('name')}** ({w.get('type').upper()}) holds `{pct:.2f}%` of supply (GMGN)")
                            
        # Search RugCheck holders
        if rug_report:
            top_holders = rug_report.get("topHolders", []) or []
            for h in top_holders:
                owner = h.get("owner")
                if owner and owner not in seen_addresses:
                    seen_addresses.add(owner)
                    if owner in tracked_wallets:
                        for w in tracked_wallets[owner]:
                            pct = h.get("pct", 0.0)
                            found_tracked.append(f"{w.get('emoji', '👤')} **{w.get('name')}** ({w.get('type').upper()}) holds `{pct:.2f}%` of supply (RugCheck)")
            
        score = rug_report.get("score", 0) if rug_report else 0
        risks = rug_report.get("risks", []) if rug_report else []
        mint_auth = rug_report.get("token", {}).get("mintAuthority") if rug_report else None
        freeze_auth = rug_report.get("token", {}).get("freezeAuthority") if rug_report else None
        
        if gmgn_security:
            if gmgn_security.get("is_honeypot") is True or gmgn_security.get("is_honeypot") == 1:
                risks.append({"name": "Honeypot Danger", "description": "GMGN detects sells are disabled"})
            if gmgn_security.get("cannot_mint") is False or gmgn_security.get("cannot_mint") == 0:
                mint_auth = "Active (GMGN)"
            if gmgn_security.get("cannot_freeze") is False or gmgn_security.get("cannot_freeze") == 0:
                freeze_auth = "Active (GMGN)"
                
        risk_status = "🟢 Low Risk (Good)" if score < 1000 else ("🟡 Medium Risk" if score < 3000 else "🔴 High Risk / Suspicious")
        if gmgn_security and (gmgn_security.get("is_honeypot") is True or gmgn_security.get("is_honeypot") == 1):
            risk_status = "🔴 Honeypot Alert"
            
        embed.add_field(name="🛡️ Security Score", value=f"{risk_status} ({score} pts)" if rug_report else risk_status, inline=False)
        
        mint_str = "❌ Disabled (Safe)" if not mint_auth else (f"⚠️ Active (`{mint_auth[:4]}...{mint_auth[-4:]}`)" if len(str(mint_auth)) > 10 else f"⚠️ {mint_auth}")
        freeze_str = "❌ Disabled (Safe)" if not freeze_auth else (f"⚠️ Active (`{freeze_auth[:4]}...{freeze_auth[-4:]}`)" if len(str(freeze_auth)) > 10 else f"⚠️ {freeze_auth}")
        embed.add_field(name="🔒 Mint Authority", value=mint_str, inline=True)
        embed.add_field(name="❄️ Freeze Authority", value=freeze_str, inline=True)
        
        http_url, _ = get_solana_rpc_urls()
        insider_report = await insider_analyzer.build_insider_report(
            mint=self.mint,
            rpc_url=http_url,
            rug_report=rug_report,
            gmgn_security=gmgn_security,
            gmgn_info=gmgn_info,
            gmgn_holders=gmgn_holders,
        )
        embed.add_field(
            name="🧠 Insider / Sniper / Bundler Distribution",
            value="\n".join(insider_analyzer.format_report_lines(insider_report)),
            inline=False
        )
        embed.add_field(
            name="📦 Cluster Distribution",
            value="\n\n".join(insider_analyzer.format_cluster_lines(insider_report))[:1024],
            inline=False
        )
        
        if risks:
            risk_list = "\n".join([f"• {r.get('name')}: {r.get('description', '')}" for r in risks[:4]])
            embed.add_field(name="⚠️ Detected Risk Flags", value=risk_list, inline=False)
        elif not rug_report and not gmgn_security:
            embed.add_field(name="🛡️ Security Analysis", value="⚠️ Security details unavailable.", inline=False)
            
        # Add Tracked Holders field
        if found_tracked:
            embed.add_field(name="🚨 Tracked Holders Detected", value="\n".join(found_tracked), inline=False)
        else:
            embed.add_field(name="👤 Tracked Holders", value="✅ None of your tracked wallets are holding this token.", inline=False)
            
        sources = ["Padre"]
        if gmgn_api_key:
            sources.append("GMGN.ai")
        if rug_report:
            sources.append("RugCheck")
        embed.set_footer(text=f"Powered by {', '.join(sources)} • Solana Token Inspector")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="Tracked / Bundler Check", style=discord.ButtonStyle.secondary, emoji="🗺️")
    async def btn_tracked_bundlers(self, interaction: discord.Interaction, button: discord.ui.Button):
        await process_tracked_holders_check(interaction, self.mint, self.ticker)


class TrendingDropdown(discord.ui.Select):
    """Select dropdown for filtering trending pools by blockchain network."""
    def __init__(self, current_chain: str):
        options = [
            discord.SelectOption(label="Global Trending", value="global", description="Across all networks", emoji="🌐"),
            discord.SelectOption(label="Solana", value="solana", description="Solana Memecoins", emoji="🟣"),
            discord.SelectOption(label="Base", value="base", description="Coinbase L2 Memecoins", emoji="🔵"),
            discord.SelectOption(label="BNB Chain", value="bsc", description="Binance Smart Chain Memecoins", emoji="🟡"),
            discord.SelectOption(label="Robinhood", value="robinhood", description="Robinhood Chain Pools", emoji="🟢"),
            discord.SelectOption(label="Ethereum", value="ethereum", description="Ethereum Pools", emoji="⚫"),
            discord.SelectOption(label="Arbitrum", value="arbitrum", description="Arbitrum Pools", emoji="🔵"),
            discord.SelectOption(label="Optimism", value="optimism", description="Optimism Pools", emoji="🔴"),
            discord.SelectOption(label="Avalanche", value="avalanche", description="Avalanche Pools", emoji="🔺"),
        ]
        
        # Pre-select current
        for option in options:
            if option.value == current_chain:
                option.default = True
                break
                
        super().__init__(placeholder="Select blockchain network...", min_values=1, max_values=1, options=options)
        
    async def callback(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer()
        except discord.NotFound:
            logger.warning("Dropdown interaction expired before defer completed.")
            return
        except Exception as e:
            logger.error(f"Error deferring dropdown: {e}")
            return
            
        selected_chain = self.values[0]
        
        # Fetch trending pools
        network_arg = None if selected_chain == "global" else selected_chain
        try:
            pools = await api_client.get_trending_pools(network=network_arg)
            if pools is None:
                await interaction.followup.send("Failed to retrieve trending pools. The API may be rate-limited.", ephemeral=True)
                return
                
            embed = create_trending_embed(pools, selected_chain)
            
            # Recreate view with updated button states
            view = TrendingView(current_chain=selected_chain)
            await interaction.message.edit(embed=embed, view=view)
        except discord.NotFound:
            logger.warning("Dropdown interaction expired during update.")
        except Exception as e:
            logger.error(f"Error handling dropdown selection: {e}")


class TrendingButton(discord.ui.Button):
    """Button for quick-switching trending pools."""
    def __init__(self, label: str, value: str, emoji: str, style: discord.ButtonStyle):
        super().__init__(label=label, style=style, emoji=emoji)
        self.value = value
        
    async def callback(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer()
        except discord.NotFound:
            logger.warning("Trending button interaction expired before defer completed.")
            return
        except Exception as e:
            logger.error(f"Error deferring trending button: {e}")
            return
            
        selected_chain = self.value
        
        # Fetch trending pools
        network_arg = None if selected_chain == "global" else selected_chain
        try:
            pools = await api_client.get_trending_pools(network=network_arg)
            if pools is None:
                await interaction.followup.send("Failed to retrieve trending pools. The API may be rate-limited.", ephemeral=True)
                return
                
            embed = create_trending_embed(pools, selected_chain)
            
            # Recreate view with updated button states
            view = TrendingView(current_chain=selected_chain)
            await interaction.message.edit(embed=embed, view=view)
        except discord.NotFound:
            logger.warning("Trending button interaction expired during update.")
        except Exception as e:
            logger.error(f"Error handling trending button click: {e}")


class TrendingView(discord.ui.View):
    """Interactive view showing buttons and a select menu for filtering trending pools."""
    def __init__(self, current_chain: str = "global"):
        super().__init__(timeout=180)
        self.current_chain = current_chain
        self.add_components()
        
    def add_components(self):
        # Add quick buttons for top chains
        top_chains = [
            ("Global", "global", "🌐"),
            ("Solana", "solana", "🟣"),
            ("Base", "base", "🔵"),
            ("BNB Chain", "bsc", "🟡"),
            ("Robinhood", "robinhood", "🟢")
        ]
        
        for label, val, emoji in top_chains:
            style = discord.ButtonStyle.primary if self.current_chain == val else discord.ButtonStyle.secondary
            self.add_item(TrendingButton(label=label, value=val, emoji=emoji, style=style))
            
        # Add dropdown for additional selections
        self.add_item(TrendingDropdown(current_chain=self.current_chain))

# ----------------- SLASH COMMANDS: /wallet & /addwallet -----------------

def check_tracker_channel_permission(interaction: discord.Interaction) -> bool:
    """Returns True if the interaction is from an allowed tracker channel.
    Supports comma-separated channel IDs in CUSTOM_TRACKER_CHANNEL_ID env var (main server).
    Also respects the global ALLOWED_CATEGORY_IDS restriction for new servers.
    """
    raw = (CUSTOM_TRACKER_CHANNEL_ID or "").strip()
    _raw_cat = os.getenv("ALLOWED_CATEGORY_IDS", "").strip()
    allowed_categories = set(c.strip() for c in _raw_cat.split(",") if c.strip())
    # Ensure default category IDs are always allowed
    allowed_categories.add("1531780360495435909")
    allowed_categories.add("1446685586667732992")

    # If no restrictions are configured at all, allow everywhere
    if not raw and not allowed_categories:
        return True

    curr_channel_id = str(interaction.channel_id)
    cat_id = str(getattr(interaction.channel, "category_id", "") or "")

    # 1. Match specific tracker channel IDs (main server)
    if raw:
        allowed_ids = [cid.strip() for cid in raw.split(",") if cid.strip()]
        for target_id in allowed_ids:
            if curr_channel_id == target_id:
                return True
            if hasattr(interaction.channel, "parent_id") and str(interaction.channel.parent_id) == target_id:
                return True

    # 2. Match parent category (new servers)
    if allowed_categories and cat_id in allowed_categories:
        return True

    return False

async def process_add_wallet(interaction: discord.Interaction, arg1: str, arg2: str, emoji: Optional[str] = "👤"):
    try:
        await interaction.response.defer(ephemeral=True)
    except Exception:
        pass

    uid = str(interaction.user.id)

    # Run the blocking Supabase HTTP call in a thread pool so it never
    # blocks the Discord event loop or silently times out
    loop = asyncio.get_event_loop()
    is_paid = await loop.run_in_executor(None, premium_db.is_premium, uid)

    # Premium-only gate: non-paid users cannot use /addwallet at all
    if not is_paid:
        embed = discord.Embed(
            title="🔒 Premium Feature",
            description=(
                "The `/addwallet` command is a **premium-only** feature.\n\n"
                "Upgrade to premium for just **$49/month** to track up to 250 custom wallets "
                "and receive real-time alerts in your private thread!"
            ),
            color=0xFFD700
        )
        await interaction.followup.send(embed=embed, view=UpgradeView(SOLANA_PAYMENT_ADDRESS, show_monthly=True), ephemeral=True)
        return

    if not check_tracker_channel_permission(interaction):
        raw = (CUSTOM_TRACKER_CHANNEL_ID or "").strip()
        allowed_ids = [cid.strip() for cid in raw.split(",") if cid.strip()]
        channels_str = ", ".join(f"<#{cid}>" for cid in allowed_ids) if allowed_ids else "the designated tracker channel"
        await interaction.followup.send(f"❌ Custom tracker commands can only be used in {channels_str}!", ephemeral=True)
        return

    arg1 = arg1.strip()
    arg2 = arg2.strip()
    emoji = (emoji or "👤").strip()

    # Auto-detect which parameter is the Solana wallet address vs name
    if 32 <= len(arg1) <= 44 and not (32 <= len(arg2) <= 44):
        address = arg1
        name = arg2
    elif 32 <= len(arg2) <= 44:
        name = arg1
        address = arg2
    else:
        name = arg1
        address = arg2

    if len(address) < 32 or len(address) > 44:
        await interaction.followup.send("❌ Invalid Solana wallet address! Addresses must be between 32 and 44 characters.", ephemeral=True)
        return

    if len(name) > 20:
        await interaction.followup.send("❌ Name too long! Please keep wallet names under 20 characters.", ephemeral=True)
        return

    user_data_all = load_user_wallets_data()
    uinfo = user_data_all.get(uid, {"thread_id": None, "wallets": []})
    limit = 250  # already confirmed premium above

    wallets = uinfo.get("wallets", [])
    if len(wallets) >= limit:
        await interaction.followup.send(f"❌ Limit reached! You can track a maximum of {limit} wallets.", ephemeral=True)
        return

    # Check duplicate
    for w in wallets:
        if w.get("address") == address:
            await interaction.followup.send(f"⚠️ You are already tracking this wallet as **{w.get('name')}**!", ephemeral=True)
            return

    # Resolve or create private thread
    thread_id = await get_or_create_user_thread(interaction.guild, interaction.user, uinfo)
    if not thread_id:
        await interaction.followup.send("❌ Could not create or access a tracker thread. Please make sure the bot has permissions in this server.", ephemeral=True)
        return

    wallets.append({
        "address": address,
        "name": name,
        "emoji": emoji,
        "alertsOn": True
    })
    uinfo["wallets"] = wallets
    user_data_all[uid] = uinfo
    save_user_wallets_data(user_data_all)

    rebuild_all_tracked_wallets()
    await DYNAMIC_SUBSCRIPTION_QUEUE.put(address)

    thread_mention = f"<#{thread_id}>"
    await interaction.followup.send(f"✅ Added {emoji} **{name}** (`{address[:4]}...{address[-4:]}`) to your personal tracker!\n🔔 Alerts will be sent to your private thread {thread_mention}.", ephemeral=True)

# Standalone command /addwallet panda <address> [emoji]
@bot.tree.command(name="addwallet", description="Track a custom wallet (Usage: /addwallet name address [emoji])")
@app_commands.describe(name="Nickname for this wallet (e.g. panda)", address="Solana wallet public key (base58)", emoji="Optional emoji symbol (default: 👤)")
async def addwallet_cmd(interaction: discord.Interaction, name: str, address: str, emoji: Optional[str] = "👤"):
    await process_add_wallet(interaction, name, address, emoji)

wallet_group = app_commands.Group(name="wallet", description="Personal custom wallet tracker commands")

@wallet_group.command(name="add", description="Track a Solana wallet address and receive alerts in your private thread")
@app_commands.describe(name="Custom label/nickname for this wallet", address="Solana wallet public key (base58)", emoji="Optional emoji symbol (default: 👤)")
async def wallet_add(interaction: discord.Interaction, name: str, address: str, emoji: Optional[str] = "👤"):
    await process_add_wallet(interaction, name, address, emoji)

@wallet_group.command(name="remove", description="Remove a tracked wallet by name or address")
@app_commands.describe(target="Wallet name or address to stop tracking")
async def wallet_remove(interaction: discord.Interaction, target: str):
    if not check_tracker_channel_permission(interaction):
        target_id = CUSTOM_TRACKER_CHANNEL_ID or "1530453827759898674"
        await interaction.response.send_message(f"❌ Custom tracker commands can only be used in <#{target_id}>!", ephemeral=True)
        return
        
    target = target.strip()
    uid = str(interaction.user.id)
    user_data_all = load_user_wallets_data()
    uinfo = user_data_all.get(uid)
    
    if not uinfo or not uinfo.get("wallets"):
        await interaction.response.send_message("❌ You do not have any tracked wallets.", ephemeral=True)
        return
        
    wallets = uinfo.get("wallets", [])
    filtered = [w for w in wallets if w.get("name").lower() != target.lower() and w.get("address") != target]
    
    if len(filtered) == len(wallets):
        await interaction.response.send_message(f"❌ Wallet '**{target}**' not found in your tracked wallets list.", ephemeral=True)
        return
        
    removed_wallets = [w for w in wallets if w.get("name").lower() == target.lower() or w.get("address") == target]
        
    uinfo["wallets"] = filtered
    user_data_all[uid] = uinfo
    save_user_wallets_data(user_data_all)
    
    # Also delete from Supabase if configured
    if is_supabase_configured():
        try:
            import urllib.parse
            import urllib.request
            for w in removed_wallets:
                addr = w.get("address")
                safe_uid = urllib.parse.quote(str(uid))
                safe_addr = urllib.parse.quote(str(addr))
                url = f"{SUPABASE_URL.rstrip('/')}/rest/v1/user_wallets?user_id=eq.{safe_uid}&address=eq.{safe_addr}"
                headers = {
                    "apikey": SUPABASE_KEY,
                    "Authorization": f"Bearer {SUPABASE_KEY}"
                }
                req = urllib.request.Request(url, headers=headers, method="DELETE")
                with urllib.request.urlopen(req, timeout=5) as response:
                    pass
                logger.info(f"Deleted wallet {addr} for user {uid} from Supabase")
        except Exception as e:
            logger.error(f"Error deleting wallet from Supabase: {e}")
            
    rebuild_all_tracked_wallets()
    
    await interaction.response.send_message(f"🗑️ Removed **{target}** from your personal tracker.", ephemeral=True)

@wallet_group.command(name="list", description="List all wallets currently tracked in your personal account")
async def wallet_list(interaction: discord.Interaction):
    if not check_tracker_channel_permission(interaction):
        target_id = CUSTOM_TRACKER_CHANNEL_ID or "1530453827759898674"
        await interaction.response.send_message(f"❌ Custom tracker commands can only be used in <#{target_id}>!", ephemeral=True)
        return
        
    uid = str(interaction.user.id)
    user_data_all = load_user_wallets_data()
    uinfo = user_data_all.get(uid)
    
    wallets = uinfo.get("wallets", []) if uinfo else []
    if not wallets:
        await interaction.response.send_message("ℹ️ You are not tracking any custom wallets yet! Use `/addwallet panda <address>` to add one.", ephemeral=True)
        return
        
    is_paid = premium_db.is_premium(uid)
    limit = 250 if is_paid else 0
    
    embed = discord.Embed(
        title=f"🔔 {interaction.user.display_name}'s Tracked Wallets ({len(wallets)}/{limit})",
        color=0x9945FF
    )
    thread_id = uinfo.get("thread_id")
    if thread_id:
        embed.description = f"Alert Destination: <#{thread_id}>"
        
    for w in wallets:
        e = w.get("emoji", "👤")
        n = w.get("name", "Wallet")
        a = w.get("address", "")
        short_a = f"{a[:6]}...{a[-6:]}" if len(a) > 12 else a
        embed.add_field(
            name=f"{e} {n}",
            value=f"`{short_a}`",
            inline=True
        )
        
    embed.set_footer(text="Use /wallet remove <name> to stop tracking a wallet")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@wallet_group.command(name="edit", description="Edit the name or emoji of a tracked wallet")
@app_commands.describe(name="Current name of the wallet", new_name="New nickname/label", new_emoji="New emoji symbol")
async def wallet_edit(interaction: discord.Interaction, name: str, new_name: Optional[str] = None, new_emoji: Optional[str] = None):
    if not check_tracker_channel_permission(interaction):
        target_id = CUSTOM_TRACKER_CHANNEL_ID or "1530453827759898674"
        await interaction.response.send_message(f"❌ Custom tracker commands can only be used in <#{target_id}>!", ephemeral=True)
        return
        
    if not new_name and not new_emoji:
        await interaction.response.send_message("❌ Please specify a `new_name` or `new_emoji` to update.", ephemeral=True)
        return
        
    uid = str(interaction.user.id)
    user_data_all = load_user_wallets_data()
    uinfo = user_data_all.get(uid)
    
    wallets = uinfo.get("wallets", []) if uinfo else []
    target_wallet = None
    for w in wallets:
        if w.get("name").lower() == name.lower() or w.get("address") == name:
            target_wallet = w
            break
            
    if not target_wallet:
        await interaction.response.send_message(f"❌ Wallet '**{name}**' not found.", ephemeral=True)
        return
        
    if new_name:
        target_wallet["name"] = new_name.strip()[:20]
    if new_emoji:
        target_wallet["emoji"] = new_emoji.strip()
        
    save_user_wallets_data(user_data_all)
    rebuild_all_tracked_wallets()
    
    await interaction.response.send_message(f"✏️ Updated wallet! New display: {target_wallet['emoji']} **{target_wallet['name']}**", ephemeral=True)

@wallet_group.command(name="clear", description="Remove all tracked wallets from your personal tracker")
async def wallet_clear(interaction: discord.Interaction):
    if not check_tracker_channel_permission(interaction):
        target_id = CUSTOM_TRACKER_CHANNEL_ID or "1530453827759898674"
        await interaction.response.send_message(f"❌ Custom tracker commands can only be used in <#{target_id}>!", ephemeral=True)
        return
        
    uid = str(interaction.user.id)
    user_data_all = load_user_wallets_data()
    if uid in user_data_all:
        user_data_all[uid]["wallets"] = []
        save_user_wallets_data(user_data_all)
        
        # Also clear from Supabase if configured
        if is_supabase_configured():
            try:
                import urllib.parse
                import urllib.request
                safe_uid = urllib.parse.quote(str(uid))
                url = f"{SUPABASE_URL.rstrip('/')}/rest/v1/user_wallets?user_id=eq.{safe_uid}"
                headers = {
                    "apikey": SUPABASE_KEY,
                    "Authorization": f"Bearer {SUPABASE_KEY}"
                }
                req = urllib.request.Request(url, headers=headers, method="DELETE")
                with urllib.request.urlopen(req, timeout=5) as response:
                    pass
                logger.info(f"Cleared all wallets for user {uid} from Supabase")
            except Exception as e:
                logger.error(f"Error clearing wallets from Supabase: {e}")
                
        rebuild_all_tracked_wallets()
        
    await interaction.response.send_message("🧹 Cleared all wallets from your personal tracker.", ephemeral=True)

bot.tree.add_command(wallet_group)


# ----------------- BOT COMMANDS & EVENTS -----------------

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CommandNotFound):
        try:
            await interaction.response.send_message("❌ This command has been renamed or no longer exists. Please run `!sync` (admins only) or wait for Discord to refresh your client.", ephemeral=True)
        except Exception:
            pass
        return
    logger.error(f"App command error: {error}")


async def start_telegram_mirror():
    """Background task running Telethon to mirror target Telegram channel/group messages to Discord."""
    api_id_str = os.getenv("TELEGRAM_API_ID")
    api_hash = os.getenv("TELEGRAM_API_HASH")
    phone = os.getenv("TELEGRAM_PHONE")
    discord_channel_id = os.getenv("TELEGRAM_MIRROR_CHANNEL_ID")
    source_chat = os.getenv("TELEGRAM_MIRROR_SOURCE_CHAT", "ctotrackersol")

    if not api_id_str or not api_hash or not discord_channel_id:
        logger.warning("Telegram Mirror: Missing credentials (TELEGRAM_API_ID/TELEGRAM_API_HASH) or TELEGRAM_MIRROR_CHANNEL_ID in .env. Skipping startup.")
        return

    try:
        api_id = int(api_id_str)
    except ValueError:
        logger.error(f"Telegram Mirror: TELEGRAM_API_ID must be an integer, got: {api_id_str}")
        return

    # Verify Discord channel at startup
    try:
        channel = bot.get_channel(int(discord_channel_id)) or await bot.fetch_channel(int(discord_channel_id))
        if channel:
            logger.info(f"Telegram Mirror: Verified Discord destination channel: #{channel.name} (ID: {discord_channel_id})")
        else:
            logger.error(f"Telegram Mirror: Discord destination channel ID {discord_channel_id} not found or is inaccessible.")
    except Exception as chan_err:
        logger.error(f"Telegram Mirror: Failed to verify Discord channel {discord_channel_id}: {chan_err}")

    logger.info("Initializing Telegram Mirror background client...")
    # Using 'groq_userbot_session' to reuse active local session credentials automatically
    client = TelegramClient('groq_userbot_session', api_id, api_hash)

    target_peer_id = None

    @client.on(events.NewMessage())
    async def mirror_handler(event):
        nonlocal target_peer_id
        try:
            # Skip outgoing messages sent by the userbot itself
            if event.out:
                return

            chat_id = event.chat_id

            is_target = False
            # Check by resolved peer ID first
            if target_peer_id and chat_id == target_peer_id:
                is_target = True
            # Fallback to hardcoded IDs
            elif str(chat_id) in ("-1002242176791", "2242176791", "-2242176791"):
                is_target = True
            else:
                # Retrieve chat username fallback (requires async API request)
                chat = await event.get_chat()
                if chat:
                    chat_username = getattr(chat, 'username', None)
                    if chat_username and chat_username.lower() == source_chat.lower():
                        is_target = True
                        # Update resolved target peer ID for subsequent messages
                        from telethon import utils
                        target_peer_id = utils.get_peer_id(chat)

            if not is_target:
                return

            text = event.message.message or ""
            # Allow messages with only media or text
            if not text and not event.message.media:
                return

            # Extract Ticker
            import re
            ticker = None
            ticker_match = re.search(r'\$[A-Za-z0-9_]+', text)
            if ticker_match:
                ticker = ticker_match.group(0).upper()

            # Extract Solana Contract Address
            ca = None
            ca_matches = re.findall(r'\b[1-9A-HJ-NP-Za-km-z]{32,44}\b', text)
            for match in ca_matches:
                if any(c.isdigit() for c in match) and any(c.isalpha() for c in match):
                    ca = match
                    break
            if not ca and ca_matches:
                ca = ca_matches[0]

            # Extract Links (Twitter/X, Website)
            # 1. From raw text URLs
            urls = re.findall(r'https?://[^\s<>"]+|www\.[^\s<>"]+', text)
            # Clean trailing punctuation from raw URLs
            urls = [re.sub(r'[.,)\]?!_*]+$', '', u) for u in urls]

            # 2. From rich text formatting entities
            from telethon.tl.types import MessageEntityTextUrl
            if event.message.entities:
                for entity in event.message.entities:
                    if isinstance(entity, MessageEntityTextUrl):
                        clean_url = entity.url.strip()
                        if clean_url not in urls:
                            urls.append(clean_url)

            x_links = []
            web_links = []
            for url in urls:
                if "twitter.com" in url.lower() or "x.com" in url.lower():
                    if url not in x_links:
                        x_links.append(url)
                elif "t.me" in url.lower() or "telegram" in url.lower():
                    pass
                else:
                    if url not in web_links:
                        web_links.append(url)

            # Build a cleaner description
            clean_desc = text
            if ca:
                clean_desc = clean_desc.replace(ca, "")
            for url in urls:
                clean_desc = clean_desc.replace(url, "")
            
            clean_desc = re.sub(r'\n\s*\n+', '\n\n', clean_desc).strip()

            # Download media if present
            import io
            media_bytes = None
            media_filename = "image.png"
            if event.message.media:
                if hasattr(event.message, 'photo') and event.message.photo:
                    media_bytes = await event.message.download_media(file=bytes)
                    media_filename = "photo.jpg"
                elif hasattr(event.message, 'document') and event.message.document:
                    mime = getattr(event.message.document, 'mime_type', '')
                    if mime.startswith('image/'):
                        media_bytes = await event.message.download_media(file=bytes)
                        media_filename = "photo.jpg"

            # Fetch token statistics if CA is found
            token_stats_value = None
            if ca:
                try:
                    import api_client
                    pairs = await api_client.get_token_by_ca(ca)
                    if pairs:
                        pair = pairs[0]
                        price_usd = pair.get("priceUsd")
                        fdv = pair.get("fdv")
                        liquidity = pair.get("liquidity", {}).get("usd")
                        volume_24h = pair.get("volume", {}).get("h24")
                        price_change_24h = pair.get("priceChange", {}).get("h24")
                        
                        stats_parts = []
                        if price_usd is not None:
                            try:
                                formatted_price = format_price(price_usd)
                            except Exception:
                                val_f = float(price_usd)
                                formatted_price = f"${val_f:,.6f}" if val_f < 1 else f"${val_f:,.2f}"
                            stats_parts.append(f"💵 **Price**: {formatted_price}")
                        if fdv is not None:
                            stats_parts.append(f"📈 **Market Cap**: ${float(fdv):,.0f}")
                        if liquidity is not None:
                            stats_parts.append(f"💧 **Liquidity**: ${float(liquidity):,.0f}")
                        if volume_24h is not None:
                            stats_parts.append(f"📊 **24h Volume**: ${float(volume_24h):,.0f}")
                        if price_change_24h is not None:
                            change_emoji = "🟢" if float(price_change_24h) >= 0 else "🔴"
                            stats_parts.append(f"{change_emoji} **24h Change**: {price_change_24h}%")
                        
                        if stats_parts:
                            token_stats_value = "\n".join(stats_parts)
                except Exception as stats_err:
                    logger.error(f"Error fetching token stats in mirror: {stats_err}")

            # Construct Discord Embed
            embed_title = "📢 Solana CTO Tracker Alert"
            if ticker:
                embed_title = f"📢 CTO Tracker | {ticker}"
            
            embed = discord.Embed(
                title=embed_title,
                description=clean_desc if clean_desc else "New update posted.",
                color=0x9b59b6
            )

            if ca:
                # Raw CA value (no backticks/quotes) for perfect copy-paste on mobile
                embed.add_field(name="🔑 Contract Address (Tap to copy)", value=ca, inline=False)
                
                if token_stats_value:
                    embed.add_field(name="📈 Token Statistics", value=token_stats_value, inline=False)
                
                analysis_tools = (
                    f"🔗 [Dexscreener](https://dexscreener.com/solana/{ca}) | "
                    f"[RugCheck](https://rugcheck.xyz/tokens/{ca}) | "
                    f"[GMGN](https://gmgn.ai/sol/token/{ca}) | "
                    f"[Solscan](https://solscan.io/token/{ca})"
                )
                embed.add_field(name="📊 Analysis Links", value=analysis_tools, inline=False)

            # Attached Links
            links_value = []
            if x_links:
                links_value.append(f"🐦 [Twitter/X]({x_links[0]})")
            if web_links:
                links_value.append(f"🌐 [Website]({web_links[0]})")
            if links_value:
                embed.add_field(name="🔗 Attached Links", value=" • ".join(links_value), inline=False)

            embed.set_footer(text="ctotrackersol mirror • 369bot")

            # Dispatch to configured Discord channel
            channel = bot.get_channel(int(discord_channel_id)) or await bot.fetch_channel(int(discord_channel_id))
            if channel:
                if media_bytes:
                    file = discord.File(io.BytesIO(media_bytes), filename=media_filename)
                    embed.set_image(url=f"attachment://{media_filename}")
                    await channel.send(embed=embed, file=file)
                else:
                    await channel.send(embed=embed)
                logger.info(f"Telegram Mirror: Mirrored message from ctotrackersol as embed to Discord channel {discord_channel_id}.")
            else:
                logger.error(f"Telegram Mirror: Could not find or send to Discord channel {discord_channel_id}")
        except Exception as handler_err:
            logger.error(f"Error in Telegram mirror event handler: {handler_err}")

    try:
        await client.connect()
        if not await client.is_user_authorized():
            logger.error("=" * 80)
            logger.error("TELEGRAM MIRROR AUTHENTICATION ERROR:")
            logger.error("Your Telegram session is NOT authorized in the 'discordbot' folder.")
            logger.error("To fix this and avoid entering a verification code:")
            logger.error("Copy the file 'groq_userbot_session.session' from the 'telegram bot' folder")
            logger.error("and paste it directly into this 'discordbot' folder.")
            logger.error("=" * 80)
            await client.disconnect()
            return

        # Resolve peer ID of the target channel once after connection
        try:
            target_entity = await client.get_entity(source_chat)
            from telethon import utils
            target_peer_id = utils.get_peer_id(target_entity)
            logger.info(f"Telegram Mirror: Resolved target source chat '{source_chat}' to peer ID {target_peer_id}")
        except Exception as resolve_err:
            logger.warning(f"Telegram Mirror: Could not resolve target source chat '{source_chat}' to entity at startup: {resolve_err}")

        logger.info("Telegram Mirror: Connected and listening to Telegram updates.")
        await client.run_until_disconnected()
    except Exception as start_err:
        logger.error(f"Failed to run Telegram Mirror client: {start_err}")


@bot.event
async def on_ready():
    logger.info(f"Bot connected as {bot.user.name} (ID: {bot.user.id})")
    # Set status
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="for contract addresses"))
    logger.info("Bot is ready and listening.")

    # Initialize premium DB and clean up old usage records
    try:
        premium_db.init_db()
        premium_db.clean_expired_and_old_usage()
    except Exception as e:
        logger.error(f"Error initializing premium database on ready: {e}")
    
    # Start KOL Tracker background task if not already running
    if not hasattr(bot, "kol_tracker_started"):
        bot.kol_tracker_started = True
        bot.loop.create_task(start_kol_tracker())

    # Start Telegram Mirror background task if not already running
    if not hasattr(bot, "telegram_mirror_started"):
        bot.telegram_mirror_started = True
        bot.loop.create_task(start_telegram_mirror())


@bot.command(name="sync")
async def sync_commands(ctx, spec: Optional[str] = None):
    """Synchronize slash commands. Use '!sync' for instant guild sync, or '!sync global' for global sync."""
    # Check permissions
    is_owner = await ctx.bot.is_owner(ctx.author)
    is_admin = ctx.author.guild_permissions.administrator if ctx.guild else False
    if not (is_owner or is_admin):
        await ctx.send("❌ You do not have permission to sync commands (requires Administrator or Bot Owner).")
        return

    if not spec:
        # Guild Sync (Instant for the current server)
        if not ctx.guild:
            await ctx.send("❌ Instant sync can only be run within a server.")
            return
        await ctx.send("🔄 Copying global commands to this guild and syncing instantly...")
        try:
            ctx.bot.tree.copy_global_to(guild=ctx.guild)
            synced = await ctx.bot.tree.sync(guild=ctx.guild)
            await ctx.send(f"✅ Successfully synced {len(synced)} slash commands to this guild instantly! Try typing `/trending` or `/ca` now.")
            logger.info(f"Synced {len(synced)} commands to guild {ctx.guild.id}")
        except Exception as e:
            await ctx.send(f"❌ Error syncing commands to this guild: {e}")
            logger.error(f"Failed to guild-sync: {e}")
    elif spec.lower() == "global":
        # Global Sync (Takes up to 1 hour to propagate on Discord)
        await ctx.send("🔄 Syncing slash commands globally... (This can take up to 1 hour to propagate on Discord)")
        try:
            synced = await ctx.bot.tree.sync()
            await ctx.send(f"✅ Successfully synced {len(synced)} slash commands globally! They will register soon.")
            logger.info(f"Synced {len(synced)} slash commands globally.")
        except Exception as e:
            await ctx.send(f"❌ Error syncing slash commands: {e}")
            logger.error(f"Failed to global-sync: {e}")
    else:
        await ctx.send("❌ Invalid argument. Use `!sync` for guild sync, or `!sync global` for global sync.")


# 1. HELP COMMANDS

@bot.command(name="help")
async def help_prefix(ctx):
    """Prefix help command."""
    embed = get_help_embed()
    await ctx.send(embed=embed)


@bot.tree.command(name="help", description="Learn how to use the bot and its commands")
async def help_slash(interaction: discord.Interaction):
    """Slash help command."""
    embed = get_help_embed()
    await interaction.response.send_message(embed=embed)


def get_help_embed() -> discord.Embed:
    """Generates the help info embed."""
    embed = discord.Embed(
        title="🤖 Memecoin Tracker Help Panel",
        description="I am a premium multi-chain token tracker. Just drop a contract address in any text channel, or use the commands below!",
        color=DEFAULT_COLOR
    )
    embed.add_field(
        name="🚀 Core Commands",
        value=(
            f"`{COMMAND_PREFIX}trending [chain]` or `/trending`\n"
            "Shows real-time top trending token pools. You can toggle chains dynamically in the UI!\n\n"
            f"`{COMMAND_PREFIX}ca <address>` or `/ca <address>`\n"
            "Displays detailed statistics, price changes, charts, buy links, and socials for a token."
        ),
        inline=False
    )
    embed.add_field(
        name="✨ Auto-Detect Address",
        value=(
            "Simply paste a token contract address in any message! I will scan it, search across chains, and post the stats instantly:\n"
            "- **Solana CA:** e.g., `EPjFWdd5...G4wEGGkZwyTDt1v`\n"
            "- **EVM CA (Base/BNB/Eth):** e.g., `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`"
        ),
        inline=False
    )
    embed.add_field(
        name="🔗 Supported Blockchains",
        value="Solana 🟣, Base 🔵, BNB Chain 🟡, Robinhood 🟢, Ethereum ⚫, Arbitrum 🔵, Optimism 🔴, Avalanche 🔺, Polygon 🟣",
        inline=False
    )
    embed.add_field(
        name="🔍 Due Diligence Tools",
        value=(
            f"`{COMMAND_PREFIX}checkuser <@handle or domain>` or `/checkuser <query>`\n"
            "🕵️ **Audit Profile & History** — Scan an X handle or website domain to check for scams, "
            "rebrands/username history timeline, fake news sites, impersonation, and domain age risks."
        ),
        inline=False
    )
    embed.set_footer(text="Developed for next-gen crypto tracking.")
    return embed


# 2. TRENDING COMMANDS

@bot.command(name="trending")
async def trending_prefix(ctx, chain: str = "global"):
    """Prefix trending command."""
    async with ctx.typing():
        network_arg = None if chain.lower() == "global" else chain
        pools = await api_client.get_trending_pools(network=network_arg)
        
        if pools is None:
            await ctx.send("Failed to fetch trending pools. The API might be rate-limited.")
            return
            
        embed = create_trending_embed(pools, chain)
        view = TrendingView(current_chain=chain.lower())
        await ctx.send(embed=embed, view=view)


@bot.tree.command(name="trending", description="View trending crypto token pools across chains")
@app_commands.describe(chain="The blockchain network to display (e.g. solana, base, bsc, robinhood)")
@app_commands.choices(chain=[
    app_commands.Choice(name="Global", value="global"),
    app_commands.Choice(name="Solana", value="solana"),
    app_commands.Choice(name="Base", value="base"),
    app_commands.Choice(name="BNB Chain", value="bsc"),
    app_commands.Choice(name="Robinhood", value="robinhood"),
    app_commands.Choice(name="Ethereum", value="ethereum"),
    app_commands.Choice(name="Arbitrum", value="arbitrum"),
    app_commands.Choice(name="Optimism", value="optimism"),
    app_commands.Choice(name="Avalanche", value="avalanche")
])
async def trending_slash(interaction: discord.Interaction, chain: Optional[app_commands.Choice[str]] = None):
    """Slash trending command."""
    try:
        await interaction.response.defer()
    except discord.NotFound:
        logger.warning("Trending slash interaction expired before defer could be completed.")
        return
    except Exception as e:
        logger.error(f"Error deferring trending slash: {e}")
        return
        
    selected_chain = chain.value if chain else "global"
    network_arg = None if selected_chain == "global" else selected_chain
    
    try:
        pools = await api_client.get_trending_pools(network=network_arg)
        if pools is None:
            await interaction.followup.send("Failed to fetch trending pools. The API might be rate-limited.", ephemeral=True)
            return
            
        embed = create_trending_embed(pools, selected_chain)
        view = TrendingView(current_chain=selected_chain)
        await interaction.followup.send(embed=embed, view=view)
    except discord.NotFound:
        logger.warning("Trending slash interaction expired before followup could be sent.")
    except Exception as e:
        logger.error(f"Error in trending_slash: {e}")


# 3. CONTRACT ADDRESS INFO COMMANDS

@bot.command(name="ca", aliases=["info"])
async def ca_prefix(ctx, address: str):
    """Prefix command to get token info by contract address."""
    # Channel/category gate
    if not is_channel_allowed(ctx.channel):
        return  # silently ignore outside allowed channels/categories

    allowed, count = premium_db.check_and_increment_usage(str(ctx.author.id), "ca", 10)
    if not allowed:
        embed = discord.Embed(
            title="🔒 Daily Limit Reached",
            description=(
                "Non-paid users can only use `/ca` **10 times per day**.\n"
                f"You've used all 10 queries today.\n\n"
                "Upgrade to **Premium** for just **$49/month** for unlimited access!"
            ),
            color=0xFF3B30
        )
        await ctx.send(embed=embed, view=UpgradeView(SOLANA_PAYMENT_ADDRESS, show_monthly=True))
        return

    async with ctx.typing():
        extracted = extract_ca(address)
        if extracted:
            address = extracted
            
        pairs = await api_client.get_token_by_ca(address)
        if not pairs:
            await ctx.send("No trading pairs found for this contract address on DexScreener.")
            return
            
        primary_pair = pairs[0]
        embed = await create_token_embed(primary_pair)
        view = TokenInfoView(primary_pair, user_id=ctx.author.id)
        await ctx.send(embed=embed, view=view)


@bot.tree.command(name="ca", description="Get detailed statistics and info for a token contract address")
@app_commands.describe(address="The token's contract address (EVM or Solana)")
async def ca_slash(interaction: discord.Interaction, address: str):
    """Slash command to get token info by contract address."""
    # Channel/category gate
    if not is_channel_allowed(interaction.channel):
        # Build a helpful message listing where the command is allowed
        hints = []
        if CA_CHANNEL_ID:
            hints.append(f"<#{CA_CHANNEL_ID}>")
        if ALLOWED_CATEGORY_IDS:
            hints.append("the designated category on your server")
        hint_str = " or ".join(hints) if hints else "the designated channel"
        await interaction.response.send_message(
            f"❌ `/ca` can only be used in {hint_str}.",
            ephemeral=True
        )
        return

    allowed, count = premium_db.check_and_increment_usage(str(interaction.user.id), "ca", 10)
    if not allowed:
        embed = discord.Embed(
            title="🔒 Daily Limit Reached",
            description=(
                "Non-paid users can only use `/ca` **10 times per day**.\n"
                f"You've used all 10 queries today.\n\n"
                "Upgrade to **Premium** for just **$49/month** for unlimited access!"
            ),
            color=0xFF3B30
        )
        await interaction.response.send_message(embed=embed, view=UpgradeView(SOLANA_PAYMENT_ADDRESS, show_monthly=True), ephemeral=True)
        return

    try:
        await interaction.response.defer()
    except discord.NotFound:
        logger.warning("ca_slash interaction expired before defer could be completed.")
        return
    except Exception as e:
        logger.error(f"Error deferring ca_slash: {e}")
        return
        
    extracted = extract_ca(address)
    if extracted:
        address = extracted
        
    try:
        pairs = await api_client.get_token_by_ca(address)
        if not pairs:
            await interaction.followup.send("No trading pairs found for this contract address on DexScreener.", ephemeral=True)
            return
            
        primary_pair = pairs[0]
        embed = await create_token_embed(primary_pair)
        view = TokenInfoView(primary_pair, user_id=interaction.user.id)
        await interaction.followup.send(embed=embed, view=view)
    except discord.NotFound:
        logger.warning("ca_slash interaction expired before followup could be sent.")
    except Exception as e:
        logger.error(f"Error in ca_slash: {e}")
        try:
            await interaction.followup.send("An error occurred while processing this request.", ephemeral=True)
        except Exception:
            pass


# ----------------- GETBALANCE COMMAND -----------------

@bot.command(name="getbalance", aliases=["bal", "balance"])
async def getbalance_prefix(ctx, address: str):
    """Prefix command to get wallet balance for a Solana or EVM address."""
    async with ctx.typing():
        embed = await _build_balance_embed(address)
        await ctx.send(embed=embed)


@bot.tree.command(name="getbalance", description="Check native token balance for any Solana or EVM wallet address")
@app_commands.describe(address="The wallet address (Solana or EVM)")
async def getbalance_slash(interaction: discord.Interaction, address: str):
    """Slash command to get wallet balance."""
    try:
        await interaction.response.defer()
    except discord.NotFound:
        return
    except Exception:
        return

    try:
        embed = await _build_balance_embed(address)
        await interaction.followup.send(embed=embed)
    except discord.NotFound:
        pass
    except Exception as e:
        logger.error(f"Error in getbalance_slash: {e}")
        try:
            await interaction.followup.send("An error occurred while fetching the balance.", ephemeral=True)
        except Exception:
            pass


async def _build_balance_embed(address: str) -> discord.Embed:
    """Builds a balance embed for a given wallet address (auto-detects Solana vs EVM)."""
    address = address.strip()

    # Detect address type
    is_solana = bool(SOL_REGEX.match(address))
    is_evm = bool(EVM_REGEX.match(address))

    if not is_solana and not is_evm:
        return discord.Embed(
            title="\u274c Invalid Address",
            description="Please provide a valid Solana or EVM wallet address.",
            color=0xFF0000
        )

    short_addr = f"`{address[:6]}...{address[-4:]}`"

    if is_solana:
        # Fetch SOL balance using Helius RPC
        http_url, _ = get_solana_rpc_urls()
        sol_balance = await api_client.get_sol_balance(address, rpc_url=http_url)

        embed = discord.Embed(
            title=f"\U0001f4b0 Wallet Balance",
            description=f"\U0001f50d **Address:** {short_addr}\n\U0001f517 **Chain:** Solana",
            color=0x9945FF
        )
        sol_emoji = "\U0001f7e2" if sol_balance > 0.01 else "\U0001f534"
        embed.add_field(
            name="\u25ce SOL Balance",
            value=f"{sol_emoji} **{sol_balance:,.6f} SOL**",
            inline=False
        )

    else:
        # Fetch EVM balances across all chains
        evm_balances = await api_client.get_evm_balance(address)

        embed = discord.Embed(
            title=f"\U0001f4b0 Wallet Balance",
            description=f"\U0001f50d **Address:** {short_addr}\n\U0001f517 **Chains:** EVM (Multi-Chain)",
            color=0x627EEA
        )

        chain_display = {
            "ethereum": ("\u2b26 ETH", "ETH"),
            "base": ("\U0001f535 Base", "ETH"),
            "bsc": ("\U0001f7e1 BSC", "BNB"),
            "arbitrum": ("\U0001f535 Arbitrum", "ETH"),
        }

        for chain_key, balance in evm_balances.items():
            display_name, token_symbol = chain_display.get(chain_key, (chain_key, "ETH"))
            bal_emoji = "\U0001f7e2" if balance > 0.0001 else "\U0001f534"
            embed.add_field(
                name=display_name,
                value=f"{bal_emoji} **{balance:,.6f} {token_symbol}**",
                inline=True
            )

    embed.set_footer(text="Balance shown is native token only (not SPL/ERC-20 tokens)")
    return embed


# ----------------- /CHECKUSER DUE DILIGENCE & USERNAME HISTORY COMMAND -----------------

async def _build_checkuser_embed(query: str) -> discord.Embed:
    """Builds a comprehensive checkuser due diligence audit embed for an X account or website domain."""
    import datetime as dt

    query = query.strip()
    
    handle: Optional[str] = None
    domain: Optional[str] = None
    
    # Parse query inputs
    if "twitter.com/" in query.lower() or "x.com/" in query.lower():
        handle = query.lower().split("twitter.com/")[-1].split("x.com/")[-1].split("/")[0].split("?")[0].lstrip("@")
    elif query.startswith("@"):
        handle = query.lstrip("@")
    elif "." in query and not query.startswith("0x"):
        # Could be a domain or domain + handle
        clean_q = query.lower().removeprefix("https://").removeprefix("http://").removeprefix("www.")
        parts = clean_q.split("/")
        domain = parts[0]
        if len(parts) > 1 and parts[1].startswith("@"):
            handle = parts[1].lstrip("@")
    else:
        handle = query.lstrip("@")

    if not handle and not domain:
        return discord.Embed(
            title="❌ Invalid Input",
            description="Please provide a valid X handle or domain. Example: `/checkuser @vainxyz` or `/checkuser cnn.com`",
            color=0xFF3B30
        )

    # Fetch data concurrently
    t_data, d_data = None, None
    tasks = []
    if handle:
        tasks.append(api_client.get_twitter_audit(handle))
    else:
        tasks.append(asyncio.sleep(0))
        
    if domain:
        tasks.append(api_client.get_domain_info(domain))
    else:
        tasks.append(asyncio.sleep(0))
        
    results = await asyncio.gather(*tasks, return_exceptions=True)
    if handle and isinstance(results[0], Exception):
        logger.error(f"[_build_checkuser_embed] Twitter audit task exception for '{handle}': {results[0]}")
    elif handle:
        t_data = results[0]
        
    if domain and isinstance(results[1], Exception):
        logger.error(f"[_build_checkuser_embed] Domain audit task exception for '{domain}': {results[1]}")
    elif domain:
        d_data = results[1]

    score, flags = api_client.calculate_risk_score(t_data, d_data)

    # Color selection based on risk score & rename count
    rename_count = t_data.get("rename_count", 0) if t_data else 0
    
    if score >= 7 or rename_count >= 5:
        color = 0xFF3B30  # Red
        risk_label = "⛔ EXTREME RISK"
        risk_emoji = "🚨"
    elif score >= 4 or rename_count >= 2:
        color = 0xFFB800  # Yellow
        risk_label = "⚠️ MEDIUM RISK"
        risk_emoji = "⚠️"
    else:
        color = 0x00FFA3  # Green
        risk_label = "✅ LOW RISK"
        risk_emoji = "✅"

    embed = discord.Embed(
        title=f"🕵️ CheckUser Audit: `{query}`",
        description=f"**Risk Score:** `{score}/10` — **{risk_label}**",
        color=color
    )

    # Red flags callout block
    if flags:
        flags_formatted = "\n".join([f"• {f}" for f in flags])
        embed.add_field(name="🚨 Red Flags & Warnings", value=flags_formatted, inline=False)
    else:
        embed.add_field(name="🛡️ Security Status", value="✅ No major red flags detected during automated scan.", inline=False)

    # X Account Audit section
    if t_data and not t_data.get("error"):
        disp = t_data.get("display_name") or f"@{handle}"
        joined = t_data.get("joined_date") or "Unknown"
        j_days = t_data.get("joined_days_ago")
        age_str = f"({j_days} days ago)" if j_days is not None else ""
        followers = t_data.get("followers") or "N/A"
        tweets = t_data.get("tweet_count") or "N/A"
        user_id_str = f" (`ID: {t_data['user_id']}`)" if t_data.get("user_id") else ""
        following = t_data.get("following") or "N/A"

        # Verified Badge Status
        if t_data.get("is_verified"):
            v_badge = f"☑️ Verified ({t_data.get('verified_type', 'Blue')})"
        else:
            v_badge = "❌ Unverified Account"

        t_value = (
            f"👤 **Name / Nickname:** {disp}{user_id_str}\n"
            f"🛡️ **Verified Status:** {v_badge}\n"
            f"📅 **Created:** {joined} {age_str}\n"
            f"👥 **Followers:** `{followers}` | **Following:** `{following}` | 🐦 **Tweets:** `{tweets}`\n"
        )
        if t_data.get("nickname_impersonation"):
            t_value += f"\n🚨 **IMPERSONATION WARNING:** Nickname claims brand **{t_data.get('impersonated_brand')}** on an unverified handle!"
        if t_data.get("bio"):
            t_value += f"\n📝 **Bio:** *{t_data['bio'][:120]}...*"

        embed.add_field(name=f"👤 Twitter/X Profile (@{handle})", value=t_value, inline=False)

        # -- Past handle timeline --
        past_handles = t_data.get("historical_handles_detail", [])
        if past_handles:
            timeline_lines = []
            for i, entry in enumerate(past_handles, 1):
                h = entry.get("handle", "").lstrip("@")
                first_s = entry.get("first_seen")
                last_s = entry.get("last_seen")

                # Format date strings: if ISO datetime, strip to date only
                def _fmt_date(d):
                    if not d:
                        return "?"
                    try:
                        # ISO datetime: 2024-01-15T... → Jan 15, 2024
                        if "T" in str(d):
                            return dt.datetime.fromisoformat(d.replace("Z", "+00:00")).strftime("%b %d, %Y")
                        # Already readable (e.g. "Jan 15, 2024" from Wayback or memory.lol date)
                        return str(d)
                    except Exception:
                        return str(d)[:10]

                first_fmt = _fmt_date(first_s)
                last_fmt = _fmt_date(last_s)

                if first_s or last_s:
                    if first_fmt == last_fmt:
                        date_info = f" _(seen: {first_fmt})_"
                    else:
                        date_info = f" _(active: {first_fmt} → {last_fmt})_"
                else:
                    date_info = " _(date unknown)_"

                timeline_lines.append(f"`{i}.` **@{h}**{date_info}")

            embed.add_field(
                name=f"🔄 Past Username Timeline ({rename_count} rename{'s' if rename_count != 1 else ''})",
                value="\n".join(timeline_lines[:20]),  # cap at 20 to avoid Discord embed overflow
                inline=False
            )
            if rename_count > 20:
                embed.add_field(
                    name="",
                    value=f"_...and {rename_count - 20} more past handles not shown._",
                    inline=False
                )
        else:
            embed.add_field(
                name="✅ Username History",
                value=(
                    "No past username changes found in:\n"
                    "• `memory.lol` username history index\n"
                    "• Wayback Machine CDX archive\n\n"
                    "_Note: Scans only go back as far as archived records exist. The account may still have renamed in periods not indexed._"
                ),
                inline=False
            )

        # -- Risk tip --
        if rename_count >= 2:
            embed.add_field(
                name="💡 What This Means",
                value=(
                    "Frequent username rebranding is a **common rug-pull tactic** used to:\n"
                    "• Impersonate legitimate projects or KOLs\n"
                    "• Wipe negative reputation from a previous scam\n"
                    "• Hide prior failed pump-and-dump launches\n\n"
                    "**Always research the full handle history before trusting a dev or KOL!**"
                ),
                inline=False
            )

    elif handle:
        embed.add_field(name=f"👤 Twitter/X Profile (@{handle})", value="⚠️ Could not scrape live X profile (account may be private, suspended, or handle typo).", inline=False)

    # Domain Audit section
    if d_data:
        dom_name = d_data.get("domain", domain)
        reg_date = d_data.get("registered_date") or "Unknown"
        exp_date = d_data.get("expiry_date") or "Unknown"
        d_days = d_data.get("days_old")
        d_age_str = f"({d_days} days old)" if d_days is not None else ""
        registrar = d_data.get("registrar") or "Unknown"
        priv = "Protected (Hidden)" if d_data.get("privacy_protected") else "Public / Visible"
        d_arch = d_data.get("first_archived") or "Never"

        d_value = (
            f"🌐 **Domain:** `{dom_name}`\n"
            f"📅 **Registered:** {reg_date} {d_age_str}\n"
            f"⏳ **Expires:** {exp_date}\n"
            f"🏢 **Registrar:** {registrar}\n"
            f"🔒 **Privacy:** {priv}\n"
            f"🕸️ **First Archived:** {d_arch}"
        )

        lk = d_data.get("lookalike", {})
        if lk.get("is_verified"):
            d_value += "\n✅ **VERIFIED OFFICIAL DOMAIN** (Legitimate Media / Protocol Outlet)"
        elif lk.get("is_lookalike"):
            d_value += f"\n🚨 **FAKE DOMAIN WARNING:** Typosquatting of **{lk.get('similar_to')}** ({lk.get('similarity_pct')}% match)!"
        else:
            d_value += "\n⚠️ **UNVERIFIED DOMAIN:** Unknown outlet (not in official media registry)."

        embed.add_field(name=f"🌐 Website / Domain Audit (`{dom_name}`)", value=d_value, inline=False)

    # Links
    if handle:
        embed.add_field(
            name="🔗 External Links",
            value=(
                f"[View on X/Twitter](https://x.com/{handle}) • "
                f"[memory.lol history](https://memory.lol/tw/{handle}) • "
                f"[Wayback Machine](https://web.archive.org/web/*/twitter.com/{handle})"
            ),
            inline=False
        )

    footer_text = "💡 Tip: Scammers frequently use fake news sites (e.g. CNN lookalikes) or recycled X accounts before rugging."
    embed.set_footer(text=footer_text)
    return embed


@bot.command(name="checkuser", aliases=["userhistory", "handlecheck", "userhist", "check"])
async def checkuser_prefix(ctx, *, query: str):
    """Prefix command to perform due diligence check & check username history on an X/Twitter account or domain."""
    allowed, count = premium_db.check_and_increment_usage(str(ctx.author.id), "checkuser", 5)
    if not allowed:
        embed = discord.Embed(
            title="🔒 Daily Limit Reached",
            description=(
                "Non-paid users can only use `/checkuser` **5 times per day**.\n"
                f"You've used all 5 audits today.\n\n"
                "Upgrade to **Premium** for just **$49/month** for unlimited access!"
            ),
            color=0xFF3B30
        )
        await ctx.send(embed=embed, view=UpgradeView(SOLANA_PAYMENT_ADDRESS, show_monthly=True))
        return

    async with ctx.typing():
        embed = await _build_checkuser_embed(query)
        await ctx.send(embed=embed)


@bot.tree.command(name="checkuser", description="Perform due diligence & check username change history for an X/Twitter account or website domain")
@app_commands.describe(query="X handle (e.g. @devname) or website domain (e.g. cnn.com or suspicious-site.xyz)")
async def checkuser_slash(interaction: discord.Interaction, query: str):
    """Slash command to audit X username change history / website domain."""
    allowed, count = premium_db.check_and_increment_usage(str(interaction.user.id), "checkuser", 5)
    if not allowed:
        embed = discord.Embed(
            title="🔒 Daily Limit Reached",
            description=(
                "Non-paid users can only use `/checkuser` **5 times per day**.\n"
                f"You've used all 5 audits today.\n\n"
                "Upgrade to **Premium** for just **$49/month** for unlimited access!"
            ),
            color=0xFF3B30
        )
        await interaction.response.send_message(embed=embed, view=UpgradeView(SOLANA_PAYMENT_ADDRESS, show_monthly=True), ephemeral=True)
        return

    try:
        await interaction.response.defer()
    except discord.NotFound:
        return
    except Exception:
        return

    try:
        embed = await _build_checkuser_embed(query)
        await interaction.followup.send(embed=embed)
    except discord.NotFound:
        pass
    except Exception as e:
        logger.error(f"Error in checkuser_slash: {e}")
        try:
            await interaction.followup.send("An error occurred while performing the check.", ephemeral=True)
        except Exception:
            pass


# ----------------- SOLANA PAYMENT & UPGRADE SYSTEM -----------------

# In-memory set of verified signature strings
_verified_signatures = set()

def is_signature_used(sig: str) -> bool:
    return sig in _verified_signatures

def mark_signature_used(sig: str):
    _verified_signatures.add(sig)

# In-memory session store: reference_pubkey -> {user_id, created_at, amount_usd, months}
_payment_sessions: dict = {}

def create_payment_session(reference: str, user_id: str, amount_usd: float, months: int):
    """Register a new payment session keyed by its reference pubkey."""
    import time
    _payment_sessions[reference] = {
        "user_id": user_id,
        "created_at": time.time(),
        "amount_usd": amount_usd,
        "months": months,
    }

def pop_payment_session(reference: str) -> dict | None:
    """Consume and return a payment session, or None if not found/expired."""
    import time
    session = _payment_sessions.pop(reference, None)
    if session and (time.time() - session["created_at"]) > 1800:  # 30 min max
        return None
    return session

async def split_funds_via_jito(amount_sol: float, amount_usd: float):
    """Splits received funds and submits transaction privately via Jito to prevent MEV."""
    try:
        priv_key_str = os.getenv("SOLANA_PRIVATE_KEY")
        addr_a_str = os.getenv("SPLIT_ADDRESS_A")
        addr_b_str = os.getenv("SPLIT_ADDRESS_B")
        
        if not priv_key_str or not addr_a_str or not addr_b_str:
            logger.info("Split addresses or private key not configured. Skipping fund split.")
            return

        from solders.keypair import Keypair
        from solders.pubkey import Pubkey
        from solders.system_program import transfer, TransferParams
        from solders.message import Message
        from solders.transaction import Transaction
        from solders.hash import Hash
        import base64
        
        sender = Keypair.from_base58_string(priv_key_str.strip())
        pubkey_a = Pubkey.from_string(addr_a_str.strip())
        pubkey_b = Pubkey.from_string(addr_b_str.strip())
        
        http_url, _ = get_solana_rpc_urls()
        headers = {"Content-Type": "application/json"}
        blockhash = Hash.default()
        
        async with aiohttp.ClientSession() as session:
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getLatestBlockhash",
                "params": [{"commitment": "finalized"}]
            }
            async with session.post(http_url, json=payload, headers=headers) as resp:
                data = await resp.json()
                if "result" in data and "value" in data["result"]:
                    blockhash_str = data["result"]["value"]["blockhash"]
                    blockhash = Hash.from_string(blockhash_str)
                    
        lamports_total = int(amount_sol * 1e9)
        fee = 5000  # 0.000005 SOL network fee
        net_lamports = lamports_total - fee
        if net_lamports <= 0:
            return
            
        # Split equally (50/50) for both $49 and $300 payments
        if amount_usd == 49.0 or amount_usd == 300.0:
            lamports_a = net_lamports // 2
            lamports_b = net_lamports - lamports_a
        else:
            # Default fallback split
            lamports_a = net_lamports // 2
            lamports_b = net_lamports - lamports_a
            
        ix_a = transfer(TransferParams(from_pubkey=sender.pubkey(), to_pubkey=pubkey_a, lamports=lamports_a))
        ix_b = transfer(TransferParams(from_pubkey=sender.pubkey(), to_pubkey=pubkey_b, lamports=lamports_b))
        
        msg = Message([ix_a, ix_b], sender.pubkey())
        tx = Transaction([sender], msg, blockhash)
        tx_b64 = base64.b64encode(bytes(tx)).decode('utf-8')
        
        jito_url = "https://mainnet.block-engine.jito.wtf/api/v1/transactions"
        jito_payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendTransaction",
            "params": [tx_b64, {"encoding": "base64", "preflightCommitment": "processed"}]
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(jito_url, json=jito_payload, headers=headers) as resp:
                result = await resp.json()
                logger.info(f"Fund split transaction submitted to Jito: {result}")
                
    except Exception as e:
        logger.error(f"Failed to split funds via Jito: {e}")

async def verify_solana_reference_payment(reference_address: str, receiver_address: str, amount_usd: float = 1.0) -> tuple[bool, str, float]:
    """Verifies a Solana payment by scanning for incoming transfers on the receiver address."""
    reference_address = reference_address.strip()
    receiver_address = receiver_address.strip()

    # Get SOL price consistently from Coinbase API
    sol_price = 150.0
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://api.coinbase.com/v2/prices/SOL-USD/spot") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    sol_price = float(data["data"]["amount"])
    except Exception as e:
        logger.error(f"Error fetching SOL price in verifier: {e}")

    expected_sol = amount_usd / sol_price
    min_sol = expected_sol * 0.99   # 1% slippage tolerance
    min_lamports = int(min_sol * 1e9)

    logger.info(f"[Verifier] session={reference_address[:12]}... receiver={receiver_address[:12]}... need>={min_lamports} lamports")

    http_url, _ = get_solana_rpc_urls()
    headers = {"Content-Type": "application/json"}

    # Get session creation time so we only look at txs AFTER the QR was generated
    import time
    now_ts = time.time()
    session_data = _payment_sessions.get(reference_address)
    created_at = session_data["created_at"] if session_data else (now_ts - 600)

    sig_payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getSignaturesForAddress",
        "params": [receiver_address, {"limit": 20}]
    }

    try:
        async with aiohttp.ClientSession() as http_sess:
            async with http_sess.post(http_url, json=sig_payload, headers=headers) as resp:
                if resp.status != 200:
                    return False, "Failed to reach Solana RPC node.", 0.0
                sig_data = await resp.json()
                signatures_info = sig_data.get("result", [])
                logger.info(f"[Verifier] {len(signatures_info)} tx(s) found on receiver")
                if not signatures_info:
                    return False, "No transactions found on the payment address yet.", 0.0

            for sig_info in signatures_info:
                sig = sig_info.get("signature")
                block_time = sig_info.get("blockTime") or 0

                # Only consider txs after QR was generated (with 60s buffer for clock drift)
                if block_time < (created_at - 60):
                    logger.info(f"[Verifier] Skip old tx blockTime={block_time} < created={created_at:.0f}")
                    continue

                # Skip already-claimed signatures
                if is_signature_used(sig):
                    logger.info(f"[Verifier] Skip used sig {sig[:16]}...")
                    continue

                tx_payload = {
                    "jsonrpc": "2.0", "id": 1,
                    "method": "getTransaction",
                    "params": [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}]
                }
                async with http_sess.post(http_url, json=tx_payload, headers=headers) as tx_resp:
                    if tx_resp.status != 200:
                        continue
                    tx_data = await tx_resp.json()
                    result = tx_data.get("result")
                    if not result:
                        continue

                    meta = result.get("meta", {})
                    if meta.get("err") is not None:
                        continue

                    transaction = result.get("transaction", {})
                    message = transaction.get("message", {})
                    account_keys = message.get("accountKeys", [])

                    resolved_keys = []
                    for k in account_keys:
                        resolved_keys.append(k.get("pubkey") if isinstance(k, dict) else str(k))

                    if receiver_address not in resolved_keys:
                        continue

                    receiver_idx = resolved_keys.index(receiver_address)
                    pre_balances = meta.get("preBalances", [])
                    post_balances = meta.get("postBalances", [])

                    if receiver_idx < len(pre_balances) and receiver_idx < len(post_balances):
                        change = post_balances[receiver_idx] - pre_balances[receiver_idx]
                        logger.info(f"[Verifier] Tx {sig[:16]}... change={change} lamports need={min_lamports}")
                        if change >= min_lamports:
                            mark_signature_used(sig)
                            _payment_sessions.pop(reference_address, None)  # consume session
                            sol_received = change / 1e9
                            return True, f"Found valid transfer of {sol_received:.4f} SOL — [View on Solscan](https://solscan.io/tx/{sig})", sol_received

    except Exception as e:
        logger.error(f"Error in verify_solana_reference_payment: {e}")
        return False, f"Verification failed due to an error: {e}", 0.0
        
    return False, f"Could not find any recent SOL transfer matching ${amount_usd:.2f} USD (min {min_sol:.5f} SOL) with the required reference ID.", 0.0


class PaymentVerificationView(discord.ui.View):
    def __init__(self, reference_address: str, receiver_address: str, amount_usd: float, months: int):
        super().__init__(timeout=300)
        self.reference_address = reference_address
        self.receiver_address = receiver_address
        self.amount_usd = amount_usd
        self.months = months
        
        # Link button to dancryptic's direct Discord profile/DM for support
        self.add_item(discord.ui.Button(
            label="Support",
            style=discord.ButtonStyle.link,
            url="https://discord.com/users/1248988195006447688",
            emoji="💬"
        ))

    @discord.ui.button(label="I Paid", style=discord.ButtonStyle.primary, emoji="✅")
    async def btn_i_paid(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        success, msg, sol_received = await verify_solana_reference_payment(self.reference_address, self.receiver_address, self.amount_usd)
        
        if success:
            try:
                expiry = premium_db.add_premium_user(str(interaction.user.id), interaction.user.name, self.months)
                
                # Trigger Jito split async task
                bot.loop.create_task(split_funds_via_jito(sol_received, self.amount_usd))
                
            except Exception as e:
                logger.error(f"Payment verified but premium activation failed for {interaction.user.id}: {e}")
                await interaction.followup.send(
                    "Payment verified, but premium activation failed while writing to Supabase. "
                    "Please contact an admin with this message.",
                    ephemeral=True
                )
                return
            try:
                expiry_dt = datetime.datetime.fromisoformat(expiry)
                expiry_str = expiry_dt.strftime("%Y-%m-%d %H:%M:%S UTC")
            except Exception:
                expiry_str = expiry
                
            embed = discord.Embed(
                title="🎉 Premium Activated!",
                description=(
                    f"Thank you for your purchase, **{interaction.user.name}**! "
                    f"Your subscription is now active.\n\n"
                    f"📅 **Expiry Date:** `{expiry_str}`\n\n"
                    f"ℹ️ {msg}"
                ),
                color=0x00FFA3
            )
            # Disable button to prevent reuse
            for child in self.children:
                child.disabled = True
            await interaction.edit_original_response(view=self)
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            embed = discord.Embed(
                title="❌ Verification Failed",
                description=(
                    f"We could not verify a payment for this reference ID yet.\n\n"
                    f"🔍 **Reason:** {msg}\n\n"
                    f"Please wait a moment for the transaction to confirm and click 'I Paid' again."
                ),
                color=0xFF3B30
            )
            await interaction.followup.send(embed=embed, ephemeral=True)


class UpgradeView(discord.ui.View):
    def __init__(self, receiver_address: str, show_monthly: bool = True):
        super().__init__(timeout=180)
        self.receiver_address = receiver_address
        
        if show_monthly:
            btn_monthly = discord.ui.Button(label="Pay Monthly ($49)", style=discord.ButtonStyle.success, emoji="💰")
            btn_monthly.callback = self.btn_pay_monthly
            self.add_item(btn_monthly)
            
        btn_yearly = discord.ui.Button(label="Pay Yearly ($300)", style=discord.ButtonStyle.primary, emoji="📅")
        btn_yearly.callback = self.btn_pay_yearly
        self.add_item(btn_yearly)

        # Support button next to payment buttons
        self.add_item(discord.ui.Button(
            label="Support",
            style=discord.ButtonStyle.link,
            url="https://discord.com/users/1248988195006447688",
            emoji="💬"
        ))

    async def btn_pay_monthly(self, interaction: discord.Interaction):
        await self.generate_payment(interaction, 49.0, 1)

    async def btn_pay_yearly(self, interaction: discord.Interaction):
        await self.generate_payment(interaction, 300.0, 12)

    async def generate_payment(self, interaction: discord.Interaction, amount_usd: float, months: int):
        await interaction.response.defer(ephemeral=True)
        
        # Generate a valid Solana reference keypair using solders
        from solders.keypair import Keypair as SoldersKeypair
        ref_keypair = SoldersKeypair()
        reference_pubkey = str(ref_keypair.pubkey())
        
        # Get SOL price consistently from Coinbase API
        sol_price = 150.0
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get("https://api.coinbase.com/v2/prices/SOL-USD/spot") as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        sol_price = float(data["data"]["amount"])
        except Exception as e:
            logger.error(f"Error fetching SOL price in QR generator: {e}")
        
        amount_sol = round(amount_usd / sol_price, 4)
        
        # Solana Pay URL
        import urllib.parse
        amount_str = f"{amount_sol:.4f}".rstrip('0').rstrip('.')
        query_params = {
            "amount": amount_str,
            "reference": reference_pubkey,
            "label": "Premium Upgrade",
            "message": "Discord Upgrade"
        }
        query_string = urllib.parse.urlencode(query_params, quote_via=urllib.parse.quote)
        solana_pay_uri = f"solana:{self.receiver_address}?{query_string}"
        
        embed = discord.Embed(
            title="💸 Solana Payment",
            description=(
                f"To upgrade to premium, pay **{amount_sol} SOL** (~${amount_usd:.2f} USD).\n\n"
                f"📱 **How to Pay**\n"
                f"Open your **Phantom** or **Solflare** mobile app, tap the **QR Scanner** icon, and scan the code below.\n\n"
                f"The payment details (amount and reference ID) will be prefilled automatically. "
                f"Once you've approved the transaction, click **I Paid** below."
            ),
            color=0x9945FF
        )
        qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=250x250&data={urllib.parse.quote(solana_pay_uri)}"
        embed.set_image(url=qr_url)
        
        # Register session so verifier knows when this QR was created
        create_payment_session(reference_pubkey, str(interaction.user.id), amount_usd, months)
        
        view = PaymentVerificationView(reference_pubkey, self.receiver_address, amount_usd, months)
        
        await interaction.followup.send(
            embed=embed, 
            view=view, 
            ephemeral=True
        )


@bot.command(name="upgrade", aliases=["premium"])
async def upgrade_prefix(ctx):
    """View premium perks and upgrade your account."""
    is_prem = premium_db.is_premium(str(ctx.author.id))
    
    if is_prem:
        embed = discord.Embed(
            title="💎 My Subscription",
            description=(
                "You are currently subscribed to **MemecoinBot Premium**!\n\n"
                "You can extend or upgrade your subscription using the options below."
            ),
            color=0x00FFA3
        )
        embed.add_field(
            name="💰 Subscription Options",
            value="**$49 USD** / month OR **$300 USD** / year (Save 50%!)",
            inline=False
        )
        embed.set_footer(text="Click a plan below to proceed with Solana payment.")
        await ctx.send(embed=embed, view=UpgradeView(SOLANA_PAYMENT_ADDRESS, show_monthly=True))
        return

    embed = discord.Embed(
        title="💎 Upgrade to MemecoinBot Premium",
        description=(
            "Supercharge your trading with our next-generation due diligence and tracking tools. "
            "Lift all daily query limits and unlock high-speed real-time custom trackers."
        ),
        color=0xFFD700
    )
    embed.add_field(
        name="🚀 Premium Perks",
        value=(
            "• **Unlimited `/ca` queries** — Run contract address security scans without daily limits.\n"
            "• **Track up to 250 custom wallets** — Add and track wallets with `/addwallet`.\n"
            "• **Add X accounts for tracking** — Monitor and track X (Twitter) accounts.\n"
            "• **Alpha Trader Calls** — Get real-time calls from alpha traders across FOMO and Pump.\n"
            "• **Advanced Coin Analysis** — Deep security scans, RugCheck, and X-account checks."
        ),
        inline=False
    )
    embed.add_field(
        name="💰 Pricing Options",
        value="**$49 USD** / month OR **$300 USD** / year (Save 50%!)",
        inline=False
    )
    embed.set_footer(text="Click a plan below to proceed with Solana payment.")
    
    await ctx.send(embed=embed, view=UpgradeView(SOLANA_PAYMENT_ADDRESS, show_monthly=True))


@bot.tree.command(name="upgrade", description="View premium perks and upgrade your account")
async def upgrade_slash(interaction: discord.Interaction):
    """Slash command to view premium perks and upgrade."""
    # Defer immediately to avoid Discord's 3-second timeout while Supabase responds
    await interaction.response.defer(ephemeral=True)
    
    import asyncio
    is_prem = await asyncio.get_event_loop().run_in_executor(
        None, premium_db.is_premium, str(interaction.user.id)
    )
    
    if is_prem:
        embed = discord.Embed(
            title="💎 My Subscription",
            description=(
                "You are currently subscribed to **MemecoinBot Premium**!\n\n"
                "You can extend or upgrade your subscription using the options below."
            ),
            color=0x00FFA3
        )
        embed.add_field(
            name="💰 Subscription Options",
            value="**$49 USD** / month OR **$300 USD** / year (Save 50%!)",
            inline=False
        )
        embed.set_footer(text="Click a plan below to proceed with Solana payment.")
        await interaction.followup.send(embed=embed, view=UpgradeView(SOLANA_PAYMENT_ADDRESS, show_monthly=True), ephemeral=True)
        return

    embed = discord.Embed(
        title="💎 Upgrade to MemecoinBot Premium",
        description=(
            "Supercharge your trading with our next-generation due diligence and tracking tools. "
            "Lift all daily query limits and unlock high-speed real-time custom trackers."
        ),
        color=0xFFD700
    )
    embed.add_field(
        name="🚀 Premium Perks",
        value=(
            "• **Unlimited `/ca` queries** — Run contract address security scans without daily limits.\n"
            "• **Track up to 250 custom wallets** — Add and track wallets with `/addwallet`.\n"
            "• **Add X accounts for tracking** — Monitor and track X (Twitter) accounts.\n"
            "• **Alpha Trader Calls** — Get real-time calls from alpha traders across FOMO and Pump.\n"
            "• **Advanced Coin Analysis** — Deep security scans, RugCheck, and X-account checks."
        ),
        inline=False
    )
    embed.add_field(
        name="💰 Pricing Options",
        value="**$49 USD** / month OR **$300 USD** / year (Save 50%!)",
        inline=False
    )
    embed.set_footer(text="Click a plan below to proceed with Solana payment.")
    
    await interaction.followup.send(embed=embed, view=UpgradeView(SOLANA_PAYMENT_ADDRESS, show_monthly=True), ephemeral=True)


# ----------------- PREMIUM MANAGEMENT COMMANDS -----------------

@bot.command(name="addpremium")
async def addpremium_prefix(ctx, user: discord.User, months: int = 1):
    """[Admin Only] Grant premium subscription status to a user for a given number of months."""
    author_name = ctx.author.name.lower()
    author_id = ctx.author.id
    if author_id != 1248988195006447688 and author_name not in ("dancryptic", "dancrytic"):
        await ctx.send("❌ Only @dancryptic can assign premium to a user.")
        return

    try:
        expiry = premium_db.add_premium_user(str(user.id), user.name, months)
    except Exception as e:
        logger.error(f"Failed to grant premium to {user.id}: {e}")
        await ctx.send(f"❌ Failed to grant premium in Supabase: `{e}`")
        return
    try:
        expiry_dt = datetime.datetime.fromisoformat(expiry)
        expiry_str = expiry_dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        expiry_str = expiry
    await ctx.send(f"✅ Granted premium to **{user.name}** (`{user.id}`) for {months} month(s). Expiry: `{expiry_str}`")


@bot.tree.command(name="addpremium", description="[Admin Only] Grant premium subscription status to a user")
@app_commands.describe(user="The user to grant premium status to", months="Number of months of premium (default 1)")
async def addpremium_slash(interaction: discord.Interaction, user: discord.User, months: int = 1):
    """Slash command to grant premium subscription status to a user."""
    author_name = interaction.user.name.lower()
    author_id = interaction.user.id
    if author_id != 1248988195006447688 and author_name not in ("dancryptic", "dancrytic"):
        await interaction.response.send_message("❌ Only @dancryptic can assign premium to a user.", ephemeral=True)
        return

    try:
        expiry = premium_db.add_premium_user(str(user.id), user.name, months)
    except Exception as e:
        logger.error(f"Failed to grant premium to {user.id}: {e}")
        await interaction.response.send_message(f"❌ Failed to grant premium in Supabase: `{e}`", ephemeral=True)
        return
    try:
        expiry_dt = datetime.datetime.fromisoformat(expiry)
        expiry_str = expiry_dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        expiry_str = expiry
    await interaction.response.send_message(f"✅ Granted premium to **{user.name}** (`{user.id}`) for {months} month(s). Expiry: `{expiry_str}`", ephemeral=True)


@bot.command(name="removepremium")
async def removepremium_prefix(ctx, user: discord.User):
    """[Admin Only] Remove premium subscription status from a user."""
    author_name = ctx.author.name.lower()
    author_id = ctx.author.id
    if author_id != 1248988195006447688 and author_name not in ("dancryptic", "dancrytic"):
        await ctx.send("❌ Only @dancryptic can remove premium from a user.")
        return

    try:
        success = premium_db.remove_premium_user(str(user.id))
    except Exception as e:
        logger.error(f"Failed to remove premium for {user.id}: {e}")
        await ctx.send(f"❌ Failed to remove premium in Supabase: `{e}`")
        return
    if success:
        await ctx.send(f"✅ Removed premium status from **{user.name}** (`{user.id}`).")
    else:
        await ctx.send(f"❌ User **{user.name}** (`{user.id}`) is not registered as a premium user.")


@bot.tree.command(name="removepremium", description="[Admin Only] Remove premium subscription status from a user")
@app_commands.describe(user="The user to remove premium status from")
async def removepremium_slash(interaction: discord.Interaction, user: discord.User):
    """Slash command to remove premium subscription status from a user."""
    author_name = interaction.user.name.lower()
    author_id = interaction.user.id
    if author_id != 1248988195006447688 and author_name not in ("dancryptic", "dancrytic"):
        await interaction.response.send_message("❌ Only @dancryptic can remove premium from a user.", ephemeral=True)
        return

    try:
        success = premium_db.remove_premium_user(str(user.id))
    except Exception as e:
        logger.error(f"Failed to remove premium for {user.id}: {e}")
        await interaction.response.send_message(f"❌ Failed to remove premium in Supabase: `{e}`", ephemeral=True)
        return
    if success:
        await interaction.response.send_message(f"✅ Removed premium status from **{user.name}** (`{user.id}`).", ephemeral=True)
    else:
        await interaction.response.send_message(f"❌ User **{user.name}** (`{user.id}`) is not registered as a premium user.", ephemeral=True)


@bot.command(name="listpremium")
async def listpremium_prefix(ctx):
    """[Admin/User] List all registered premium users and their subscription status."""
    try:
        users = premium_db.get_premium_users()
    except Exception as e:
        logger.error(f"Failed to list premium users: {e}")
        await ctx.send(f"❌ Failed to load premium users from Supabase: `{e}`")
        return
    if not users:
        await ctx.send("ℹ️ No premium users found.")
        return

    embed = discord.Embed(title="💎 Premium Users Directory", color=0xFFD700) # Gold
    lines = []
    for u in users:
        status_emoji = "🟢 Active" if u["active"] else "🔴 Expired"
        try:
            exp_dt = datetime.datetime.fromisoformat(u["expire_date"])
            exp_str = exp_dt.strftime("%Y-%m-%d")
        except Exception:
            exp_str = u["expire_date"]
        lines.append(f"• **{u['username']}** (`{u['user_id']}`) - {status_emoji} (Expires: `{exp_str}`)")
        
    embed.description = "\n".join(lines)
    await ctx.send(embed=embed)


@bot.tree.command(name="listpremium", description="List all registered premium users and their subscription status")
async def listpremium_slash(interaction: discord.Interaction):
    """Slash command to list all registered premium users."""
    try:
        users = premium_db.get_premium_users()
    except Exception as e:
        logger.error(f"Failed to list premium users: {e}")
        await interaction.response.send_message(f"❌ Failed to load premium users from Supabase: `{e}`", ephemeral=True)
        return
    if not users:
        await interaction.response.send_message("ℹ️ No premium users found.", ephemeral=True)
        return

    embed = discord.Embed(title="💎 Premium Users Directory", color=0xFFD700) # Gold
    lines = []
    for u in users:
        status_emoji = "🟢 Active" if u["active"] else "🔴 Expired"
        try:
            exp_dt = datetime.datetime.fromisoformat(u["expire_date"])
            exp_str = exp_dt.strftime("%Y-%m-%d")
        except Exception:
            exp_str = u["expire_date"]
        lines.append(f"• **{u['username']}** (`{u['user_id']}`) - {status_emoji} (Expires: `{exp_str}`)")
        
    embed.description = "\n".join(lines)
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ----------------- MESSAGE LISTENER (AUTO-DETECT CA) -----------------

# ----------------- MESSAGE LISTENER (AUTO-DETECT CA) -----------------

def create_basic_token_embed(pair: Dict[str, Any], rug_report: Optional[Dict[str, Any]] = None) -> discord.Embed:
    """Creates a highly concise, mobile-optimized Embed from a DexScreener token pair dictionary."""
    base_token = pair.get("baseToken", {})
    chain_id = pair.get("chainId", "")
    ca_address = base_token.get("address", "")
    ticker = base_token.get('symbol', 'Unknown').upper()
    token_name = base_token.get('name', 'Unknown Token')

    chain_cfg = get_chain_config(chain_id)
    embed_color = chain_cfg.get("color", DEFAULT_COLOR)

    # Get logo/image
    info = pair.get("info", {})
    image_url = info.get("imageUrl") if info else None
    if not image_url:
        image_url = "https://i.imgur.com/f94QG4v.png"
    pair["resolved_image_url"] = image_url

    # Key statistics
    mcap_val = pair.get("marketCap")
    mcap_formatted = format_large_number(mcap_val)
    liquidity_val = pair.get("liquidity", {}).get("usd")
    liquidity_formatted = format_large_number(liquidity_val)
    volume_24h = pair.get("volume", {}).get("h24")
    vol_formatted = format_large_number(volume_24h)

    # Price changes
    price_changes = pair.get("priceChange", {})
    change_5m = format_percentage(price_changes.get("m5"))
    change_1h = format_percentage(price_changes.get("h1"))

    # Distribution Stats (Bundles / Snipers)
    dist = get_distribution_stats(rug_report) if chain_id == "solana" else {}
    
    # Format Bundles / Snipers cleanly
    bundler_pct = dist.get("bundler_pct")
    if bundler_pct is None:
        bundler_str = "N/A"
    else:
        clusters = dist.get("bundler_clusters", 0)
        if clusters > 0:
            bundler_str = f"{bundler_pct} ({clusters} clusters)"
        else:
            bundler_str = f"{bundler_pct}"

    sniper_pct = dist.get("sniper_pct")
    sniper_str = f"{sniper_pct}" if sniper_pct else "N/A"

    # Single-field description or compact fields for phone screen compatibility
    description = (
        f"💎 **MCap:** `{mcap_formatted}`  \u2022  💧 **Liq:** `{liquidity_formatted}`\n"
        f"📈 **24h Vol:** `{vol_formatted}`\n"
        f"⚡ **5m:** {change_5m}  \u2022  **1h:** {change_1h}\n"
    )
    
    if chain_id == "solana":
        description += (
            f"📦 **Bundles:** `{bundler_str}`\n"
            f"🎯 **Snipers:** `{sniper_str}`\n"
        )

    description += (
        f"\n📝 **CA:** `{ca_address}`\n\n"
        f"💡 *For full developer wallet track record, fresh wallets count, holder list, and security audits, run the command:* `/ca {ca_address}`"
    )

    embed = discord.Embed(
        title=f"🚀 {token_name} ({ticker})",
        description=description,
        color=embed_color
    )
    embed.set_thumbnail(url=image_url)

    return embed


class BasicTokenInfoView(discord.ui.View):
    """Stateless link buttons for basic auto-detected token info."""
    def __init__(self, pair: Dict[str, Any]):
        super().__init__(timeout=None)
        self.pair = pair
        self.add_action_buttons()

    def add_action_buttons(self):
        chain_id = self.pair.get("chainId", "")
        base_token = self.pair.get("baseToken", {})
        ca_address = base_token.get("address", "")
        pair_address = self.pair.get("pairAddress", "")
        
        if chain_id == "solana":
            self.add_item(discord.ui.Button(label="Axiom", url=f"https://axiom.trade/meme/{pair_address}?chain=sol&pulseChains=sol&trackerChains=sol,robinhood,bnb,eth", style=discord.ButtonStyle.link, emoji="🎯"))
            self.add_item(discord.ui.Button(label="Padre", url=f"https://trade.padre.gg/token/{ca_address}", style=discord.ButtonStyle.link, emoji="🦅"))
            self.add_item(discord.ui.Button(label="GMGN", url=f"https://gmgn.ai/sol/token/{ca_address}", style=discord.ButtonStyle.link, emoji="🐸"))
            self.add_item(discord.ui.Button(label="Pump.fun", url=f"https://pump.fun/coin/{ca_address}", style=discord.ButtonStyle.link, emoji="💊"))
        else:
            pair_url = self.pair.get("url")
            if pair_url:
                self.add_item(discord.ui.Button(label="View on DexScreener", url=pair_url, style=discord.ButtonStyle.link, emoji="📊"))
                
            chain_cfg = get_chain_config(chain_id)
            buy_url = chain_cfg.get("buy_url")
            if buy_url and ca_address:
                full_buy_url = f"{buy_url}{ca_address}"
                self.add_item(discord.ui.Button(label=f"Buy on {chain_cfg['name']}", url=full_buy_url, style=discord.ButtonStyle.link, emoji="💳"))
            
        info = self.pair.get("info", {})
        if info:
            websites = info.get("websites", [])
            if websites and len(websites) > 0:
                self.add_item(discord.ui.Button(label="Website", url=websites[0].get("url"), style=discord.ButtonStyle.link, emoji="🌐", row=1 if chain_id == "solana" else None))
                
            socials = info.get("socials", [])
            for social in socials[:2]:
                soc_type = social.get("type", "").lower()
                soc_url = social.get("url")
                if soc_url:
                    emoji = "🐦" if soc_type == "twitter" else ("💬" if soc_type == "telegram" else "🔗")
                    label = soc_type.capitalize()
                    self.add_item(discord.ui.Button(label=label, url=soc_url, style=discord.ButtonStyle.link, emoji=emoji, row=1 if chain_id == "solana" else None))

        image_url = self.pair.get("resolved_image_url")
        if image_url and image_url != "https://i.imgur.com/f94QG4v.png":
            self.add_item(discord.ui.Button(
                label="Image Search",
                url=f"https://lens.google.com/uploadbyurl?url={image_url}",
                style=discord.ButtonStyle.link,
                emoji="🔍",
                row=1 if chain_id == "solana" else None
            ))


@bot.event
async def on_message(message: discord.Message):
    # Ignore messages sent by the bot itself
    if message.author.bot:
        return
        
    logger.info(f"Received message from {message.author} in channel '{message.channel}': '{message.content}'")
        
    # Check if prefix command is used, if so let commands handle it
    if message.content.startswith(COMMAND_PREFIX):
        await bot.process_commands(message)
        return
        
    # Auto-detect CAs only in category 1446685586667732992
    category_id = getattr(message.channel, "category_id", None)
    if category_id and str(category_id) == "1446685586667732992":
        # Search for EVM and Solana contract addresses in message content
        evm_matches = EVM_REGEX.findall(message.content)
        sol_matches = SOL_REGEX.findall(message.content)
        
        # Combine matches, remove duplicates
        all_cas = list(set(evm_matches + sol_matches))
        
        if all_cas:
            # Limit to fetching 2 CAs at a time to prevent spam
            for ca in all_cas[:2]:
                logger.info(f"Auto-detected contract address: {ca} in message from {message.author} in category 1446685586667732992")
                try:
                    pairs = await api_client.get_token_by_ca(ca)
                    if pairs:
                        primary_pair = pairs[0]
                        rug_report = None
                        if primary_pair.get("chainId") == "solana":
                            try:
                                rug_report = await api_client.get_rugcheck_report(ca)
                            except Exception as ree:
                                logger.error(f"Error fetching RugCheck report in auto-detect: {ree}")
                        embed = create_basic_token_embed(primary_pair, rug_report)
                        view = BasicTokenInfoView(primary_pair)
                        # Reply directly to the message containing the address
                        await message.reply(embed=embed, view=view, mention_author=False)
                        logger.info(f"Successfully replied with basic info for CA: {ca}")
                except Exception as e:
                    logger.error(f"Error handling auto-detected CA {ca}: {e}")
                
    # Allow command processing for non-prefix messages containing prefix commands (unusual but good practice)
    await bot.process_commands(message)


# ----------------- PERSONAL WALLET TRACKER SYSTEM -----------------

CUSTOM_TRACKER_CHANNEL_ID = os.getenv("CUSTOM_TRACKER_CHANNEL_ID") or "1530453827759898674"
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
SOLANA_PAYMENT_ADDRESS = os.getenv("SOLANA_PAYMENT_ADDRESS") or "7XoNeTnT9fuEqTh7M4TsZ95YS2Ch7MpJgTJwXFj7o1tN"

USER_WALLETS_FILE = os.path.join(os.path.dirname(__file__), "user_wallets.json")
user_pending_alerts = {}
DYNAMIC_SUBSCRIPTION_QUEUE = asyncio.Queue()

# Master lookup map: address -> list of owner dicts
ALL_TRACKED_WALLETS = {}

def is_supabase_configured() -> bool:
    return bool(SUPABASE_URL and SUPABASE_KEY and "YOUR_SUPABASE" not in SUPABASE_URL and "YOUR_SUPABASE" not in SUPABASE_KEY)

def load_user_wallets_data() -> dict:
    if is_supabase_configured():
        try:
            headers = {
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/json"
            }
            url = f"{SUPABASE_URL.rstrip('/')}/rest/v1/user_wallets?select=*"
            import urllib.request
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=5) as response:
                if response.status == 200:
                    rows = json.loads(response.read().decode())
                    result = {}
                    for row in rows:
                        uid = str(row.get("user_id"))
                        if uid not in result:
                            result[uid] = {"thread_id": row.get("thread_id"), "wallets": []}
                        if row.get("thread_id") and not result[uid]["thread_id"]:
                            result[uid]["thread_id"] = row.get("thread_id")
                        result[uid]["wallets"].append({
                            "address": row.get("address"),
                            "name": row.get("name"),
                            "emoji": row.get("emoji", "👤"),
                            "alertsOn": row.get("alerts_on", True)
                        })
                    return result
        except Exception as e:
            logger.error(f"Supabase fetch error: {e}")
            return {}

    if os.path.exists(USER_WALLETS_FILE):
        try:
            with open(USER_WALLETS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading {USER_WALLETS_FILE}: {e}")
    return {}

def save_user_wallets_data(data: dict):
    if is_supabase_configured():
        # Sync to Supabase only
        try:
            headers = {
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates"
            }
            url = f"{SUPABASE_URL.rstrip('/')}/rest/v1/user_wallets?on_conflict=user_id,address"
            rows = []
            for uid, uinfo in data.items():
                tid = uinfo.get("thread_id")
                for w in uinfo.get("wallets", []):
                    rows.append({
                        "user_id": uid,
                        "thread_id": str(tid) if tid else None,
                        "address": w.get("address"),
                        "name": w.get("name"),
                        "emoji": w.get("emoji", "👤"),
                        "alerts_on": w.get("alertsOn", True)
                    })
            if rows:
                payload = json.dumps(rows).encode("utf-8")
                import urllib.request
                req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=5) as response:
                    pass
        except Exception as e:
            logger.error(f"Error syncing to Supabase: {e}")
        return

    # Fallback to local file backup only when Supabase is not configured
    try:
        with open(USER_WALLETS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error saving {USER_WALLETS_FILE}: {e}")

def rebuild_all_tracked_wallets():
    global ALL_TRACKED_WALLETS
    new_map = {}
    
    # 1. Load KOL wallets
    try:
        if os.path.exists("kol.js"):
            with open("kol.js", "r", encoding="utf-8") as f:
                wallets_data = json.load(f)
                for w in wallets_data:
                    addr = w.get("trackedWalletAddress")
                    if addr and w.get("alertsOn", True):
                        if addr not in new_map:
                            new_map[addr] = []
                        new_map[addr].append({
                            "type": "kol",
                            "name": w.get("name") or "KOL",
                            "emoji": w.get("emoji") or "👤"
                        })
    except Exception as e:
        logger.error(f"Error loading kol.js in rebuild: {e}")
        
    # 2. Load User custom wallets
    user_data = load_user_wallets_data()
    for uid, uinfo in user_data.items():
        thread_ids = uinfo.get("thread_ids", {})
        # If thread_ids dict doesn't exist but legacy thread_id does
        if not thread_ids and uinfo.get("thread_id"):
            thread_ids = {"legacy": uinfo.get("thread_id")}

        wallets = uinfo.get("wallets", [])
        for w in wallets:
            addr = w.get("address")
            if addr and w.get("alertsOn", True):
                if addr not in new_map:
                    new_map[addr] = []
                
                # Append a target for each server thread
                for guild_id_str, thread_id in thread_ids.items():
                    if thread_id:
                        new_map[addr].append({
                            "type": "user",
                            "user_id": uid,
                            "thread_id": thread_id,
                            "name": w.get("name") or "Custom Wallet",
                            "emoji": w.get("emoji") or "👤"
                        })
                
    ALL_TRACKED_WALLETS = new_map
    return ALL_TRACKED_WALLETS

async def get_or_create_user_thread(guild: Optional[discord.Guild], user: discord.User, user_data: dict) -> Optional[int]:
    if not guild:
        return None
        
    guild_id_str = str(guild.id)
    thread_ids = user_data.setdefault("thread_ids", {})
    
    # Check if we already have a thread for this specific guild
    t_id = thread_ids.get(guild_id_str)
    if t_id:
        try:
            thread = bot.get_channel(int(t_id)) or await bot.fetch_channel(int(t_id))
            if thread:
                return thread.id
        except Exception:
            pass
            
    # Legacy fallback: check the old single thread_id field
    legacy_id = user_data.get("thread_id")
    if legacy_id:
        try:
            thread = bot.get_channel(int(legacy_id)) or await bot.fetch_channel(int(legacy_id))
            if thread and thread.guild:
                # Save it under its appropriate guild ID
                thread_ids[str(thread.guild.id)] = thread.id
                if str(thread.guild.id) == guild_id_str:
                    return thread.id
        except Exception:
            pass

    parent_channel = None
    target_channel_id = CUSTOM_TRACKER_CHANNEL_ID or KOL_TRACKER_CHANNEL_ID
    
    # If CUSTOM_TRACKER_CHANNEL_ID is a comma-separated list, find the one belonging to this guild
    if target_channel_id:
        allowed_ids = [cid.strip() for cid in str(target_channel_id).split(",") if cid.strip()]
        for cid in allowed_ids:
            try:
                ch = bot.get_channel(int(cid)) or await bot.fetch_channel(int(cid))
                if ch and ch.guild and ch.guild.id == guild.id:
                    parent_channel = ch
                    break
            except Exception:
                pass
                
    if not parent_channel and guild:
        for ch in guild.text_channels:
            if ch.name.lower() in ["kol-tracker", "custom-trackers", "wallet-alerts", "general"]:
                parent_channel = ch
                break
        if not parent_channel and guild.text_channels:
            parent_channel = guild.text_channels[0]
            
    if not parent_channel:
        return None
        
    try:
        thread_name = f"🔔 {user.display_name}'s Tracker"
        try:
            thread = await parent_channel.create_thread(
                name=thread_name,
                type=discord.ChannelType.private_thread,
                auto_archive_duration=10080
            )
        except Exception:
            thread = await parent_channel.create_thread(
                name=thread_name,
                type=discord.ChannelType.public_thread,
                auto_archive_duration=10080
            )
            
        await thread.add_user(user)
        thread_ids[guild_id_str] = thread.id
        user_data["thread_id"] = thread.id  # backward compatibility fallback
        return thread.id
    except Exception as e:
        logger.error(f"Error creating private thread for user {user.id} in guild {guild.id}: {e}")
        return None

async def send_user_alert_message(thread_id: int, message_text: str, embed: Optional[discord.Embed] = None, view: Optional[discord.ui.View] = None):
    try:
        thread = bot.get_channel(thread_id) or await bot.fetch_channel(thread_id)
        if thread:
            await thread.send(content=message_text, embed=embed, view=view)
    except Exception as e:
        logger.error(f"Failed to send user alert to thread {thread_id}: {e}")

async def queue_user_alert(user_id: str, thread_id: int, emoji: str, name: str, wallet_address: str, ticker: str, mint: str,
                           sol_spent: float, market_cap_str: str):
    key = f"{user_id}:{mint}"
    if key in user_pending_alerts:
        user_pending_alerts[key]["buys"].append({
            "name": name,
            "emoji": emoji,
            "wallet_address": wallet_address,
            "sol_spent": sol_spent
        })
        user_pending_alerts[key]["sol_spent"] += sol_spent
        if market_cap_str != "N/A":
            user_pending_alerts[key]["market_cap"] = market_cap_str
    else:
        user_pending_alerts[key] = {
            "user_id": user_id,
            "thread_id": thread_id,
            "buys": [{
                "name": name,
                "emoji": emoji,
                "wallet_address": wallet_address,
                "sol_spent": sol_spent
            }],
            "ticker": ticker,
            "sol_spent": sol_spent,
            "market_cap": market_cap_str,
            "mint": mint
        }
        asyncio.create_task(flush_user_alert(key))

async def flush_user_alert(key: str):
    await asyncio.sleep(5.0)
    alert = user_pending_alerts.pop(key, None)
    if not alert or not alert.get("thread_id"):
        return
        
    ticker = alert["ticker"]
    sol_spent = alert["sol_spent"]
    market_cap_str = alert["market_cap"]
    thread_id = alert["thread_id"]
    mint = alert["mint"]
    buys = alert["buys"]
    
    token_name = "Unknown Token"
    pair_address = mint
    cached = TOKEN_CACHE.get(mint)
    if cached:
        token_name = cached.get("name") or token_name
        pair_address = cached.get("pair_address") or pair_address
        
    names_list = sorted(list({b["name"] for b in buys}))
    emojis_list = sorted(list({b["emoji"] for b in buys}))
    primary_emoji = emojis_list[0] if emojis_list else "👤"
    if len(names_list) > 1:
        names_str = ", ".join(names_list[:-1]) + f" & {names_list[-1]}"
    else:
        names_str = names_list[0]
        
    notification_text = f"{primary_emoji} {names_str} bought ${ticker} for {sol_spent:.2f} SOL at {market_cap_str} MC"
    
    embed = create_tracker_alert_embed(alert, token_name)
    view = UserAlertView(mint=mint, ticker=ticker, pair_address=pair_address)
    
    await send_user_alert_message(thread_id, notification_text, embed=embed, view=view)


# ----------------- KOL TRACKER SYSTEM -----------------

# Comma-separated list — alerts are broadcast to ALL configured KOL channels
KOL_TRACKER_CHANNEL_ID = os.getenv("KOL_TRACKER_CHANNEL_ID", "")
KOL_TRACKER_CHANNEL_IDS = [cid.strip() for cid in KOL_TRACKER_CHANNEL_ID.split(",") if cid.strip()]

# Specific channel where /ca is allowed (old server)
CA_CHANNEL_ID = os.getenv("CA_CHANNEL_ID", "").strip()

# Comma-separated category IDs — commands are ONLY allowed inside these categories (new server)
_raw_cat = os.getenv("ALLOWED_CATEGORY_IDS", "").strip()
ALLOWED_CATEGORY_IDS = set(c.strip() for c in _raw_cat.split(",") if c.strip())
# Ensure default category IDs are always allowed
ALLOWED_CATEGORY_IDS.add("1531780360495435909")
ALLOWED_CATEGORY_IDS.add("1446685586667732992")

def is_channel_allowed(channel) -> bool:
    """
    Central gate for all bot commands.

    Logic (any match = allowed):
      1. If the channel is the specific CA_CHANNEL_ID  → allowed
      2. If the channel's parent category is in ALLOWED_CATEGORY_IDS → allowed
      3. If NEITHER CA_CHANNEL_ID nor ALLOWED_CATEGORY_IDS is configured → allow all
         (backward compatible — old server without strict restrictions)

    This means:
      - Old server: /ca only works in channel 1446686294401880175
      - New server: every command works inside category 1531780360495435909
    """
    # No restrictions configured at all → open
    if not CA_CHANNEL_ID and not ALLOWED_CATEGORY_IDS:
        return True

    # Match specific channel (old server /ca channel)
    if CA_CHANNEL_ID and str(getattr(channel, "id", "")) == CA_CHANNEL_ID:
        return True

    # Match parent category (new server category restriction)
    if ALLOWED_CATEGORY_IDS:
        cat_id = str(getattr(channel, "category_id", "") or "")
        if cat_id in ALLOWED_CATEGORY_IDS:
            return True

    return False

# In-memory alert aggregator
pending_alerts = {}

# In-memory token metadata cache to prevent hitting DexScreener rate limits
# Key: mint address (str) -> Value: (ticker (str), market_cap_str (str), timestamp (float))
TOKEN_CACHE = {}
TOKEN_CACHE_TTL = 300.0  # 5 minutes cache lifetime

# Semaphore to control concurrent getTransaction RPC requests
RPC_SEMAPHORE = asyncio.Semaphore(5)

# Known DEX Programs on Solana for filtering
DEX_PROGRAMS = {
    "675k1h2AYysA3Bt2hrrMGgA3qvwGxsWkFQ8y3E6ypJhX",  # Raydium AMM v4
    "5qu5cjv2ip563bvphwndvxgusfs3jg8eqtqwnrmzkuyj",  # Raydium Route Swap
    "CAMMCzo5YL8w4VFFnm2HmgSgxnJefdOPnVH45Ldg775Y",  # Raydium CLMM
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",  # Pump.fun (corrected Program ID)
    "JUP6Lgp5gZXrJZ1q9QUNPp2sK5oGGmrrJjKUZZwi84zp",  # Jupiter v6
    "L2b1ZzSF7qR42jqgZhbvB3vCDkpqq689xKpa1Vra681",  # Meteora DLMM
    "Eo7WjKq67rjJQSvokg81txo23Yh6xz3mgqMTctttrqyc",  # Meteora Pools
    "MoonCV1mW1A1Xf22AE2jRbmT7x9xzfRnWN5b1sY271e",  # Moonshot
    "whirLMiZXZioTu2aD58eyib9wst3qvvCaBtPPQ7yZG7",  # Orca Whirlpool
    "PhoeNiX81Zzu13wj2egmUnPYY3x5CcK59Tbx8TR93OP",  # Phoenix
}

DEX_KEYWORDS = ["instruction: swap", "instruction: buy", "instruction: route", "swap"]

def should_process_logs(logs: List[str]) -> bool:
    """Pre-filters transaction logs. Returns True if a swap/buy or DEX activity is detected."""
    if not logs:
        return False
    
    for log in logs:
        log_lower = log.lower()
        if "invoke" in log_lower:
            for prog in DEX_PROGRAMS:
                if prog in log:
                    return True
        for kw in DEX_KEYWORDS:
            if kw in log_lower:
                return True
                
    return False

async def send_discord_message(message_text: str, embed: Optional[discord.Embed] = None, view: Optional[discord.ui.View] = None):
    """Sends a KOL alert to ALL configured KOL tracker channels."""
    sent_to_any = False
    channels_to_try = list(KOL_TRACKER_CHANNEL_IDS)  # from env

    # If no IDs configured, fall back to searching by channel name
    if not channels_to_try:
        for guild in bot.guilds:
            for ch in guild.text_channels:
                if ch.name.lower() in ["kol-tracker", "kol-alerts"]:
                    try:
                        await ch.send(content=message_text, embed=embed, view=view)
                        sent_to_any = True
                    except Exception as e:
                        logger.error(f"Failed to send KOL alert to #{ch.name}: {e}")
        if not sent_to_any:
            logger.warning("KOL Tracker channel not found. Set KOL_TRACKER_CHANNEL_ID in .env.")
        return

    for cid in channels_to_try:
        try:
            channel = bot.get_channel(int(cid))
            if not channel:
                channel = await bot.fetch_channel(int(cid))
            await channel.send(content=message_text, embed=embed, view=view)
            sent_to_any = True
        except Exception as e:
            logger.error(f"Failed to send KOL alert to channel {cid}: {e}")

    if not sent_to_any:
        logger.warning(f"Could not send KOL alert to any configured channel: {KOL_TRACKER_CHANNEL_IDS}")

async def queue_kol_alert(emoji: str, name: str, wallet_address: str, ticker: str, mint: str, sol_spent: float, market_cap_str: str,
                           liquidity_str: str, bonding_progress: Optional[float], insiders_pct: Optional[float],
                           dev_holdings_pct: Optional[float], is_pump: bool):
    if mint in pending_alerts:
        pending_alerts[mint]["buys"].append({
            "name": name,
            "emoji": emoji,
            "wallet_address": wallet_address,
            "sol_spent": sol_spent
        })
        pending_alerts[mint]["sol_spent"] += sol_spent
        if market_cap_str != "N/A":
            pending_alerts[mint]["market_cap"] = market_cap_str
        if liquidity_str != "N/A":
            pending_alerts[mint]["liquidity"] = liquidity_str
        if bonding_progress is not None:
            pending_alerts[mint]["bonding_progress"] = bonding_progress
        if insiders_pct is not None:
            pending_alerts[mint]["insiders_pct"] = insiders_pct
        if dev_holdings_pct is not None:
            pending_alerts[mint]["dev_holdings_pct"] = dev_holdings_pct
    else:
        pending_alerts[mint] = {
            "mint": mint,
            "buys": [{
                "name": name,
                "emoji": emoji,
                "wallet_address": wallet_address,
                "sol_spent": sol_spent
            }],
            "ticker": ticker,
            "sol_spent": sol_spent,
            "market_cap": market_cap_str,
            "liquidity": liquidity_str,
            "bonding_progress": bonding_progress,
            "insiders_pct": insiders_pct,
            "dev_holdings_pct": dev_holdings_pct,
            "is_pump": is_pump
        }
        asyncio.create_task(flush_kol_alert(mint))

async def flush_kol_alert(mint: str):
    await asyncio.sleep(5.0)
    alert = pending_alerts.pop(mint, None)
    if not alert:
        return
        
    ticker = alert["ticker"]
    sol_spent = alert["sol_spent"]
    market_cap_str = alert["market_cap"]
    buys = alert["buys"]
    
    token_name = "Unknown Token"
    pair_address = mint
    cached = TOKEN_CACHE.get(mint)
    if cached:
        token_name = cached.get("name") or token_name
        pair_address = cached.get("pair_address") or pair_address
        
    names_list = sorted(list({b["name"] for b in buys}))
    emojis_list = sorted(list({b["emoji"] for b in buys}))
    primary_emoji = emojis_list[0] if emojis_list else "👤"
    if len(names_list) > 1:
        names_str = ", ".join(names_list[:-1]) + f" & {names_list[-1]}"
    else:
        names_str = names_list[0]
        
    notification_text = f"{primary_emoji} {names_str} bought ${ticker} for {sol_spent:.2f} SOL at {market_cap_str} MC"
    
    embed = create_tracker_alert_embed(alert, token_name)
    view = UserAlertView(mint=mint, ticker=ticker, pair_address=pair_address)
    
    await send_discord_message(notification_text, embed=embed, view=view)

async def process_kol_signature(sig: str, tracked_wallets: dict):
    async with RPC_SEMAPHORE:
        logger.debug(f"Processing tracked wallet transaction: {sig}")
        rpc_url, _ = get_solana_rpc_urls()
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTransaction",
            "params": [
                sig,
                {
                    "encoding": "jsonParsed",
                    "maxSupportedTransactionVersion": 0,
                    "commitment": "confirmed"
                }
            ]
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(rpc_url, json=payload, timeout=10) as response:
                    if response.status != 200:
                        logger.warning(f"RPC HTTP error fetching {sig}: {response.status}")
                        return
                    tx_data = await response.json()
                    
                    result = tx_data.get("result")
                    if not result:
                        # Retry once after 1.5 seconds due to node replication delay
                        await asyncio.sleep(1.5)
                        async with session.post(rpc_url, json=payload, timeout=10) as retry_response:
                            if retry_response.status == 200:
                                tx_data = await retry_response.json()
                                result = tx_data.get("result")
                                
                    if not result:
                        logger.warning(f"No transaction details returned for {sig} after retry (might be slot expiration or pruning)")
                        return
                        
                    meta = result.get("meta", {})
                    if not meta or meta.get("err"):
                        logger.debug(f"Transaction {sig} failed or has no metadata. Skipping.")
                        return
                        
                    transaction = result.get("transaction", {})
                    account_keys = transaction.get("message", {}).get("accountKeys", [])
                    
                    signer_pubkey = None
                    signer_idx = -1
                    for idx, acc in enumerate(account_keys):
                        pubkey = acc.get("pubkey") if isinstance(acc, dict) else acc
                        is_signer = acc.get("signer") if isinstance(acc, dict) else (idx == 0)
                        if is_signer:
                            signer_pubkey = pubkey
                            signer_idx = idx
                            break
                            
                    if signer_idx == -1:
                        return
                        
                    pre_balances = {}
                    for b in meta.get("preTokenBalances", []):
                        owner = b.get("owner")
                        mint = b.get("mint")
                        ui_amount = b.get("uiTokenAmount", {}).get("uiAmount", 0) or 0
                        if owner and mint:
                            pre_balances[(owner, mint)] = ui_amount
                            
                    for b in meta.get("postTokenBalances", []):
                        owner = b.get("owner")
                        mint = b.get("mint")
                        post_amount = b.get("uiTokenAmount", {}).get("uiAmount", 0) or 0
                        
                        if owner and mint and owner in ALL_TRACKED_WALLETS:
                            if mint == "So11111111111111111111111111111111111111112":
                                continue
                                
                            pre_amount = pre_balances.get((owner, mint), 0)
                            if post_amount > pre_amount:
                                amount_bought = post_amount - pre_amount
                                
                                pre_sol = meta.get("preBalances", [])[signer_idx]
                                post_sol = meta.get("postBalances", [])[signer_idx]
                                sol_spent = (pre_sol - post_sol) / 1e9
                                
                                fee = meta.get("fee", 0) / 1e9
                                sol_spent = max(0.0, sol_spent - fee)
                                
                                targets = ALL_TRACKED_WALLETS[owner]
                                first_name = targets[0].get("name") or f"Wallet ({owner[:4]}...{owner[-4:]})"
                                
                                if sol_spent < 0.005:
                                    logger.debug(f"Skipped transfer/airdrop: {first_name} received {amount_bought:.2f} of {mint} (spent: {sol_spent:.5f} SOL)")
                                    continue
                                    
                                logger.info(f"Wallet Buy Detected! {first_name} bought {amount_bought:.2f} of {mint} for {sol_spent:.4f} SOL")
                                
                                # Use cached token details if available
                                now = time.time()
                                cached = TOKEN_CACHE.get(mint)
                                if cached and (now - cached.get("timestamp", 0) < TOKEN_CACHE_TTL):
                                    ticker = cached["ticker"]
                                    market_cap_str = cached["market_cap"]
                                    liquidity_str = cached["liquidity"]
                                    bonding_progress = cached["bonding_progress"]
                                    insiders_pct = cached["insiders_pct"]
                                    dev_holdings_pct = cached["dev_holdings_pct"]
                                    is_pump = cached["is_pump"]
                                    token_name = cached.get("name") or ticker or "Unknown Token"
                                    pair_address = cached.get("pair_address") or mint
                                    logger.debug(f"Using cached metadata for token {mint}: {ticker}")
                                else:
                                    ticker = None
                                    token_name = "Unknown Token"
                                    pair_address = mint
                                    market_cap_str = "N/A"
                                    liquidity_str = "N/A"
                                    bonding_progress = None
                                    insiders_pct = None
                                    dev_holdings_pct = None
                                    is_pump = mint.endswith("pump")
                                    
                                    # Try pump.fun API FIRST for pump tokens — it's always faster and more reliable
                                    if is_pump:
                                        try:
                                            pump_url = f"https://frontend-api-v3.pump.fun/coins/{mint}"
                                            pump_headers = {
                                                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                                            }
                                            async with session.get(pump_url, headers=pump_headers, timeout=5) as resp:
                                                if resp.status == 200:
                                                    coin_data = await resp.json()
                                                    if coin_data:
                                                        ticker = coin_data.get("symbol") or coin_data.get("name")
                                                        token_name = coin_data.get("name") or ticker
                                                        
                                                        usd_mc = coin_data.get("usd_market_cap")
                                                        if usd_mc:
                                                            market_cap_str = format_large_number(usd_mc)
                                                        
                                                        complete = coin_data.get("complete", False)
                                                        if complete:
                                                            bonding_progress = 100.0
                                                        else:
                                                            real_sol = coin_data.get("real_sol_reserves", 0)
                                                            sol_val = real_sol / 1e9 if real_sol > 1000 else real_sol
                                                            bonding_progress = min(100.0, (sol_val / 85.0) * 100.0)
                                        except Exception as e:
                                            logger.error(f"Error fetching pump.fun data for {mint}: {e}")
                                    
                                    # Fallback to DexScreener if pump.fun didn't give us data
                                    if not ticker:
                                        try:
                                            pairs = await api_client.get_token_by_ca(mint)
                                            if pairs:
                                                primary_pair = pairs[0]
                                                base_token = primary_pair.get("baseToken", {})
                                                ticker = base_token.get("symbol")
                                                token_name = base_token.get("name") or ticker
                                                pair_address = primary_pair.get("pairAddress", mint)
                                                
                                                mcap = primary_pair.get("marketCap")
                                                if mcap:
                                                    market_cap_str = format_large_number(mcap)
                                                    
                                                liq = primary_pair.get("liquidity", {}).get("usd")
                                                if liq is not None:
                                                    liquidity_str = format_large_number(liq)
                                                    
                                                if not is_pump:
                                                    is_pump = primary_pair.get("dexId") == "pumpfun"
                                        except Exception as e:
                                            logger.error(f"Error fetching DexScreener info for KOL alert: {e}")
                                    
                                    # If we STILL don't have a ticker, skip this alert entirely
                                    if not ticker:
                                        logger.warning(f"Skipping alert for {mint}: could not resolve token ticker from any source")
                                        continue
                                        
                                    # Cache metadata
                                    TOKEN_CACHE[mint] = {
                                        "ticker": ticker,
                                        "name": token_name,
                                        "pair_address": pair_address,
                                        "market_cap": market_cap_str,
                                        "liquidity": liquidity_str,
                                        "bonding_progress": bonding_progress,
                                        "insiders_pct": insiders_pct,
                                        "dev_holdings_pct": dev_holdings_pct,
                                        "is_pump": is_pump,
                                        "timestamp": now
                                    }
                                    
                                # Dispatch alerts to all targets registered for this wallet
                                for target in targets:
                                    t_type = target.get("type")
                                    t_name = target.get("name") or "Wallet"
                                    t_emoji = target.get("emoji") or "👤"
                                    
                                    if t_type == "kol":
                                        await queue_kol_alert(
                                            t_emoji,
                                            t_name,
                                            owner,
                                            ticker,
                                            mint,
                                            sol_spent,
                                            market_cap_str,
                                            liquidity_str,
                                            bonding_progress,
                                            insiders_pct,
                                            dev_holdings_pct,
                                            is_pump
                                        )
                                    elif t_type == "user":
                                        u_id = target.get("user_id")
                                        t_id = target.get("thread_id")
                                        if u_id and t_id:
                                            await queue_user_alert(
                                                u_id,
                                                t_id,
                                                t_emoji,
                                                t_name,
                                                owner,
                                                ticker,
                                                mint,
                                                sol_spent,
                                                market_cap_str
                                            )
                                
        except Exception as e:
            logger.error(f"Error processing KOL transaction signature {sig}: {e}")

async def kol_websocket_worker(worker_id: int, wallets_chunk: list):
    logger.info(f"[KOL Worker {worker_id}] Starting for {len(wallets_chunk)} wallets...")
    subscribed_addrs = set(wallets_chunk)
    
    while True:
        uris = get_solana_wss_urls()
        connected = False
        
        for uri in uris:
            masked_uri = re.sub(r"api-key=[^&]+", "api-key=****", uri)
            try:
                logger.info(f"[KOL Worker {worker_id}] Connecting to {masked_uri}...")
                async with websockets.connect(uri) as websocket:
                    connected = True
                    logger.info(f"[KOL Worker {worker_id}] Connected to Solana WebSocket.")
                    
                    req_id = 1
                    for addr in list(subscribed_addrs):
                        req = {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "method": "logsSubscribe",
                            "params": [
                                {"mentions": [addr]},
                                {"commitment": "confirmed"}
                            ]
                        }
                        req_id += 1
                        await websocket.send(json.dumps(req))
                        await websocket.recv()
                        
                    logger.info(f"[KOL Worker {worker_id}] Subscribed to all {len(subscribed_addrs)} wallets.")
                    
                    while True:
                        # Drain dynamic subscription queue for new user-added wallets
                        while not DYNAMIC_SUBSCRIPTION_QUEUE.empty():
                            try:
                                new_addr = DYNAMIC_SUBSCRIPTION_QUEUE.get_nowait()
                                if new_addr and new_addr not in subscribed_addrs:
                                    subscribed_addrs.add(new_addr)
                                    req = {
                                        "jsonrpc": "2.0",
                                        "id": req_id,
                                        "method": "logsSubscribe",
                                        "params": [
                                            {"mentions": [new_addr]},
                                            {"commitment": "confirmed"}
                                        ]
                                    }
                                    req_id += 1
                                    await websocket.send(json.dumps(req))
                                    await websocket.recv()
                                    logger.info(f"[KOL Worker {worker_id}] Dynamically subscribed to new wallet: {new_addr}")
                            except asyncio.QueueEmpty:
                                break

                        try:
                            msg = await asyncio.wait_for(websocket.recv(), timeout=1.0)
                        except asyncio.TimeoutError:
                            continue

                        data = json.loads(msg)
                        if data.get("method") == "logsNotification":
                            result = data.get("params", {}).get("result", {})
                            value = result.get("value", {})
                            if value.get("err") is None:
                                sig = value.get("signature")
                                logs = value.get("logs", [])
                                # Pre-filter logs
                                if should_process_logs(logs):
                                    asyncio.create_task(process_kol_signature(sig, ALL_TRACKED_WALLETS))
                                    
            except websockets.exceptions.ConnectionClosed:
                logger.warning(f"[KOL Worker {worker_id}] Connection closed. Retrying/switching URLs...")
                break  # break inner loop to try next URI / retry
            except Exception as e:
                # Mask key in error logging
                masked_err = re.sub(r"api-key=[^&]+", "api-key=****", str(e))
                logger.error(f"[KOL Worker {worker_id}] Connection error using {masked_uri}: {masked_err}")
                await asyncio.sleep(1.0)
                
        if not connected:
            logger.warning(f"[KOL Worker {worker_id}] All WebSocket connection attempts failed. Retrying in 5 seconds...")
            await asyncio.sleep(5.0)

async def start_kol_tracker():
    logger.info("Starting Wallet Tracker background task...")
    
    rebuild_all_tracked_wallets()
    if not ALL_TRACKED_WALLETS:
        logger.warning("No wallets loaded for tracking.")
        return
        
    logger.info(f"Loaded {len(ALL_TRACKED_WALLETS)} total unique wallets for tracking (KOLs + Personal).")
    
    addrs = list(ALL_TRACKED_WALLETS.keys())
    chunk_size = 250  # Keep all wallets in 1 WebSocket connection (Solana supports ~256 subs/conn)
    chunks = [addrs[i:i + chunk_size] for i in range(0, len(addrs), chunk_size)]
    
    tasks = []
    for idx, chunk in enumerate(chunks):
        task = asyncio.create_task(kol_websocket_worker(idx + 1, chunk))
        tasks.append(task)
        await asyncio.sleep(2.0)
        
    await asyncio.gather(*tasks)


# ----------------- RUN ENTRYPOINT -----------------

if __name__ == "__main__":
    if not DISCORD_TOKEN or DISCORD_TOKEN == "YOUR_DISCORD_TOKEN_HERE":
        print("ERROR: DISCORD_TOKEN is not set in the .env file.")
        print("Please edit .env and replace 'YOUR_DISCORD_TOKEN_HERE' with your actual bot token.")
    else:
        logger.info("Starting Discord bot...")
        bot.run(DISCORD_TOKEN)
