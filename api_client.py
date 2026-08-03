import time
import os
import re
import json
import asyncio
import datetime
import unicodedata
from difflib import SequenceMatcher
import aiohttp
from typing import Dict, List, Any, Optional
from config import logger, GECKOTERMINAL_API_URL, DEXSCREENER_API_URL

HEADERS = {
    "Accept": "application/json;version=20230203",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

async def fetch_json(url: str, custom_headers: Optional[Dict[str, str]] = None) -> Optional[Dict[str, Any]]:
    """Helper to perform asynchronous GET requests and return parsed JSON."""
    req_headers = custom_headers if custom_headers is not None else HEADERS
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=req_headers, timeout=10) as response:
                if response.status == 200:
                    return await response.json()
                elif response.status in (429, 502, 503, 504):
                    logger.warning(f"Upstream service temporarily busy for URL: {url} (Status {response.status})")
                    return None
                else:
                    logger.error(f"Error fetching {url}: Status {response.status}")
                    return None
    except Exception as e:
        logger.warning(f"Request timeout/exception for {url}: {e}")
        return None

async def get_token_by_ca(contract_address: str) -> Optional[List[Dict[str, Any]]]:
    """
    Fetches token pairs from DexScreener matching the contract address.
    Returns a sorted list of pairs (highest liquidity first).
    """
    # Append cache-busting timestamp to bypass DexScreener CDN cache on demand
    url = f"{DEXSCREENER_API_URL}/tokens/{contract_address}?t={int(time.time() * 1000)}"
    data = await fetch_json(url)
    if not data or "pairs" not in data or not data["pairs"]:
        return None
    
    # Sort pairs by liquidity USD descending, fallback to volume
    pairs = data["pairs"]
    def get_sort_key(p):
        liq = p.get("liquidity", {}).get("usd")
        if liq is not None:
            return float(liq)
        vol = p.get("volume", {}).get("h24")
        if vol is not None:
            return float(vol)
        return 0.0

    pairs.sort(key=get_sort_key, reverse=True)
    return pairs

async def get_trending_pools(network: Optional[str] = None) -> Optional[List[Dict[str, Any]]]:
    """
    Fetches trending pools from GeckoTerminal.
    If network is specified (e.g. 'solana', 'base', 'bsc', 'robinhood'), fetches for that network.
    Otherwise, fetches global trending pools.
    """
    if network:
        # Normalize network name
        network_lower = network.lower()
        # GeckoTerminal mapping
        network_map = {
            "sol": "solana",
            "bnb": "bsc",
            "bsc": "bsc",
            "base": "base",
            "robinhood": "robinhood",
            "eth": "eth",
            "ethereum": "eth",
            "polygon": "polygon_pos",
            "arbitrum": "arbitrum",
            "optimism": "optimism",
            "avalanche": "avax"
        }
        gt_network = network_map.get(network_lower, network_lower)
        url = f"{GECKOTERMINAL_API_URL}/networks/{gt_network}/trending_pools?include=base_token,quote_token,dex,network&page=1"
    else:
        url = f"{GECKOTERMINAL_API_URL}/networks/trending_pools?include=base_token,quote_token,dex,network&page=1"
        
    data = await fetch_json(url)
    if not data or "data" not in data:
        return None
        
    pools = data["data"]
    included = data.get("included", [])
    
    # Map token and network details from 'included' for easier parsing
    tokens_by_id = {}
    networks_by_id = {}
    dexes_by_id = {}
    
    for item in included:
        item_type = item.get("type")
        item_id = item.get("id")
        if item_type == "token":
            tokens_by_id[item_id] = item.get("attributes", {})
        elif item_type == "network":
            networks_by_id[item_id] = item.get("attributes", {})
        elif item_type == "dex":
            dexes_by_id[item_id] = item.get("attributes", {})
            
    parsed_pools = []
    for pool in pools:
        attributes = pool.get("attributes", {})
        pool_id = pool.get("id")
        
        # Relationships
        relationships = pool.get("relationships", {})
        base_token_id = relationships.get("base_token", {}).get("data", {}).get("id")
        network_id = relationships.get("network", {}).get("data", {}).get("id")
        dex_id = relationships.get("dex", {}).get("data", {}).get("id")
        
        base_token = tokens_by_id.get(base_token_id, {})
        network_info = networks_by_id.get(network_id, {})
        dex_info = dexes_by_id.get(dex_id, {})
        
        parsed_pools.append({
            "pool_id": pool_id,
            "pool_name": attributes.get("name"),
            "pool_address": attributes.get("address"),
            "price_usd": attributes.get("base_token_price_usd"),
            "fdv_usd": attributes.get("fdv_usd"),
            "market_cap_usd": attributes.get("market_cap_usd"),
            "liquidity_usd": attributes.get("reserve_in_usd"),
            "price_change": attributes.get("price_change_percentage", {}),
            "volume_24h": attributes.get("volume_usd", {}).get("h24"),
            "base_token_name": base_token.get("name"),
            "base_token_symbol": base_token.get("symbol"),
            "base_token_address": base_token.get("address"),
            "base_token_image": base_token.get("image_url"),
            "network_id": network_id,
            "network_name": network_info.get("name", network_id),
            "dex_id": dex_id,
            "dex_name": dex_info.get("name", dex_id)
        })
        
    return parsed_pools


async def get_rugcheck_report(mint_address: str) -> Optional[Dict[str, Any]]:
    """Fetches the token audit report from Rugcheck.xyz on Solana."""
    # Append cache-busting timestamp to bypass RugCheck CDN cache on demand
    url = f"https://api.rugcheck.xyz/v1/tokens/{mint_address}/report?t={int(time.time() * 1000)}"
    custom_headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    return await fetch_json(url, custom_headers=custom_headers)


