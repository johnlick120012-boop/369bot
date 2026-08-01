import os
import logging
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Clean up environment variables (strip outer quotes, newlines, and trailing spaces)
for key, value in list(os.environ.items()):
    if value:
        os.environ[key] = value.strip().strip('"').strip("'").strip()


# Logger configuration
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8")
    ]
)
logger = logging.getLogger("MemecoinBot")

# Discord Configuration
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
COMMAND_PREFIX = os.getenv("COMMAND_PREFIX", "!")

# GeckoTerminal and DexScreener APIs
GECKOTERMINAL_API_URL = "https://api.geckoterminal.com/api/v2"
DEXSCREENER_API_URL = "https://api.dexscreener.com/latest/dex"

# Chain Configurations (Name, Color, Explorer/Chart base, Quick-buy DEX)
CHAINS = {
    "solana": {
        "name": "Solana",
        "color": 0x9945FF,  # Purple
        "icon": "🟣",
        "chart_url": "https://dexscreener.com/solana/",
        "buy_url": "https://jup.ag/swap/SOL-"
    },
    "base": {
        "name": "Base",
        "color": 0x0052FF,  # Blue
        "icon": "🔵",
        "chart_url": "https://dexscreener.com/base/",
        "buy_url": "https://app.uniswap.org/#/swap?outputCurrency="
    },
    "bsc": {
        "name": "BNB Chain",
        "color": 0xF3BA2F,  # Gold/Yellow
        "icon": "🟡",
        "chart_url": "https://dexscreener.com/bsc/",
        "buy_url": "https://pancakeswap.finance/swap?outputCurrency="
    },
    "robinhood": {
        "name": "Robinhood",
        "color": 0x00C805,  # Robinhood Green
        "icon": "🟢",
        "chart_url": "https://dexscreener.com/robinhood/",
        "buy_url": None
    },
    "ethereum": {
        "name": "Ethereum",
        "color": 0x627EEA,  # Slate Blue
        "icon": "⚫",
        "chart_url": "https://dexscreener.com/ethereum/",
        "buy_url": "https://app.uniswap.org/#/swap?outputCurrency="
    },
    "polygon": {
        "name": "Polygon",
        "color": 0x8247E5,  # Purple/Violet
        "icon": "🟣",
        "chart_url": "https://dexscreener.com/polygon/",
        "buy_url": "https://quickswap.exchange/#/swap?outputCurrency="
    },
    "arbitrum": {
        "name": "Arbitrum",
        "color": 0x28A0F0,  # Light Blue
        "icon": "🔵",
        "chart_url": "https://dexscreener.com/arbitrum/",
        "buy_url": "https://app.uniswap.org/#/swap?outputCurrency="
    },
    "optimism": {
        "name": "Optimism",
        "color": 0xFF0420,  # Red
        "icon": "🔴",
        "chart_url": "https://dexscreener.com/optimism/",
        "buy_url": "https://app.uniswap.org/#/swap?outputCurrency="
    },
    "avalanche": {
        "name": "Avalanche",
        "color": 0xE84142,  # Red/White
        "icon": "🔺",
        "chart_url": "https://dexscreener.com/avalanche/",
        "buy_url": "https://traderjoexyz.com/trade?outputCurrency="
    }
}

DEFAULT_COLOR = 0xFF007A  # Hot Pink

# Helper to get chain config by ID/name
def get_chain_config(chain_id: str):
    if not chain_id:
        return {"name": "Unknown", "color": DEFAULT_COLOR, "icon": "❓", "chart_url": "https://dexscreener.com/", "buy_url": None}
    chain_lower = chain_id.lower()
    # Handle aliases
    aliases = {
        "sol": "solana",
        "bnb": "bsc",
        "eth": "ethereum",
        "poly": "polygon",
        "arb": "arbitrum",
        "op": "optimism",
        "avax": "avalanche"
    }
    target = aliases.get(chain_lower, chain_lower)
    return CHAINS.get(target, {
        "name": chain_id.capitalize(),
        "color": DEFAULT_COLOR,
        "icon": "🪙",
        "chart_url": f"https://dexscreener.com/{chain_id}/",
        "buy_url": None
    })
