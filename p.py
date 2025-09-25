#!/usr/bin/env python3
# coding: utf-8
"""
Kyoko Swap Bot - Revisi
Perbaikan: estimate_gas lebih robust, decode revert reason, EIP-1559 detection,
fallback gas handling, parser Kyoko response lebih tolerant.
Bahasa: Indonesian log messages.
"""

import os
import json
import time
import requests
from web3 import Web3
from dotenv import load_dotenv

load_dotenv()

def hex_or_int_to_int(x):
    """Convert possible hex string (0x...) or int/string decimal to int safely."""
    if isinstance(x, int):
        return x
    if isinstance(x, str):
        x = x.strip()
        if x.startswith("0x") or x.startswith("0X"):
            return int(x, 16)
        return int(x)
    raise ValueError("Unsupported value type for conversion to int: %r" % (type(x),))


def decode_revert_reason(data_hex: str) -> str:
    """
    Decode Solidity revert reason if present.
    Typical ABI-encoded revert: 0x08c379a0 + offset + str_len + str_bytes
    Returns decoded string or raw hex if can't decode.
    """
    try:
        if not data_hex or not isinstance(data_hex, str):
            return ""
        if data_hex.startswith("0x"):
            data_hex = data_hex[2:]
        b = bytes.fromhex(data_hex)
        if len(b) < 4:
            return ""
        selector = b[:4].hex()
        # 0x08c379a0 is Error(string) selector
        if selector != "08c379a0":
            return ""
        # offset is next 32 bytes, then length, then bytes
        # skip selector (4) + 32 offset
        if len(b) < 4 + 32 + 32:
            return ""
        str_len = int.from_bytes(b[4 + 32:4 + 32 + 32], "big")
        reason_bytes = b[4 + 32 + 32:4 + 32 + 32 + str_len]
        return reason_bytes.decode(errors="replace")
    except Exception:
        return ""


