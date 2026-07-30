import asyncio
from typing import Any, Dict, List, Optional

import aiohttp


RED_FLAG_SUPPLY_PCT = 10.0


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        if isinstance(value, str):
            value = value.replace("%", "").replace(",", "").strip()
        return float(value)
    except Exception:
        return default


def _short(address: str) -> str:
    if not address:
        return "unknown"
    return f"{address[:6]}...{address[-6:]}" if len(address) > 14 else address


def _extract_pct(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        stripped = value.replace("%", "").strip()
        return _as_float(stripped, None)
    return None


async def fetch_largest_token_accounts(mint: str, rpc_url: str) -> List[Dict[str, Any]]:
    """Free RPC fallback. Standard RPC returns the 20 largest token accounts for a mint."""
    if not mint or not rpc_url:
        return []

    payload = {
        "jsonrpc": "2.0",
        "id": "largest-token-accounts",
        "method": "getTokenLargestAccounts",
        "params": [mint, {"commitment": "confirmed"}],
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                rpc_url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as response:
                if response.status != 200:
                    return []
                data = await response.json()
                return data.get("result", {}).get("value", []) or []
    except Exception:
        return []


def summarize_rugcheck(rug_report: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    token = (rug_report or {}).get("token") or {}
    total_supply = _as_float(token.get("supply"))
    networks = (rug_report or {}).get("insiderNetworks") or []
    risks = (rug_report or {}).get("risks") or []
    top_holders = (rug_report or {}).get("topHolders") or []

    clusters = []
    insider_total_pct = 0.0
    bundler_total_pct = 0.0
    sniper_pct = None

    for risk in risks:
        name = str(risk.get("name", "")).lower()
        value = risk.get("value")
        pct = _extract_pct(value)
        if pct is not None and ("sniper" in name or "bot" in name):
            sniper_pct = pct
        if pct is not None and "bundle" in name:
            bundler_total_pct = max(bundler_total_pct, pct)

    for idx, network in enumerate(networks, 1):
        amount = _as_float(network.get("tokenAmount"))
        pct = (amount / total_supply * 100.0) if total_supply > 0 else _as_float(network.get("percentage") or network.get("pct"))
        size = int(_as_float(network.get("size")))
        kind = str(network.get("type") or "linked")
        wallets = network.get("wallets") or network.get("nodes") or []

        insider_total_pct += pct
        if kind.lower() in {"transfer", "bundle", "bundler"}:
            bundler_total_pct += pct

        clusters.append({
            "id": idx,
            "type": kind,
            "wallet_count": size or len(wallets),
            "supply_pct": pct,
            "wallets": wallets[:15],
            "source": "RugCheck insider graph",
            "red_flag": pct >= RED_FLAG_SUPPLY_PCT,
        })

    if insider_total_pct <= 0:
        insider_wallets = [h for h in top_holders if h.get("insider") is True]
        insider_total_pct = sum(_as_float(h.get("pct")) for h in insider_wallets)

    return {
        "total_supply": total_supply,
        "total_holders": (rug_report or {}).get("totalHolders") or 0,
        "insider_pct": insider_total_pct,
        "bundler_pct": bundler_total_pct,
        "sniper_pct": sniper_pct,
        "clusters": clusters,
        "risk_names": [r.get("name", "Unknown risk") for r in risks],
        "source": "RugCheck",
    }


def summarize_solana_tracker(st_bundlers: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not st_bundlers:
        return {"bundler_pct": 0.0, "clusters": [], "wallet_count": 0, "source": None}

    wallets = st_bundlers.get("wallets") or st_bundlers.get("holders") or []
    total_pct = _as_float(
        st_bundlers.get("totalPercentage")
        or st_bundlers.get("totalBundlerPercentage")
        or st_bundlers.get("percentage")
    )

    clusters_by_group: Dict[str, Dict[str, Any]] = {}
    for wallet in wallets:
        address = wallet.get("wallet") or wallet.get("address") or ""
        pct = _as_float(wallet.get("percentage") or wallet.get("pct"))
        group = str(wallet.get("cluster") or wallet.get("clusterId") or wallet.get("bundleId") or "bundler")
        cluster = clusters_by_group.setdefault(group, {
            "id": group,
            "type": "bundler",
            "wallet_count": 0,
            "supply_pct": 0.0,
            "wallets": [],
            "source": "Solana Tracker bundlers",
            "red_flag": False,
        })
        cluster["wallet_count"] += 1
        cluster["supply_pct"] += pct
        if address:
            cluster["wallets"].append(address)

    clusters = list(clusters_by_group.values())
    for cluster in clusters:
        cluster["wallets"] = cluster["wallets"][:15]
        cluster["red_flag"] = cluster["supply_pct"] >= RED_FLAG_SUPPLY_PCT

    if total_pct <= 0 and clusters:
        total_pct = sum(c["supply_pct"] for c in clusters)

    return {
        "bundler_pct": total_pct,
        "clusters": clusters,
        "wallet_count": len(wallets),
        "source": "Solana Tracker",
    }


def summarize_gmgn(
    gmgn_security: Optional[Dict[str, Any]],
    gmgn_info: Optional[Dict[str, Any]],
    gmgn_holders: Optional[List[Dict[str, Any]]],
) -> Dict[str, Any]:
    if not gmgn_info:
        return {
            "top_10_holder_pct": 0.0,
            "dev_pct": 0.0,
            "sniper_pct": 0.0,
            "bundler_pct": 0.0,
            "insider_pct": 0.0,
            "clusters": [],
            "source": None,
        }

    stat = gmgn_info.get("stat") or {}
    dev_data = gmgn_info.get("dev") or {}
    
    def get_pct(val):
        if val is None:
            return 0.0
        try:
            return float(val) * 100.0
        except Exception:
            return 0.0

    top_10_holder_pct = get_pct(stat.get("top_10_holder_rate") or dev_data.get("top_10_holder_rate") or (gmgn_security or {}).get("top_10_holder_rate"))
    dev_pct = get_pct(stat.get("dev_team_hold_rate")) + get_pct(stat.get("creator_hold_rate"))
    sniper_pct = get_pct(stat.get("top70_sniper_hold_rate"))
    bundler_pct = get_pct(stat.get("top_bundler_trader_percentage"))
    insider_pct = get_pct(stat.get("top_rat_trader_percentage")) + dev_pct
    
    # Process holders to form clusters
    clusters_dict = {}
    holders_list = gmgn_holders or []
    for h in holders_list:
        address = h.get("address") or h.get("wallet") or ""
        if not address:
            continue
        pct = (h.get("amount_percentage") or 0.0) * 100.0
        wallet_tags = h.get("tags") or []
        
        group = None
        if "dev" in wallet_tags:
            group = "dev"
        elif "bundler" in wallet_tags:
            group = "bundler"
        elif "sniper" in wallet_tags:
            group = "sniper"
        elif "rat_trader" in wallet_tags:
            group = "insider"
        elif any(t in wallet_tags for t in ["smart_degen", "bluechip_owner", "renowned"]):
            group = "smart_money"
            
        if group:
            cluster = clusters_dict.setdefault(group, {
                "id": group,
                "type": group,
                "wallet_count": 0,
                "supply_pct": 0.0,
                "wallets": [],
                "source": "GMGN top holders",
                "red_flag": False
            })
            cluster["wallet_count"] += 1
            cluster["supply_pct"] += pct
            cluster["wallets"].append(address)

    clusters = list(clusters_dict.values())
    for cluster in clusters:
        cluster["wallets"] = cluster["wallets"][:15]
        cluster["red_flag"] = cluster["supply_pct"] >= RED_FLAG_SUPPLY_PCT

    return {
        "top_10_holder_pct": top_10_holder_pct,
        "dev_pct": dev_pct,
        "sniper_pct": sniper_pct,
        "bundler_pct": bundler_pct,
        "insider_pct": insider_pct,
        "clusters": clusters,
        "source": "GMGN.ai",
    }


def summarize_holders_from_sources(
    rug_report: Optional[Dict[str, Any]],
    st_holders: Optional[Any],
    largest_accounts: List[Dict[str, Any]],
) -> Dict[str, Any]:
    holders = []
    if rug_report:
        for holder in rug_report.get("topHolders", []) or []:
            address = holder.get("owner") or holder.get("address")
            pct = _as_float(holder.get("pct"))
            if address:
                holders.append({"address": address, "pct": pct, "source": "RugCheck"})

    if not holders and st_holders:
        raw_holders = st_holders.get("holders", []) if isinstance(st_holders, dict) else st_holders
        for holder in raw_holders or []:
            address = holder.get("owner") or holder.get("wallet") or holder.get("address")
            pct = _as_float(holder.get("percentage") or holder.get("pct"))
            if address:
                holders.append({"address": address, "pct": pct, "source": "Solana Tracker"})

    if not holders and largest_accounts:
        for account in largest_accounts:
            ui_amount = _as_float(account.get("uiAmount") or account.get("amount"))
            holders.append({
                "address": account.get("address"),
                "pct": 0.0,
                "amount": ui_amount,
                "source": "RPC largest token accounts",
            })

    top_10_pct = sum(h.get("pct", 0.0) for h in holders[:10])
    whale_red_flags = [h for h in holders[:10] if h.get("pct", 0.0) >= RED_FLAG_SUPPLY_PCT]
    return {
        "holders": holders[:25],
        "top_10_pct": top_10_pct,
        "whale_red_flags": whale_red_flags,
        "source": holders[0]["source"] if holders else None,
    }


async def build_insider_report(
    mint: str,
    rpc_url: str,
    rug_report: Optional[Dict[str, Any]] = None,
    st_bundlers: Optional[Dict[str, Any]] = None,
    st_holders: Optional[Any] = None,
    gmgn_security: Optional[Dict[str, Any]] = None,
    gmgn_info: Optional[Dict[str, Any]] = None,
    gmgn_holders: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    largest_task = asyncio.create_task(fetch_largest_token_accounts(mint, rpc_url))
    rug = summarize_rugcheck(rug_report)
    st = summarize_solana_tracker(st_bundlers)
    gmgn = summarize_gmgn(gmgn_security, gmgn_info, gmgn_holders)
    largest_accounts = await largest_task
    holder_summary = summarize_holders_from_sources(rug_report, st_holders, largest_accounts)

    if gmgn["source"]:
        clusters = gmgn["clusters"]
        bundler_pct = gmgn["bundler_pct"]
        insider_pct = gmgn["insider_pct"]
        sniper_pct = gmgn["sniper_pct"]
        top_10_holder_pct = gmgn["top_10_holder_pct"]
        primary_source = "GMGN.ai"
    elif st["source"]:
        clusters = st["clusters"]
        bundler_pct = st["bundler_pct"]
        insider_pct = rug["insider_pct"]
        sniper_pct = rug["sniper_pct"]
        top_10_holder_pct = holder_summary["top_10_pct"]
        primary_source = "Solana Tracker"
    else:
        clusters = rug["clusters"]
        bundler_pct = rug["bundler_pct"]
        insider_pct = rug["insider_pct"]
        sniper_pct = rug["sniper_pct"]
        top_10_holder_pct = holder_summary["top_10_pct"]
        primary_source = "RugCheck"

    risk_level = "LOW"
    if max(insider_pct, bundler_pct, sniper_pct or 0.0) >= RED_FLAG_SUPPLY_PCT:
        risk_level = "HIGH"
    elif max(insider_pct, bundler_pct, sniper_pct or 0.0, top_10_holder_pct) >= 5.0:
        risk_level = "MEDIUM"

    return {
        "mint": mint,
        "risk_level": risk_level,
        "red_flag_threshold_pct": RED_FLAG_SUPPLY_PCT,
        "insider_pct": insider_pct,
        "bundler_pct": bundler_pct,
        "sniper_pct": sniper_pct,
        "top_10_holder_pct": top_10_holder_pct,
        "clusters": sorted(clusters, key=lambda c: c.get("supply_pct", 0.0), reverse=True),
        "holder_summary": holder_summary,
        "primary_source": primary_source,
        "sources": {
            "gmgn": bool(gmgn_info),
            "rugcheck": bool(rug_report),
            "solana_tracker": bool(st_bundlers or st_holders),
            "rpc_largest_accounts": bool(largest_accounts),
        },
    }


def format_report_lines(report: Dict[str, Any]) -> List[str]:
    sniper = report.get("sniper_pct")
    lines = [
        f"**Risk Level:** `{report['risk_level']}`",
        f"**Insider Hold:** `{report['insider_pct']:.2f}%` of supply",
        f"**Bundler Hold:** `{report['bundler_pct']:.2f}%` of supply",
        f"**Top 10 Holders:** `{report['top_10_holder_pct']:.2f}%` of supply",
    ]
    if sniper is not None:
        lines.append(f"**Sniper/Bot Hold:** `{sniper:.2f}%` of supply")
    lines.append(f"**Red Flag Rule:** `>= {report['red_flag_threshold_pct']:.0f}%` insider/bundler/sniper supply")
    return lines


def format_cluster_lines(report: Dict[str, Any], max_clusters: int = 8) -> List[str]:
    clusters = report.get("clusters") or []
    if not clusters:
        return ["No linked insider/bundler clusters were returned by the available data sources."]

    lines = []
    for idx, cluster in enumerate(clusters[:max_clusters], 1):
        flag = "RED FLAG" if cluster.get("red_flag") else "watch"
        cluster_id = cluster.get("id", idx)
        wallets = cluster.get("wallets") or []
        sample = ", ".join(f"`{_short(w)}`" for w in wallets[:3]) if wallets else "wallet list unavailable"
        lines.append(
            f"**Cluster {cluster_id}** ({cluster.get('type', 'linked')}) - "
            f"`{cluster.get('supply_pct', 0.0):.2f}%` supply, "
            f"`{cluster.get('wallet_count', 0)}` wallets, `{flag}`\n"
            f"Sample: {sample}"
        )
    return lines
