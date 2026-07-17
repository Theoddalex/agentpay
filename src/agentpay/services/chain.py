"""Ethereum layer — thin web3.py wrapper, Sepolia testnet.

Read methods (balance, gas) are always safe. The single write method (send_eth)
is the only thing that can move funds, and it is only ever called AFTER the
policy engine has returned ALLOW — never directly by the agent.

web3 is imported lazily so the pure policy layer stays dependency-free.
"""

from __future__ import annotations

from decimal import Decimal

# Minimal ERC-20 ABI — only the three methods agentpay touches. We deliberately
# do NOT include approve(): the transfer() flow moves funds directly and never
# grants an allowance, so the unlimited-approval drain vector simply doesn't
# exist on this path. A guarded approve() is a separate, later feature.
_ERC20_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function",
    },
    {
        "constant": False,
        "inputs": [
            {"name": "_to", "type": "address"},
            {"name": "_value", "type": "uint256"},
        ],
        "name": "transfer",
        "outputs": [{"name": "success", "type": "bool"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "type": "function",
    },
]


def _to_base_units(amount: Decimal, decimals: int) -> int:
    """Whole token units -> integer base units, exactly (no float).

    USDC has 6 decimals, so 50 USDC -> 50_000_000. Any fractional part finer
    than the token's precision is a bug in the caller, so we refuse it rather
    than silently truncate.
    """
    scaled = amount * (Decimal(10) ** decimals)
    if scaled != scaled.to_integral_value():
        raise ValueError(
            f"amount {amount} has more precision than the token's {decimals} decimals"
        )
    return int(scaled)


class Chain:
    def __init__(self, rpc_url: str, chain_id: int, account=None) -> None:
        from web3 import Web3

        self.w3 = Web3(Web3.HTTPProvider(rpc_url))
        self.chain_id = chain_id
        self.account = account

    def is_connected(self) -> bool:
        return self.w3.is_connected()

    def get_balance(self, address: str) -> Decimal:
        """ETH balance of an address."""
        checksum = self.w3.to_checksum_address(address)
        wei = self.w3.eth.get_balance(checksum)
        return Decimal(self.w3.from_wei(wei, "ether"))

    def gas_price_gwei(self) -> Decimal:
        return Decimal(self.w3.from_wei(self.w3.eth.gas_price, "gwei"))

    def get_token_balance(self, token_address: str, address: str, decimals: int) -> Decimal:
        """ERC-20 balance of an address, in whole token units (read-only)."""
        contract = self.w3.eth.contract(
            address=self.w3.to_checksum_address(token_address), abi=_ERC20_ABI
        )
        raw = contract.functions.balanceOf(
            self.w3.to_checksum_address(address)
        ).call()
        return Decimal(raw) / (Decimal(10) ** decimals)

    def _fees(self) -> tuple[int, int]:
        """(maxFeePerGas, maxPriorityFeePerGas), priority clamped below max."""
        max_fee = self.w3.eth.gas_price * 2
        # priority fee must never exceed max fee (invalid tx when base fee is
        # tiny, e.g. on quiet testnets); clamp it.
        priority_fee = min(self.w3.to_wei(1, "gwei"), max_fee)
        return max_fee, priority_fee

    def send_eth(self, to: str, amount_eth: Decimal) -> str:
        """Sign and broadcast an ETH transfer. Returns the tx hash.

        Precondition: caller has already cleared this with the policy engine.
        """
        if self.account is None:
            raise RuntimeError("no account loaded; cannot send")

        to_checksum = self.w3.to_checksum_address(to)
        max_fee, priority_fee = self._fees()
        tx = {
            "to": to_checksum,
            "value": self.w3.to_wei(amount_eth, "ether"),
            "nonce": self.w3.eth.get_transaction_count(self.account.address),
            "gas": 21_000,
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": priority_fee,
            "chainId": self.chain_id,
        }
        signed = self.account.sign_transaction(tx)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        return tx_hash.hex()

    def send_erc20(
        self, token_address: str, to: str, amount: Decimal, decimals: int
    ) -> str:
        """Sign and broadcast an ERC-20 transfer. Returns the tx hash.

        `amount` is in whole token units (e.g. 50 for 50 USDC); it is converted
        to base units using the token's own `decimals`. This calls transfer()
        only — no approve(), so no allowance is ever granted.

        Precondition: caller has already cleared this with the policy engine.
        """
        if self.account is None:
            raise RuntimeError("no account loaded; cannot send")

        contract = self.w3.eth.contract(
            address=self.w3.to_checksum_address(token_address), abi=_ERC20_ABI
        )
        value = _to_base_units(amount, decimals)
        max_fee, priority_fee = self._fees()
        tx = contract.functions.transfer(
            self.w3.to_checksum_address(to), value
        ).build_transaction(
            {
                "from": self.account.address,
                "nonce": self.w3.eth.get_transaction_count(self.account.address),
                "maxFeePerGas": max_fee,
                "maxPriorityFeePerGas": priority_fee,
                "chainId": self.chain_id,
            }
        )
        signed = self.account.sign_transaction(tx)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        return tx_hash.hex()