async def get_sol_balance(wallet_address: str, rpc_url: str = "https://api.mainnet-beta.solana.com") -> float:
    """Fetches the SOL balance of a wallet via Solana JSON-RPC."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getBalance",
        "params": [wallet_address]
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(rpc_url, json=payload, timeout=aiohttp.ClientTimeout(total=5)) as r:
                if r.status == 200:
                    data = await r.json()
                    lamports = data.get("result", {}).get("value", 0)
                    return lamports / 1e9
    except Exception as e:
        logger.error(f"RPC getBalance error for {wallet_address}: {e}")
    return 0.0


EVM_RPCS = {
    "ethereum": "https://eth.llamarpc.com",
    "base": "https://mainnet.base.org",
    "bsc": "https://bsc-dataseed1.binance.org",
    "arbitrum": "https://arb1.arbitrum.io/rpc",
}

async def get_evm_balance(wallet_address: str) -> Dict[str, float]:
    """Fetches the native token balance across multiple EVM chains via public RPCs."""
    results: Dict[str, float] = {}
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_getBalance",
        "params": [wallet_address, "latest"]
    }
    headers = {"Content-Type": "application/json"}
    try:
        async with aiohttp.ClientSession() as session:
            for chain_name, rpc_url in EVM_RPCS.items():
                try:
                    async with session.post(rpc_url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as r:
                        if r.status == 200:
                            data = await r.json()
                            hex_val = data.get("result", "0x0")
                            results[chain_name] = int(hex_val, 16) / 1e18
                except Exception:
                    results[chain_name] = 0.0
    except Exception as e:
        logger.error(f"EVM getBalance error for {wallet_address}: {e}")
    return results


async def get_dev_info(creator_address: str) -> Dict[str, Any]:
    """
    Fetches all coins created by a dev on pump.fun.
    Returns a dict with:
      - total_coins: int
      - migrated_coins: int  (coins where complete=True, i.e. graduated to Raydium)
      - twitter: str | None  (from the first coin found with a twitter link)
    """
    result: Dict[str, Any] = {"total_coins": 0, "migrated_coins": 0, "twitter": None}
    if not creator_address:
        return result

    _headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    try:
        url = (
            f"https://frontend-api-v3.pump.fun/coins"
            f"?offset=0&limit=50&sort=created_timestamp&order=DESC&includeNsfw=true&creator={creator_address}"
        )
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=_headers, timeout=aiohttp.ClientTimeout(total=6)) as r:
                if r.status == 200:
                    coins = await r.json()
                    if isinstance(coins, list):
                        result["total_coins"] = len(coins)
                        result["migrated_coins"] = sum(1 for c in coins if c.get("complete"))
                        # Grab first non-empty twitter link from any of the dev's coins
                        for c in coins:
                            tw = c.get("twitter")
                            if tw and str(tw).strip():
                                result["twitter"] = str(tw).strip()
                                break
    except Exception as e:
        logger.error(f"get_dev_info error for creator {creator_address}: {e}")
    return result


async def get_solanatracker_bundlers(mint_address: str, api_key: str) -> Optional[Dict[str, Any]]:
    """
    Fetches bundler wallet details from Solana Tracker API.
    Requires x-api-key.
    """
    url = f"https://data.solanatracker.io/tokens/{mint_address}/bundlers"
    headers = {
        "Accept": "application/json",
        "x-api-key": api_key,
        "User-Agent": "Mozilla/5.0"
    }
    return await fetch_json(url, custom_headers=headers)


async def get_solanatracker_holders(mint_address: str, api_key: str, limit: int = 100) -> Optional[Dict[str, Any]]:
    """
    Fetches paginated holders list from Solana Tracker API.
    Requires x-api-key.
    """
    url = f"https://data.solanatracker.io/tokens/{mint_address}/holders/paginated?limit={limit}"
    headers = {
        "Accept": "application/json",
        "x-api-key": api_key,
        "User-Agent": "Mozilla/5.0"
    }
    return await fetch_json(url, custom_headers=headers)


async def get_dex_paid_orders(chain_id: str, token_address: str) -> Dict[str, Any]:
    """
    Checks DexScreener paid orders for a token.
    Returns dict with:
      - has_paid: bool (any approved order exists)
      - order_types: list of approved order types (e.g. tokenProfile, communityTakeover)
      - boost_active: int (number of active boosts, from pair data)
    Free endpoint, no API key required. Rate limit: 60/min.
    """
    result = {"has_paid": False, "order_types": [], "boost_active": 0}
    url = f"https://api.dexscreener.com/orders/v1/{chain_id}/{token_address}"
    try:
        data = await fetch_json(url)
        if data:
            orders = []
            boosts = []
            if isinstance(data, list):
                orders = data
            elif isinstance(data, dict):
                orders = data.get("orders", [])
                boosts = data.get("boosts", [])
            
            approved = [o for o in orders if o.get("status") == "approved"]
            if approved:
                result["has_paid"] = True
                result["order_types"] = list({o.get("type", "unknown") for o in approved})
            
            if boosts:
                result["boost_active"] = len(boosts)
    except Exception as e:
        logger.error(f"Error fetching DEX paid orders for {token_address}: {e}")
    return result


async def get_fresh_wallets_count(mint_address: str, rpc_url: str, market_cap: float = 0, total_supply: float = 0) -> Dict[str, Any]:
    """
    Counts fresh/new wallets among token holders via Helius RPC.
    A wallet is considered 'fresh' if its native SOL balance is < 0.1 SOL,
    which typically indicates a burner/throwaway wallet created just to buy.
    
    Returns dict with:
      - fresh_count: int (number of fresh wallets found)
      - total_sampled: int (total wallets sampled)
      - fresh_pct: float (percentage)
    """
    result = {"fresh_count": 0, "total_sampled": 0, "fresh_pct": 0.0}
    if not mint_address or "helius-rpc.com" not in rpc_url:
        return result
    
    try:
        # Step 1: Get token account holders
        payload = {
            "jsonrpc": "2.0",
            "id": "fresh-holders",
            "method": "getTokenAccounts",
            "params": {
                "mint": mint_address,
                "page": 1,
                "limit": 100  # Sample top 100 holders
            }
        }
        headers = {"Content-Type": "application/json"}
        
        async with aiohttp.ClientSession() as session:
            async with session.post(rpc_url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=6)) as r:
                if r.status != 200:
                    return result
                data = await r.json()
                accounts = data.get("result", {}).get("token_accounts", [])
                if not accounts:
                    return result
            
            # Extract unique owner addresses
            owners = list({acc.get("owner") for acc in accounts if acc.get("owner")})
            if not owners:
                return result
            
            # Step 2: Batch check native SOL balances (max 100 at once)
            batch_size = 100
            owners_batch = owners[:batch_size]
            
            balance_payload = {
                "jsonrpc": "2.0",
                "id": "fresh-balances",
                "method": "getMultipleAccounts",
                "params": [
                    owners_batch,
                    {"encoding": "base64"}
                ]
            }
            
            async with session.post(rpc_url, headers=headers, json=balance_payload, timeout=aiohttp.ClientTimeout(total=6)) as r2:
                if r2.status != 200:
                    return result
                bal_data = await r2.json()
                accs = bal_data.get("result", {}).get("value", [])
                
                fresh = 0
                total = len(accs)
                for acc_info in accs:
                    if acc_info is None:
                        fresh += 1  # Account doesn't exist = definitely fresh/empty
                        continue
                    lamports = acc_info.get("lamports", 0)
                    sol_balance = lamports / 1e9
                    if sol_balance < 0.1:
                        fresh += 1
                
                result["fresh_count"] = fresh
                result["total_sampled"] = total
                result["fresh_pct"] = (fresh / total * 100) if total > 0 else 0.0
    except Exception as e:
        logger.error(f"Error in get_fresh_wallets_count for {mint_address}: {e}")
    
    return result


# ─────────────────────────────────────────────
#  SOCIAL & DOMAIN UTILITY TOOL
# ─────────────────────────────────────────────

import re
import datetime
import unicodedata
from difflib import SequenceMatcher

# Curated list of trusted/known legitimate domains (100+ outlets)
TRUSTED_DOMAINS = {
    # US Major News & Publications
    "cnn.com","bbc.com","reuters.com","bloomberg.com","nytimes.com",
    "wsj.com","forbes.com","techcrunch.com","theverge.com","wired.com",
    "ft.com","theguardian.com","apnews.com","washingtonpost.com",
    "foxnews.com","nbcnews.com","cbsnews.com","yahoo.com","time.com",
    "economist.com","cnbc.com","marketwatch.com","businessinsider.com",
    "nypost.com","nyp.st","usatoday.com","latimes.com","chicagotribune.com",
    "abcnews.go.com","huffpost.com","politico.com","axios.com",
    "dailymail.co.uk","independent.co.uk","telegraph.co.uk","newsweek.com",
    "npr.org","pbs.org","cnet.com","zdnet.com","engadget.com",
    # Financial & Business
    "barrons.com","fool.com","seekingalpha.com","investopedia.com",
    "fortune.com","foxbusiness.com","thestreet.com","benzinga.com",
    # Crypto & Web3 Media
    "coindesk.com","cointelegraph.com","decrypt.co","theblock.co",
    "cryptonews.com","bitcoinmagazine.com","blockworks.co","beincrypto.com",
    "cryptoslate.com","ambcrypto.com","newsbtc.com","dailyhodl.com",
    "dlnews.com","u.today","coingape.com","bitcoin.com",
    # Exchanges & Wallets
    "binance.com","coinbase.com","kraken.com","okx.com","bybit.com",
    "kucoin.com","bitget.com","gate.io","crypto.com","gemini.com",
    "phantom.app","solflare.com","metamask.io","ledger.com",
    # Solana Ecosystem
    "solana.com","jup.ag","raydium.io","pump.fun","dexscreener.com",
    "birdeye.so","rugcheck.xyz","solscan.io","orca.so","drift.trade",
    "marginfi.com","sanctum.so","helium.com","tensor.trade",
    # Tech & Social Platforms
    "twitter.com","x.com","telegram.org","discord.com","reddit.com",
    "github.com","medium.com","youtube.com","substack.com","linktree.ee",
}

# Suspicious TLDs when used by "news" or project sites
_SUSPICIOUS_TLDS = {".xyz",".info",".click",".loan",".top",".online",
                    ".site",".biz",".tk",".ml",".ga",".cf",".pw",".icu"}

def _normalize(text: str) -> str:
    """Lowercase, strip www, normalize homoglyphs (0→o, 1→l, rn→m, vv→w)."""
    text = unicodedata.normalize("NFKD", text).lower().strip()
    if text.startswith("www."):
        text = text[4:]
    text = text.replace("vv","w").replace("rn","m")
    for src, dst in [("0","o"),("1","l"),("3","e"),("4","a"),("5","s"),("@","a")]:
        text = text.replace(src, dst)
    return text

def check_lookalike_domain(domain: str) -> dict:
    """Detect typosquatting / impersonation of known trusted domains and verify official domains."""
    domain = domain.lower().strip().removeprefix("https://").removeprefix("http://").removeprefix("www.").split("/")[0]
    result = {
        "is_verified": False,
        "is_lookalike": False,
        "similar_to": None,
        "similarity_pct": 0,
        "suspicious_tld": False,
        "suspicious_tld_name": None,
        "flags": []
    }

    # 1. Exact match with trusted domain registry
    if domain in TRUSTED_DOMAINS:
        result["is_verified"] = True
        return result

    # 2. Check suspicious TLD
    for tld in _SUSPICIOUS_TLDS:
        if domain.endswith(tld):
            result["suspicious_tld"] = True
            result["suspicious_tld_name"] = tld
            result["flags"].append(f"Suspicious TLD `{tld}` — commonly used for scam/phishing sites")
            break

    # 3. Homoglyph / typosquatting / lookalike analysis
    norm_input = _normalize(domain)
    input_root = norm_input.split(".")[0]
    best_match, best_score = None, 0.0

    for trusted in TRUSTED_DOMAINS:
        trusted_norm = _normalize(trusted)
        trusted_root = trusted_norm.split(".")[0]

        # Same root name, different TLD = almost certainly impersonating (e.g. cnn.xyz vs cnn.com)
        if input_root == trusted_root and domain != trusted:
            result.update({"is_lookalike": True, "similar_to": trusted, "similarity_pct": 99})
            result["flags"].append(f"Exact name match with **{trusted}** but different TLD — likely impersonating!")
            return result

        score = SequenceMatcher(None, input_root, trusted_root).ratio()
        if score > best_score and score >= 0.75 and input_root != trusted_root:
            best_score, best_match = score, trusted

    if best_match:
        result.update({"is_lookalike": True, "similar_to": best_match, "similarity_pct": int(best_score * 100)})
        result["flags"].append(f"`{domain}` is **{int(best_score*100)}% similar** to **{best_match}** — possible typosquatting")

    # If domain is not in TRUSTED_DOMAINS and not a lookalike, flag as unverified
    if not result["is_verified"] and not result["is_lookalike"]:
        result["flags"].append(f"⚠️ `{domain}` is **unverified / unknown** (not in official media registry)")

    return result


async def get_domain_info(domain: str) -> dict:
    """RDAP domain lookup + Wayback first-archive date + lookalike check."""
    domain = domain.lower().strip().removeprefix("https://").removeprefix("http://").removeprefix("www.").split("/")[0]
    result = {
        "domain": domain, "registered_date": None, "expiry_date": None,
        "registrar": None, "privacy_protected": False,
        "first_archived": None, "days_old": None,
        "lookalike": check_lookalike_domain(domain), "error": None
    }

    # 1. RDAP (free, no key)
    try:
        rdap = await fetch_json(f"https://rdap.org/domain/{domain}")
        if rdap:
            for ev in rdap.get("events", []):
                action, date_str = ev.get("eventAction",""), ev.get("eventDate","")
                if action == "registration" and date_str:
                    try:
                        dt = datetime.datetime.fromisoformat(date_str.replace("Z","+00:00"))
                        result["registered_date"] = dt.strftime("%b %d, %Y")
                        result["days_old"] = (datetime.datetime.now(datetime.timezone.utc) - dt).days
                    except Exception:
                        result["registered_date"] = date_str[:10]
                elif action == "expiration" and date_str:
                    result["expiry_date"] = date_str[:10]
            for ent in rdap.get("entities", []):
                if "registrar" in ent.get("roles", []):
                    vcard = ent.get("vcardArray", [[],[]])[1]
                    for field in vcard:
                        if field[0] == "fn":
                            result["registrar"] = field[3]; break
            result["privacy_protected"] = not any(
                "registrant" in e.get("roles",[]) for e in rdap.get("entities",[])
            )
    except Exception as e:
        result["error"] = str(e)[:80]

    # 2. Wayback first archive
    try:
        cdx = await fetch_json(
            f"https://web.archive.org/cdx/search/cdx?url={domain}&output=json"
            f"&fl=timestamp&limit=1&from=20000101&filter=statuscode:200"
        )
        if cdx and len(cdx) > 1:
            ts = cdx[1][0]
            result["first_archived"] = datetime.datetime.strptime(ts[:8],"%Y%m%d").strftime("%b %d, %Y")
    except Exception:
        pass

    return result


async def get_twitter_audit(handle: str) -> dict:
    """
    Audits an X (Twitter) account using Twitter CDN Syndication + FxTwitter open APIs
    and Twitter Snowflake ID math for instant, exact account age calculation.
    """
    handle = handle.lstrip("@").strip()
    logger.info(f"[Twitter Audit] Starting audit for handle: @{handle}")
    
    result = {
        "handle": handle,
        "display_name": None,
        "joined_date": None,
        "joined_days_ago": None,
        "followers": None,
        "following": None,
        "tweet_count": None,
        "bio": None,
        "user_id": None,
        "avatar_url": None,
        "is_verified": False,
        "verified_type": "None",
        "protected": False,
        "possible_rename": False,
        "nickname_impersonation": False,
        "impersonated_brand": None,
        "error": None
    }

    hdrs = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json"
    }

    user_data = None

    # --- Step 1: FxTwitter Status Embed API ---
    # FxTwitter works best when fetching a tweet URL; we use the profile shortcut
    try:
        async with aiohttp.ClientSession() as session:
            fx_url = f"https://api.fxtwitter.com/{handle}"
            async with session.get(fx_url, headers=hdrs, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                logger.info(f"[Twitter Audit] FxTwitter status for {fx_url}: {resp.status}")
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    u = data.get("user") or data
                    if u and (u.get("name") or u.get("screen_name") or u.get("id")):
                        is_ver = bool(
                            u.get("verified") or u.get("is_verified") or 
                            u.get("is_blue_verified") or u.get("ext_is_blue_verified") or 
                            u.get("verified_type")
                        )
                        v_type = u.get("verified_type") or ("Blue" if is_ver else "None")
                        user_data = u
                        user_data["verified"] = is_ver
                        user_data["verified_type"] = v_type
                        logger.info(f"[Twitter Audit] ✅ Fetched @{handle} via FxTwitter (Verified: {is_ver})")
    except Exception as e:
        logger.warning(f"[Twitter Audit] FxTwitter failed for @{handle}: {e}")

    # --- Step 2: Twitter oEmbed API (No auth, always works for public accounts) ---
    if not user_data:
        try:
            oembed_url = f"https://publish.twitter.com/oembed?url=https://twitter.com/{handle}&omit_script=true"
            async with aiohttp.ClientSession() as session:
                async with session.get(oembed_url, headers=hdrs, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    logger.info(f"[Twitter Audit] oEmbed status for @{handle}: {resp.status}")
                    if resp.status == 200:
                        oembed = await resp.json(content_type=None)
                        author_name = oembed.get("author_name")
                        author_url = oembed.get("author_url", "")
                        # Extract screen_name from author_url like https://twitter.com/zubin_eth
                        oembed_handle = author_url.rstrip("/").split("/")[-1] if author_url else handle
                        html_body = oembed.get("html", "")
                        if author_name:
                            user_data = {
                                "name": author_name,
                                "screen_name": oembed_handle,
                                "verified": False,
                                "verified_type": "None"
                            }
                            logger.info(f"[Twitter Audit] ✅ Fetched @{handle} via Twitter oEmbed (name: {author_name})")
        except Exception as e:
            logger.warning(f"[Twitter Audit] oEmbed failed for @{handle}: {e}")

    # --- Step 3: Twitter CDN Syndication Widget API ---
    if not user_data:
        synd_url = f"https://cdn.syndication.twimg.com/widgets/followbutton/info.json?screen_names={handle}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(synd_url, headers=hdrs, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    logger.info(f"[Twitter Audit] Syndication API status for @{handle}: {resp.status}")
                    if resp.status == 200:
                        arr = await resp.json(content_type=None)
                        if isinstance(arr, list) and len(arr) > 0:
                            u = arr[0]
                            is_ver = bool(
                                u.get("verified") or u.get("is_verified") or 
                                u.get("is_blue_verified") or u.get("ext_is_blue_verified") or 
                                u.get("verified_type")
                            )
                            v_type = u.get("verified_type") or ("Blue" if is_ver else "None")
                            user_data = {
                                "name": u.get("name"),
                                "screen_name": u.get("screen_name"),
                                "id": u.get("id"),
                                "followers": u.get("followers_count"),
                                "following": u.get("friends_count", 0),
                                "tweets": u.get("statuses_count", 0),
                                "avatar_url": u.get("profile_image_url_https"),
                                "verified": is_ver,
                                "verified_type": v_type,
                                "protected": u.get("protected", False)
                            }
                            logger.info(f"[Twitter Audit] ✅ Fetched @{handle} via CDN Syndication")
        except Exception as e:
            logger.warning(f"[Twitter Audit] Syndication API failed for @{handle}: {e}")

    # Step 3: Parse user_data if obtained
    if user_data:
        result["display_name"] = user_data.get("name") or user_data.get("screen_name") or handle
        result["bio"] = (user_data.get("description") or user_data.get("bio") or "").strip()[:200]
        result["is_verified"] = bool(
            user_data.get("verified") or 
            user_data.get("is_verified") or 
            user_data.get("is_blue_verified") or 
            user_data.get("ext_is_blue_verified") or 
            user_data.get("verified_type")
        )
        result["protected"] = bool(user_data.get("protected"))
        
        v_type = user_data.get("verified_type") or user_data.get("verification_type")
        if v_type:
            result["verified_type"] = str(v_type).capitalize()
        elif result["is_verified"]:
            result["verified_type"] = "Blue"

        followers_cnt = user_data.get("followers") or user_data.get("followers_count") or 0
        following_cnt = user_data.get("following") or user_data.get("friends_count") or 0
        tweets_cnt = user_data.get("tweets") or user_data.get("statuses_count") or user_data.get("post_count") or 0
        
        result["followers"] = f"{followers_cnt:,}" if isinstance(followers_cnt, int) else str(followers_cnt)
        result["following"] = f"{following_cnt:,}" if isinstance(following_cnt, int) else str(following_cnt)
        result["tweet_count"] = f"{tweets_cnt:,}" if isinstance(tweets_cnt, int) else str(tweets_cnt)
        result["avatar_url"] = user_data.get("avatar_url") or user_data.get("profile_image_url_https")
        
        # Check Nickname/Display Name Impersonation
        disp_upper = (result["display_name"] or "").upper()
        known_brands = ["CNN", "REUTERS", "BLOOMBERG", "NY POST", "BBC", "BINANCE", "COINBASE", "KRAKEN", "SOLANA", "JUPITER", "PHANTOM", "PUMP.FUN", "DEXSCREENER", "ELON MUSK"]
        for brand in known_brands:
            if brand in disp_upper and brand.lower().replace(" ", "") not in handle.lower():
                if not result["is_verified"]:
                    result["nickname_impersonation"] = True
                    result["impersonated_brand"] = brand
                    logger.warning(f"[Twitter Audit] Nickname impersonation detected: Display name '{result['display_name']}' claims brand '{brand}' for handle @{handle}")
                    break
        
        # Extract user ID and decode Twitter Snowflake timestamp for exact creation date
        user_id_val = user_data.get("id") or user_data.get("id_str")
        if user_id_val:
            try:
                uid_int = int(user_id_val)
                result["user_id"] = str(uid_int)
                # Twitter Snowflake epoch: 1288834974657 (Nov 4, 2010 UTC)
                timestamp_ms = (uid_int >> 22) + 1288834974657
                if timestamp_ms > 0:
                    created_dt = datetime.datetime.fromtimestamp(timestamp_ms / 1000.0, tz=datetime.timezone.utc)
                    result["joined_date"] = created_dt.strftime("%b %d, %Y")
                    result["joined_days_ago"] = (datetime.datetime.now(datetime.timezone.utc) - created_dt).days
                    logger.info(f"[Twitter Audit] Snowflake decoded User ID {uid_int} -> Created {result['joined_date']} ({result['joined_days_ago']} days ago)")
            except Exception as _e:
                logger.error(f"[Twitter Audit] Snowflake decode error for ID {user_id_val}: {_e}")

    # Step 4: Nitter fallback if all previous failed
    if not result["display_name"]:
        logger.info(f"[Twitter Audit] Attempting Nitter scraping fallback for @{handle}...")
        for base in ["https://nitter.net", "https://nitter.privacydev.net", "https://nitter.poast.org"]:
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.get(f"{base}/{handle}", headers=hdrs, timeout=aiohttp.ClientTimeout(total=5)) as r:
                        logger.info(f"[Twitter Audit] Nitter instance {base} status: {r.status}")
                        if r.status == 200:
                            html = await r.text()
                            m_name = re.search(r'class="profile-card-fullname"[^>]*>.*?<span[^>]*>([^<]+)</span>', html, re.DOTALL)
                            if m_name:
                                result["display_name"] = m_name.group(1).strip()
                            m_bio = re.search(r'class="profile-bio"[^>]*>.*?<p[^>]*>(.*?)</p>', html, re.DOTALL)
                            if m_bio:
                                result["bio"] = re.sub(r"<[^>]+>", "", m_bio.group(1)).strip()[:200]
                            m_fol = re.search(r'class="followers"[^>]*>.*?title="([\d,]+)"', html, re.DOTALL)
                            if m_fol:
                                result["followers"] = m_fol.group(1)
                            m_tw = re.search(r'"tweets-count"[^>]*>([\d,\.K]+)<', html, re.DOTALL)
                            if m_tw:
                                result["tweet_count"] = m_tw.group(1)
                            break
            except Exception as ne:
                logger.error(f"[Twitter Audit] Nitter node {base} failed: {ne}")

    if not result["display_name"]:
        result["error"] = "Could not scrape profile metadata"

    # Step 5: Wayback CDX Multi-Endpoint Rename Audit & memory.lol
    result["historical_handles"] = []
    result["rename_count"] = 0
    result["historical_handles_detail"] = []

    if result.get("user_id"):
        uid = result["user_id"]
        # found_handles: dict mapping lowercase handle -> {"handle": str, "first_seen": str|None, "last_seen": str|None}
        found_handles_map: Dict[str, Dict[str, Any]] = {}

        # 5a. Query memory.lol API (Fast and precise username history indexer)
        memory_success = False
        try:
            memory_url = f"https://api.memory.lol/v1/tw/id/{uid}"
            async with aiohttp.ClientSession() as s:
                async with s.get(memory_url, headers=hdrs, timeout=aiohttp.ClientTimeout(total=4)) as r:
                    if r.status == 200:
                        data = await r.json(content_type=None)
                        sn_dict = data.get("screen_names", {})
                        if isinstance(sn_dict, dict):
                            for sn, dates in sn_dict.items():
                                if sn.lower() != handle.lower():
                                    # memory.lol returns dates as ["YYYY-MM-DD", ...] or a single string
                                    first_s = last_s = None
                                    if isinstance(dates, list) and dates:
                                        first_s = dates[0] if dates else None
                                        last_s = dates[-1] if len(dates) > 1 else first_s
                                    elif isinstance(dates, str):
                                        first_s = last_s = dates
                                    key = sn.lower()
                                    if key not in found_handles_map:
                                        found_handles_map[key] = {"handle": sn, "first_seen": first_s, "last_seen": last_s}
                            logger.info(f"[Twitter Audit] Successfully fetched handle history from memory.lol for ID {uid}")
                            memory_success = True
        except Exception as _me:
            logger.warning(f"[Twitter Audit] memory.lol lookup failed for ID {uid}: {_me}")

        # 5b. Fallback to Concurrent Deep Query across 4 Wayback CDX archive patterns if memory.lol failed
        if not memory_success:
            try:
                cdx_urls = [
                    f"https://web.archive.org/cdx/search/cdx?url=twitter.com/intent/user?user_id={uid}&output=json&fl=original,timestamp&limit=100",
                    f"https://web.archive.org/cdx/search/cdx?url=twitter.com/i/user/{uid}&output=json&fl=original,timestamp&limit=100",
                    f"https://web.archive.org/cdx/search/cdx?url=twitter.com/intent/follow?user_id={uid}&output=json&fl=original,timestamp&limit=100",
                    f"https://web.archive.org/cdx/search/cdx?url=twitter.com/intent/tweet?via={uid}&output=json&fl=original,timestamp&limit=100"
                ]

                async def _fetch_cdx(url: str):
                    try:
                        async with aiohttp.ClientSession() as s:
                            async with s.get(url, timeout=aiohttp.ClientTimeout(total=4)) as r:
                                if r.status == 200:
                                    return await r.json()
                    except Exception:
                        pass
                    return None

                cdx_results = await asyncio.gather(*[_fetch_cdx(u) for u in cdx_urls], return_exceptions=True)
                for res in cdx_results:
                    if isinstance(res, list) and len(res) > 1:
                        for row in res[1:]:
                            orig_url = row[0]
                            ts_str = row[1] if len(row) > 1 else None
                            # Parse Wayback timestamp YYYYMMDDHHMMSS -> readable date
                            ts_readable = None
                            if ts_str and len(ts_str) >= 8:
                                try:
                                    ts_readable = datetime.datetime.strptime(ts_str[:8], "%Y%m%d").strftime("%b %d, %Y")
                                except Exception:
                                    pass
                            # Match screen_name=... or via=... or twitter.com/...
                            matches = re.findall(r'(?:screen_name=|via=|twitter\.com/)([a-zA-Z0-9_]{3,15})', orig_url, re.IGNORECASE)
                            for m in matches:
                                m_clean = m.strip()
                                # Filter out system paths
                                if m_clean.lower() not in (handle.lower(), "intent", "i", "user", "follow", "tweet", "search", "home", "settings", "explore"):
                                    key = m_clean.lower()
                                    if key not in found_handles_map:
                                        found_handles_map[key] = {"handle": m_clean, "first_seen": ts_readable, "last_seen": ts_readable}
                                    elif ts_readable:
                                        # Update last_seen to later date if applicable
                                        entry = found_handles_map[key]
                                        if not entry["last_seen"] or ts_readable > entry["last_seen"]:
                                            entry["last_seen"] = ts_readable
            except Exception as _e:
                logger.debug(f"[Twitter Audit] User ID CDX deep scan skipped/timed out: {_e}")

        # Update result metrics — expose human-readable list
        if found_handles_map:
            result["historical_handles"] = [f"@{e['handle']}" for e in found_handles_map.values()]
            result["historical_handles_detail"] = list(found_handles_map.values())  # full detail with timestamps
            result["rename_count"] = len(found_handles_map)
            result["possible_rename"] = True
            logger.info(f"[Twitter Audit] ID {uid} has changed handle {result['rename_count']} time(s). Past handles: {result['historical_handles']}")

    return result


async def get_username_history(handle: str) -> dict:
    """
    Focused username-history lookup for a Twitter/X handle.
    Returns a structured dict with:
      - handle: str
      - user_id: str | None
      - display_name: str | None
      - current_handle: str
      - rename_count: int
      - past_handles: list of {handle, first_seen, last_seen}
      - joined_date: str | None
      - followers: str | None
      - is_verified: bool
      - error: str | None
    Pulls from memory.lol, then Wayback CDX.
    """
    handle = handle.lstrip("@").strip()
    logger.info(f"[Username History] Starting lookup for @{handle}")

    result: Dict[str, Any] = {
        "handle": handle,
        "user_id": None,
        "display_name": None,
        "current_handle": handle,
        "rename_count": 0,
        "past_handles": [],  # list of {handle, first_seen, last_seen}
        "joined_date": None,
        "followers": None,
        "is_verified": False,
        "error": None
    }

    hdrs = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json"
    }

    # --- Step 1: Resolve user_id via full audit (re-uses existing logic) ---
    audit = await get_twitter_audit(handle)
    if audit.get("error") and not audit.get("user_id"):
        result["error"] = audit["error"]

    result["user_id"] = audit.get("user_id")
    result["display_name"] = audit.get("display_name")
    result["joined_date"] = audit.get("joined_date")
    result["followers"] = audit.get("followers")
    result["is_verified"] = audit.get("is_verified", False)
    result["verified_type"] = audit.get("verified_type", "None")

    # --- Step 2: Load full handle detail from audit (now directly pulled dynamically) ---
    past_detail: list = []
    for entry in audit.get("historical_handles_detail", []):
        h_str = entry.get("handle", "").lstrip("@")
        if not h_str or h_str.lower() == handle.lower():
            continue
        # Avoid duplicates
        if not any(p["handle"].lower() == h_str.lower() for p in past_detail):
            past_detail.append(entry)

    result["past_handles"] = past_detail
    result["rename_count"] = len(past_detail)
    return result


def calculate_risk_score(twitter_data: Optional[dict], domain_data: Optional[dict]) -> tuple:
    """Returns (score: int, flags: list[str]) — score 0-10."""
    score = 0
    flags = []

    if twitter_data and not twitter_data.get("error"):
        days = twitter_data.get("joined_days_ago")
        followers_raw = twitter_data.get("followers") or "0"
        following_raw = twitter_data.get("following") or "0"
        tweets_raw = twitter_data.get("tweet_count") or "0"

        try:
            f_count = int(re.sub(r"[^0-9]", "", str(followers_raw)) or 0)
            fg_count = int(re.sub(r"[^0-9]", "", str(following_raw)) or 0)
            tw_count = int(re.sub(r"[^0-9]", "", str(tweets_raw)) or 0)
        except Exception:
            f_count, fg_count, tw_count = 0, 0, 0

        # 1. Fresh account risk
        if days is not None:
            if days < 7:
                score += 3
                flags.append(f"🚨 BRAND NEW ACCOUNT: Created just **{days} days ago**")
            elif days < 30:
                score += 2
                flags.append(f"❗ Fresh X account: Created **{days} days ago**")
            elif days < 90:
                score += 1
                flags.append(f"⚠️ Relatively new account: Created **{days} days ago**")

        # 2. Bought Aged Account Detection (Created > 1 year ago, but < 10 tweets & low followers)
        if days is not None and days > 365 and tw_count <= 10 and f_count < 150:
            score += 3
            flags.append(f"🚨 SUSPICIOUS RECYCLED ACCOUNT: Account created **{days // 365} years ago**, but has only **{tw_count} tweets** (Classic purchased/wiped account sign)")

        # 3. Botted Follower Anomaly (High followers but almost zero tweets)
        if f_count > 5000 and tw_count < 20:
            score += 3
            flags.append(f"🚨 SUSPICIOUS FOLLOWER ANOMALY: **{f_count:,} followers** with only **{tw_count} total tweets** (High probability of botted followers)")

        # 4. Mass-Following / Spam Ratio
        if fg_count > 800 and f_count < 100:
            score += 2
            flags.append(f"⚠️ Follower Farm Ratio: Following **{fg_count:,} accounts** with only **{f_count} followers**")

        # 5. Handle Rebrand / Frequent Username Change Counter
        r_cnt = twitter_data.get("rename_count", 0)
        if r_cnt > 0:
            score += min(r_cnt * 2, 5)
            prev_handles_str = ", ".join(twitter_data.get("historical_handles", []))
            flags.append(f"🚨 FREQUENT REBRAND ALERT: Account has changed handle **{r_cnt} time(s)**! Past handles: **{prev_handles_str}**")
        elif twitter_data.get("possible_rename"):
            score += 2
            flags.append("🚨 REBRANDED ACCOUNT: Evidence of prior username change detected in archive records")

        # 6. Blank Bio / Minimal Profile Setup
        if not twitter_data.get("bio"):
            score += 1
            flags.append("⚠️ Minimal Profile Setup: Account has no bio description")

        # 7. Nickname Impersonation Flag
        if twitter_data.get("nickname_impersonation"):
            score += 3
            flags.append(f"🚨 IMPERSONATION NICKNAME: Display name claims **{twitter_data.get('impersonated_brand')}** but handle (@{twitter_data.get('handle')}) is unverified!")

    if domain_data:
        days_old = domain_data.get("days_old")
        if days_old is not None:
            if days_old < 7:
                score += 3
                flags.append(f"🚨 Domain registered just **{days_old} days ago**")
            elif days_old < 30:
                score += 2
                flags.append(f"❗ Domain is only **{days_old} days old**")
            elif days_old < 90:
                score += 1
                flags.append(f"⚠️ Domain registered **{days_old} days ago**")

        if domain_data.get("privacy_protected"):
            score += 1
            flags.append("⚠️ Domain owner is **privacy protected / hidden**")

        if not domain_data.get("first_archived"):
            score += 1
            flags.append("⚠️ Domain has never been indexed by Wayback Machine")

        lk = domain_data.get("lookalike", {})
        for f in lk.get("flags", []):
            score += 3
            flags.append(f"🚨 FAKE DOMAIN: {f}")

    return min(score, 10), flags


async def fetch_gmgn_json(url: str, headers: Dict[str, str], max_retries: int = 3) -> Optional[Dict[str, Any]]:
    """Helper to perform requests to GMGN OpenAPI with rate limit retry logic."""
    for attempt in range(1, max_retries + 1):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=10) as response:
                    if response.status == 200:
                        data = await response.json()
                        if data and data.get("code") == 0:
                            return data
                        else:
                            err_msg = data.get("message") or data.get("error") or "Unknown API error"
                            logger.warning(f"GMGN API error (code {data.get('code')}): {err_msg}")
                            # Check if rate limit
                            if "RATE_LIMIT" in str(err_msg).upper() or data.get("code") == 429:
                                reset_str = response.headers.get("x-ratelimit-reset") or response.headers.get("X-RateLimit-Reset")
                                wait_sec = 2
                                if reset_str:
                                    try:
                                        wait_sec = max(1, int(reset_str) - int(time.time()) + 1)
                                    except ValueError:
                                        pass
                                logger.info(f"Rate limited by GMGN, waiting {wait_sec}s before retry (attempt {attempt}/{max_retries})...")
                                await asyncio.sleep(wait_sec)
                                continue
                            return data
                    elif response.status == 429:
                        reset_str = response.headers.get("x-ratelimit-reset") or response.headers.get("X-RateLimit-Reset")
                        wait_sec = 2
                        if reset_str:
                            try:
                                wait_sec = max(1, int(reset_str) - int(time.time()) + 1)
                            except ValueError:
                                pass
                        logger.info(f"Rate limited (status 429) by GMGN, waiting {wait_sec}s before retry (attempt {attempt}/{max_retries})...")
                        await asyncio.sleep(wait_sec)
                        continue
                    else:
                        logger.error(f"Error fetching GMGN {url}: Status {response.status}")
                        return None
        except Exception as e:
            logger.warning(f"Request exception for GMGN {url}: {e}")
            if attempt < max_retries:
                await asyncio.sleep(1)
            else:
                return None
    return None


async def get_gmgn_token_info(chain: str, address: str) -> Optional[Dict[str, Any]]:
    """
    Fetches token general info and stats from GMGN OpenAPI.
    """
    api_key = os.getenv("GMGN_API_KEY")
    if not api_key:
        logger.warning("GMGN_API_KEY not configured in env.")
        return None
    
    gmgn_chain = chain.lower()
    if gmgn_chain == "solana":
        gmgn_chain = "sol"
    elif gmgn_chain in ("ethereum", "eth"):
        gmgn_chain = "eth"
    
    import uuid
    client_id = str(uuid.uuid4())
    timestamp = int(time.time())
    
    url = f"https://openapi.gmgn.ai/v1/token/info?chain={gmgn_chain}&address={address}&timestamp={timestamp}&client_id={client_id}"
    headers = {
        "X-APIKEY": api_key,
        "User-Agent": "gmgn-cli/1.0.0"
    }
    
    res = await fetch_gmgn_json(url, headers)
    if res and res.get("code") == 0:
        data = res.get("data")
        if data and not data.get("address"):
            return None
        return data
    return None


async def get_gmgn_token_security(chain: str, address: str) -> Optional[Dict[str, Any]]:
    """
    Fetches token security details (mint/freeze authority, lock status, honeypot) from GMGN OpenAPI.
    """
    api_key = os.getenv("GMGN_API_KEY")
    if not api_key:
        logger.warning("GMGN_API_KEY not configured in env.")
        return None
    
    gmgn_chain = chain.lower()
    if gmgn_chain == "solana":
        gmgn_chain = "sol"
    elif gmgn_chain in ("ethereum", "eth"):
        gmgn_chain = "eth"
    
    import uuid
    client_id = str(uuid.uuid4())
    timestamp = int(time.time())
    
    url = f"https://openapi.gmgn.ai/v1/token/security?chain={gmgn_chain}&address={address}&timestamp={timestamp}&client_id={client_id}"
    headers = {
        "X-APIKEY": api_key,
        "User-Agent": "gmgn-cli/1.0.0"
    }
    
    res = await fetch_gmgn_json(url, headers)
    if res and res.get("code") == 0:
        data = res.get("data")
        # Check if dummy/empty data (all key flag fields are None)
        if data and data.get("is_honeypot") is None and data.get("is_renounced") is None:
            return None
        return data
    return None


async def get_gmgn_token_holders(chain: str, address: str, limit: int = 100, tag: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Fetches top token holders (with tags and percentages) from GMGN OpenAPI.
    Optional tag filters: smart_degen, renowned, fresh_wallet, dev, sniper, rat_trader, bundler, transfer_in, dex_bot, bluechip_owner
    """
    api_key = os.getenv("GMGN_API_KEY")
    if not api_key:
        logger.warning("GMGN_API_KEY not configured in env.")
        return None
    
    gmgn_chain = chain.lower()
    if gmgn_chain == "solana":
        gmgn_chain = "sol"
    elif gmgn_chain in ("ethereum", "eth"):
        gmgn_chain = "eth"
    
    import uuid
    client_id = str(uuid.uuid4())
    timestamp = int(time.time())
    
    url = f"https://openapi.gmgn.ai/v1/market/token_top_holders?chain={gmgn_chain}&address={address}&limit={limit}&timestamp={timestamp}&client_id={client_id}"
    if tag:
        url += f"&tag={tag}"
        
    headers = {
        "X-APIKEY": api_key,
        "User-Agent": "gmgn-cli/1.0.0"
    }
    
    res = await fetch_gmgn_json(url, headers)
    if res and res.get("code") == 0:
        data = res.get("data")
        if data and not data.get("list"):
            return None
        return data
    return None
