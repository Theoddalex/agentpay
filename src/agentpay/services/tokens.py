"""Known ERC-20 tokens, keyed by chain id.

The agent (and policy.yaml) refer to tokens by symbol — "USDC" — never by
address. This registry resolves a symbol to the right contract for whatever
network the server is pointed at, so nobody copy-pastes a 42-char address into
config and no agent can smuggle an arbitrary token contract into a payment.

Addresses are the official testnet deployments (Circle's testnet USDC). Add
mainnet entries only alongside the rest of the mainnet hardening — this is a
testnet-first product.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TokenInfo:
    symbol: str
    address: str      # checksummed contract address
    decimals: int     # USDC is 6, NOT 18 — the classic footgun


# chain_id -> {symbol: TokenInfo}
KNOWN_TOKENS: dict[int, dict[str, TokenInfo]] = {
    # Base Sepolia — where most agent-payment / x402 activity actually happens.
    84532: {
        "USDC": TokenInfo("USDC", "0x036CbD53842c5426634e7929541eC2318f3dCF7e", 6),
    },
    # Ethereum Sepolia — Circle testnet USDC.
    11155111: {
        "USDC": TokenInfo("USDC", "0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238", 6),
    },
}


def token_for(chain_id: int, symbol: str) -> TokenInfo | None:
    """Resolve a token symbol for a given network, or None if it isn't known."""
    return KNOWN_TOKENS.get(chain_id, {}).get(symbol)