class KyokoSwapBot:
    def __init__(self,
                 private_key: str,
                 rpc_url: str,
                 kyoko_api_url: str = "https://rpc.kyo.finance/router/route",
                 router_address: str = "0xf4087AFfE358c1f267Cca84293abB89C4BD10712",
                 usdc_address: str = "0xbA9986D2381edf1DA03B0B9c1f8b00dc4AacC369",
                 weth_placeholder: str = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
                 gas_buffer_multiplier: float = 1.2,
                 dry_run: bool = False):
        self.PRIVATE_KEY = private_key
        self.RPC_URL = rpc_url
        self.KYOKO_API_URL = kyoko_api_url
        self.ROUTER_ADDRESS = router_address
        self.USDC_ADDRESS = usdc_address
        self.WETH_ADDRESS = weth_placeholder
        self.GAS_BUFFER_MULTIPLIER = float(gas_buffer_multiplier)
        self.DRY_RUN = bool(dry_run)

        # Init web3
        self.w3 = Web3(Web3.HTTPProvider(self.RPC_URL))
        if not self.w3.is_connected():
            raise Exception("❌ Gagal connect karo RPC")

        # Setup account
        self.account = self.w3.eth.account.from_key(self.PRIVATE_KEY)
        print(f"✅ Wallet: {self.account.address}")

        # Minimal USDC ABI (balanceOf, decimals)
        self.USDC_ABI = [
            {"constant": True, "inputs": [{"name": "account", "type": "address"}],
             "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
            {"constant": True, "inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}],
             "type": "function"}
        ]
        self.usdc_contract = self.w3.eth.contract(address=Web3.to_checksum_address(self.USDC_ADDRESS),
                                                  abi=self.USDC_ABI)

    def supports_eip1559(self) -> bool:
        """Detect if chain supports EIP-1559 by checking baseFeePerGas in pending/latest block."""
        try:
            blk = self.w3.eth.get_block("pending")
            return "baseFeePerGas" in blk and blk["baseFeePerGas"] is not None
        except Exception:
            # fallback try latest
            try:
                blk = self.w3.eth.get_block("latest")
                return "baseFeePerGas" in blk and blk["baseFeePerGas"] is not None
            except Exception:
                return False

    def get_quote_from_kyoko(self, amount_eth, slippage=0.01, timeout=30):
        """Request route/quote from Kyoko API with tolerant parsing."""
        print("🔄 Njaluk quote soko Kyoko API...")
        # Convert to wei
        amount_wei = self.w3.to_wei(amount_eth, "ether")
        payload = {
            "origin": self.account.address,
            "slippage": slippage,
            "constraints": [[self.WETH_ADDRESS, f"-{amount_wei}"]],
            "variable": self.USDC_ADDRESS
        }
        headers = {
            "Content-Type": "application/json",
            "Origin": "https://app.kyo.finance",
            "Referer": "https://app.kyo.finance/"
        }
        try:
            r = requests.post(self.KYOKO_API_URL, json=payload, headers=headers, timeout=timeout)
            r.raise_for_status()
            data = r.json()
            print("✅ Quote diterimo soko Kyoko API")
            return data
        except requests.exceptions.RequestException as e:
            print(f"❌ Gagal njaluk quote: {e}")
            return None

    def check_balances(self):
        eth_balance = self.w3.eth.get_balance(self.account.address)
        usdc_balance = 0
        try:
            usdc_balance = self.usdc_contract.functions.balanceOf(self.account.address).call()
            usdc_decimals = self.usdc_contract.functions.decimals().call()
            usdc_balance_formatted = usdc_balance / (10 ** usdc_decimals)
        except Exception:
            usdc_balance_formatted = None
        print(f"💎 Balance ETH: {self.w3.from_wei(eth_balance, 'ether'):.6f}")
        if usdc_balance_formatted is not None:
            print(f"💎 Balance USDC: {usdc_balance_formatted:.6f}")
        else:
            print("⚠️ Gagal baca balance USDC (cek ABI/contract address)")
        return eth_balance, usdc_balance

    def prepare_tx_from_kyoko_txdata(self, tx_data):
        """
        Normalize transaction data returned by Kyoko.
        Kyoko keys may be 'to', 'input' or 'calldata', 'value', 'gas', and optional fee fields.
        Return a dict ready for building tx.
        """
        # tolerant key access
        to_address = tx_data.get("to") or tx_data.get("to_address") or tx_data.get("toAddress")
        calldata = tx_data.get("input") or tx_data.get("calldata") or tx_data.get("data") or "0x"
        # gas and value may be hex or decimal strings
        value = tx_data.get("value", 0)
        gas = tx_data.get("gas", None)

        # fee fields
        max_fee = tx_data.get("maxFeePerGas") or tx_data.get("max_fee_per_gas")
        max_priority = tx_data.get("maxPriorityFeePerGas") or tx_data.get("max_priority_fee_per_gas")
        gas_price = tx_data.get("gasPrice") or tx_data.get("gas_price")

        # convert
        try:
            value_int = hex_or_int_to_int(value)
        except Exception:
            value_int = 0
        gas_int = None
        if gas is not None:
            try:
                gas_int = hex_or_int_to_int(gas)
            except Exception:
                gas_int = None

        max_fee_int = None
        max_priority_int = None
        gas_price_int = None
        try:
            if max_fee:
                max_fee_int = hex_or_int_to_int(max_fee)
            if max_priority:
                max_priority_int = hex_or_int_to_int(max_priority)
            if gas_price:
                gas_price_int = hex_or_int_to_int(gas_price)
        except Exception:
            pass

        return {
            "to": to_address,
            "data": calldata,
            "value": value_int,
            "gas": gas_int,
            "maxFeePerGas": max_fee_int,
            "maxPriorityFeePerGas": max_priority_int,
            "gasPrice": gas_price_int
        }

    def simulate_call_and_estimate(self, tx_for_estimate):
        """
        Try eth_call (simulate) to capture revert reason, then try estimate_gas with 'from' provided.
        Returns (estimated_gas, revert_reason) where estimated_gas may be None if estimate fails.
        """
        revert_reason = ""
        estimated_gas = None
        tx_sim = {
            "from": self.account.address,
            "to": tx_for_estimate["to"],
            "data": tx_for_estimate.get("data", "0x"),
            "value": tx_for_estimate.get("value", 0)
        }
        # First try eth_call to surface revert reason (does not change chain state)
        try:
            # eth_call may raise when contract reverts; use call to capture exception payload if any
            self.w3.eth.call(tx_sim, "latest")
        except Exception as call_exc:
            # web3 exception message sometimes contains hex revert data; try to parse it
            msg = str(call_exc)
            # attempt to find 'data' hex in message
            # common format: 'execution reverted: ...' or contains hex return data after 'revert'
            for part in msg.split():
                if part.startswith("0x") and len(part) > 10:
                    reason = decode_revert_reason(part)
                    if reason:
                        revert_reason = reason
                        break
            if not revert_reason:
                # fallback: if message contains readable reason
                if "execution reverted" in msg:
                    # extract trailing human readable part if present
                    idx = msg.find("execution reverted")
                    revert_reason = msg[idx:]
                else:
                    revert_reason = msg

        # Then try estimate_gas (with from)
        try:
            est = self.w3.eth.estimate_gas(tx_sim)
            estimated_gas = int(est)
        except Exception as est_exc:
            # try to decode revert reason from exception message
            est_msg = str(est_exc)
            for part in est_msg.split():
                if part.startswith("0x") and len(part) > 10:
                    reason = decode_revert_reason(part)
                    if reason:
                        revert_reason = revert_reason or reason
                        break
            if not revert_reason:
                if "execution reverted" in est_msg:
                    revert_reason = revert_reason or est_msg

        return estimated_gas, revert_reason

    def execute_swap(self, amount_eth, slippage=0.01):
        """Execute a single swap using Kyoko quote+transaction data"""
        print(f"💰 Lagi swap {amount_eth} ETH ke USDC...")
        quote = self.get_quote_from_kyoko(amount_eth, slippage)
        if not quote:
            print("❌ Ora iso dapet quote")
            return False

        print("📊 Quote data diterimo, menyiapkan swap...")
        # debug keys
        print(f"📋 Keys dalam response: {list(quote.keys())}")

        # get transactions array tolerant
        txs = quote.get("transactions") or quote.get("txs") or quote.get("transactions_list") or []
        if not isinstance(txs, list) or len(txs) == 0:
            print("❌ Ora ana data transaksi nang response")
            return False

        # choose first tx by default
        raw_tx = txs[0]

        tx_info = self.prepare_tx_from_kyoko_txdata(raw_tx)
        to_address = tx_info["to"]
        calldata = tx_info["data"]
        value = tx_info["value"]
        gas_from_api = tx_info["gas"] or 0

        if not to_address:
            print("❌ To address ora ditemukan di response transaksi — cek respons Kyoko")
            return False

        print(f"📍 To Address: {to_address}")
        print(f"📊 Value: {value} wei ({self.w3.from_wei(value, 'ether')} ETH)")
        if gas_from_api:
            print(f"⛽ Gas (dari API): {gas_from_api}")

        # fee fields
        max_fee = tx_info.get("maxFeePerGas")
        max_priority = tx_info.get("maxPriorityFeePerGas")
        gas_price = tx_info.get("gasPrice")

        if max_fee and max_priority:
            print(f"💰 Max Fee Per Gas: {max_fee}")
            print(f"🎯 Max Priority Fee Per Gas: {max_priority}")
        elif gas_price:
            print(f"⚠️ Gas Price from API: {gas_price}")
        else:
            if self.supports_eip1559():
                # derive sensible defaults from pending block
                try:
                    pending = self.w3.eth.get_block("pending")
                    base = pending.get("baseFeePerGas", None)
                    if base:
                        # set maxPriority small gwei
                        default_priority = self.w3.to_wei("1", "gwei")
                        max_priority = default_priority
                        max_fee = int(base * 2 + default_priority)
                        print(f"ℹ️ EIP-1559 detected, using baseFee estimate. maxFee={max_fee}, maxPriority={max_priority}")
                except Exception:
                    pass
            else:
                gas_price = self.w3.eth.gas_price
                print(f"⚠️ Using node gasPrice: {gas_price}")

        # Build transaction template for estimation/sending
        tx_template = {
            "to": Web3.to_checksum_address(to_address),
            "data": calldata,
            "value": int(value)
        }

        # Simulate + estimate gas
        est_gas, revert_reason = self.simulate_call_and_estimate(tx_template)
        if est_gas:
            print(f"⛽ Estimated gas (node): {est_gas}")
            gas_to_use = int(max(gas_from_api or 0, est_gas) * self.GAS_BUFFER_MULTIPLIER)
        else:
            # estimation failed -> show revert reason if any, fallback to API gas if present
            if revert_reason:
                print(f"⚠️ Ora iso estimate gas: {revert_reason}")
            else:
                print("⚠️ Ora iso estimate gas: unknown reason")
            if gas_from_api and gas_from_api > 0:
                gas_to_use = int(gas_from_api * self.GAS_BUFFER_MULTIPLIER)
                print(f"ℹ️ Fallback pake gas dari API (dengan buffer {self.GAS_BUFFER_MULTIPLIER}x): {gas_to_use}")
            else:
                print("❌ Gagal dapet gas sama sekali (ora ana gas di API lan ora iso estimate). Batal.")
                return False

        # Build final tx dict with appropriate fee fields
        tx_final = {
            "to": tx_template["to"],
            "value": tx_template["value"],
            "gas": gas_to_use,
            "data": calldata,
            "nonce": self.w3.eth.get_transaction_count(self.account.address),
            "chainId": self.w3.eth.chain_id,
            # include 'from' only for simulation/estimate; not required in signed tx payload
        }

        # If EIP-1559 supported or API provided fields, use them
        if max_fee and max_priority:
            tx_final["maxFeePerGas"] = int(max_fee)
            tx_final["maxPriorityFeePerGas"] = int(max_priority)
            tx_final["type"] = 2
        elif gas_price:
            tx_final["gasPrice"] = int(gas_price)
            tx_final["type"] = 0  # legacy
        else:
            # fallback to node gas_price (legacy) if EIP-1559 not used
            if self.supports_eip1559():
                # Already set max_fee earlier if possible; if still not set, derive conservative defaults
                try:
                    pending = self.w3.eth.get_block("pending")
                    base = pending.get("baseFeePerGas", 0)
                    default_priority = self.w3.to_wei("1", "gwei")
                    tx_final["maxPriorityFeePerGas"] = default_priority
                    tx_final["maxFeePerGas"] = int(base * 2 + default_priority)
                    tx_final["type"] = 2
                    print(f"ℹ️ Fallback EIP-1559 fees: maxFeePerGas={tx_final['maxFeePerGas']}, maxPriorityFeePerGas={tx_final['maxPriorityFeePerGas']}")
                except Exception:
                    tx_final["gasPrice"] = self.w3.eth.gas_price
                    tx_final["type"] = 0
            else:
                tx_final["gasPrice"] = self.w3.eth.gas_price
                tx_final["type"] = 0

        print(f"🔢 Nonce: {tx_final['nonce']}")
        print(f"⛽ Gas to send: {tx_final['gas']}")

        if revert_reason:
            # Helpful suggestion for common revert
            if "transfer to the zero address" in revert_reason.lower():
                print("❗ Revert reason menunjukkan 'transfer to the zero address'.")
                print("  -> Periksa apakah address token (USDC/WETH) benar dan bukan placeholder.")
                print("  -> Pastikan Kyoko route tidak memasukkan address 0x000... sebagai target.")
            print(f"⚠️ Revert reason (simulasi): {revert_reason}")

        if self.DRY_RUN:
            print("🔎 Dry-run mode aktif — transaksi TIDAK dikirim. Berikut preview tx_final:")
            print(json.dumps({k: (v if not isinstance(v, bytes) else v.hex()) for k, v in tx_final.items()}, default=str, indent=2))
            return True

        # Sign and send
        try:
            signed = self.w3.eth.account.sign_transaction(tx_final, self.PRIVATE_KEY)
            tx_hash = self.w3.eth.send_raw_transaction(signed.rawTransaction)
            print(f"📝 Swap transaction: {tx_hash.hex()}")
            print("⏳ Nunggu konfirmasi...")
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
            if receipt and receipt.status == 1:
                print(f"✅ Swap sukses di block: {receipt['blockNumber']}")
                print(f"💸 Gas used: {receipt['gasUsed']}")
                self.check_balances()
                return True
            else:
                print("❌ Swap gagal - status transaksi 0")
                print("Receipt:", receipt)
                return False
        except Exception as send_exc:
            # decode revert if present in exception
            exc_msg = str(send_exc)
            found_reason = ""
            for part in exc_msg.split():
                if part.startswith("0x") and len(part) > 10:
                    r = decode_revert_reason(part)
                    if r:
                        found_reason = r
                        break
            if found_reason:
                print(f"❌ Error ngirim transaksi: {found_reason}")
            else:
                print(f"❌ Error ngirim transaksi: {send_exc}")
            return False

    def run_swap_bot_cli(self):
        print("🤖 Kyoko Swap Bot - Miwiti...")
        print("=" * 50)
        self.check_balances()

        try:
            amount_eth = float(input("📝 Ketik jumlah ETH yang arep diswap: ").strip())
        except Exception:
            print("❌ Input ora bener, ketik angka")
            return

        if amount_eth <= 0:
            print("❌ Amount kudu lebih soko 0")
            return

        eth_balance, _ = self.check_balances()
        if amount_eth > float(self.w3.from_wei(eth_balance, "ether")):
            print("❌ Balance ETH kurang")
            return

        try:
            loop_count = int(input("📝 Ketik jumlah loop (berapa kali swap): ").strip())
        except Exception:
            print("❌ Input ora bener, ketik angka")
            return

        if loop_count <= 0:
            print("❌ Loop count kudu lebih soko 0")
            return

        try:
            dr = input("🔬 Dry-run mode? (y/n): ").strip().lower()
            if dr == "y":
                self.DRY_RUN = True
        except Exception:
            pass

        slippage = 0.01
        print(f"\n🔁 Konfigurasi Swap:")
        print(f"   Jumlah ETH per swap: {amount_eth}")
        print(f"   Jumlah loop: {loop_count}")
        print(f"   Slippage: {slippage * 100}%")
        print(f"   From: ETH")
        print(f"   To: USDC")
        print("=" * 50)

        confirm = input("🚀 Arep lanjut swap? (y/n): ").lower()
        if confirm != "y":
            print("❌ Swap dibatalno")
            return

        successful = 0
        for i in range(loop_count):
            print(f"\n🔄 Loop ke-{i+1} saka {loop_count}")
            print("-" * 30)
            eth_balance_now = self.w3.from_wei(self.w3.eth.get_balance(self.account.address), "ether")
            if amount_eth > eth_balance_now:
                print(f"❌ Balance ETH kurang. dibutuhake: {amount_eth}, ana: {eth_balance_now}")
                break
            ok = self.execute_swap(amount_eth, slippage)
            if ok:
                successful += 1
            else:
                print(f"❌ Swap gagal nang loop ke-{i+1}")
                # small delay then continue
                time.sleep(5)
                continue
            if i < loop_count - 1:
                wait_time = 10
                print(f"⏳ Tunggu {wait_time} detik sebelum swap berikutnya...")
                time.sleep(wait_time)

        print(f"\n🎉 Swap rampung! Sukses: {successful} saka {loop_count}")


def main():
    if not os.getenv("PRIVATE_KEY"):
        print("❌ PRIVATE_KEY ra ono nang file .env!")
        return
    if not os.getenv("RPC_URL"):
        print("❌ RPC_URL ra ono nang file .env!")
        return

    private_key = os.getenv("PRIVATE_KEY")
    rpc_url = os.getenv("RPC_URL")
    kyoko_api = os.getenv("KYOKO_API_URL") or "https://rpc.kyo.finance/router/route"
    dry = os.getenv("DRY_RUN", "false").lower() in ("1", "true", "y", "yes")

    bot = KyokoSwapBot(
        private_key=private_key,
        rpc_url=rpc_url,
        kyoko_api_url=kyoko_api,
        dry_run=dry,
        gas_buffer_multiplier=float(os.getenv("GAS_BUFFER_MULTIPLIER", "1.2"))
    )
    try:
        bot.run_swap_bot_cli()
    except KeyboardInterrupt:
        print("\n🛑 Bot ditokno")
    except Exception as e:
        print(f"💥 Error umum: {e}")


if __name__ == "__main__":
    main()
