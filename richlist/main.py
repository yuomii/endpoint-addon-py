import json
from threading import Thread
from typing import Optional, Union

import uvicorn
from fastapi import FastAPI

from richlist.endpoint import SHARED_MEMORY_DICT, API_ROUTER
from utils.rest import get_blocks, get_all_validators, get_balance, get_profile, get_tokens
from utils.exception import DelegationDoesNotExist, ValidatorDoesNotExist, RequestTimedOut, NodeIsCatchingUp
from utils import create_sub_dir, load_files, get_file_logger, save_file
from utils.cosmos import (get_validator_delegations,
                          get_delegator_delegations,
                          get_delegator_unbonding_delegations,
                          get_delegator_distribution,
                          get_validator_distribution)
import time
import os


DATABASE_PATH = ["database", "richlist", "wallet"]

SECONDS_BETWEEN_BLOCK_FETCH = float(os.getenv("SECONDS_BETWEEN_BLOCK_FETCH")) if os.getenv("SECONDS_BETWEEN_BLOCK_FETCH") else 10
MAX_BLOCK_SPREAD_UPDATE_WALLET = float(os.getenv("MAX_BLOCK_SPREAD_UPDATE_WALLET")) if os.getenv("MAX_BLOCK_SPREAD_UPDATE_WALLET") else 2000
MAX_BLOCK_SPREAD_FETCH_SOURCES = float(os.getenv("MAX_BLOCK_SPREAD_FETCH_SOURCES")) if os.getenv("MAX_BLOCK_SPREAD_FETCH_SOURCES") else 5000

# In memory storage for currently loaded wallets
WALLETS = {}
# In memory storage for token information(used by staking)
TOKENS = {}

LOG_LEVEL_TERMINAL = os.getenv("LOG_LEVEL_TERMINAL") or "INFO"
LOG_LEVEL_FILE = os.getenv("LOG_LEVEL_FILE") or "INFO"

LOGGER = get_file_logger("richlist")


def main():
    global DATABASE_PATH, SECONDS_BETWEEN_BLOCK_FETCH
    # create database directories if not created yet
    # returns the abs path to directory
    wallet_db: str = create_sub_dir(DATABASE_PATH)
    load_wallets(wallet_db)
    last_full_validator_check: int = get_lowest_block_height()
    LOGGER.info(f"Loaded {len(WALLETS.keys())} wallets, lowest block height: {last_full_validator_check}")
    LOGGER.info(f"ENVIRONMENT {LOG_LEVEL_TERMINAL} {LOG_LEVEL_FILE}")
    while True:
        try:
            block: dict = get_blocks(limit=1)[0]
            block_height: int = int(block['block_height'])
            block_time: str = block['time']
            LOGGER.info(f"Current block {block_height} - {block_time}")
            if block_height - last_full_validator_check > MAX_BLOCK_SPREAD_FETCH_SOURCES:
                request_validators()
                last_full_validator_check = block_height

            update_wallets = []
            for wallet in WALLETS.values():
                difference: int = block_height - wallet["last_checked_height"]
                if difference > MAX_BLOCK_SPREAD_UPDATE_WALLET:
                    update_wallets.append(wallet)
            LOGGER.info(f"Found {len(update_wallets)} wallets to update")
            for i, wallet in enumerate(update_wallets):
                update_wallet(wallet)
                wallet["last_checked_height"] = block_height
                wallet["last_checked_time"] = block_time
                save_wallet(wallet_db, wallet)
                LOGGER.info(f"Updated {i+1}/{len(update_wallets)} wallets.")

            update_rich_list_per_coin()

            time.sleep(SECONDS_BETWEEN_BLOCK_FETCH)
        except RequestTimedOut:
            LOGGER.warning(f"Request timed out, wait 30sec and retry.")
            time.sleep(30)
        except NodeIsCatchingUp:
            LOGGER.warning(f"Node is catching up, wait 60sec")
            time.sleep(60)


def update_rich_list_per_coin():
    global TOKENS, SHARED_MEMORY_DICT

    # copy all wallets so the update process does not make problems if endpoint is requested
    wallets = [wallet.copy() for wallet in WALLETS.values()]

    wallets_per_coin = {}

    # build dict for each coin containing all wallets owning this coin
    for wallet in wallets:
        for coin in wallet["balance"].keys():
            if coin not in wallets_per_coin.keys():
                wallets_per_coin[coin] = []

            wallets_per_coin[coin].append(wallet)

    for coin in wallets_per_coin:
        SHARED_MEMORY_DICT[coin] = sorted(wallets_per_coin[coin], key=lambda entry: float(entry["balance"][coin]["total"]), reverse=True)
        LOGGER.info(f"Updated richlist for coin '{coin}'. Wallets: {len(SHARED_MEMORY_DICT[coin])}")


def load_wallets(path: str) -> None:
    """
    Load all wallets into global WALLETS and return the lowest checked block height
    :param path:
    :return:
    """
    for data in load_files(path, ".json"):
        wallet: dict = json.loads(data)
        WALLETS[wallet["address"]] = wallet
        LOGGER.info(f"Loaded wallet: {wallet['address']}")


def save_wallet(path: str, wallet: dict):
    data: str = json.dumps(wallet, indent=4)
    save_file(path, f"{wallet['address']}.json", data)


def get_lowest_block_height():
    sorted_by_block_height: list = sorted(WALLETS.values(), key=lambda entry: entry["last_checked_height"])
    if sorted_by_block_height:
        return sorted_by_block_height[0]["last_checked_height"]
    return 0


def get_wallet(swth_address: str):
    if swth_address not in WALLETS.keys():
        WALLETS[swth_address] = {
            "address": swth_address,
            "last_seen_time": None,
            "last_seen_height": 0,
            "last_checked_time": None,
            "last_checked_height": 0,
            "username": None,
            "validator": None,
            "balance": {

            }
        }
    return WALLETS[swth_address]


def set_wallet_balance(wallet: dict,
                       denom: str,
                       available: Optional[float] = None,
                       staking: Optional[float] = None,
                       unbonding: Optional[float] = None,
                       rewards: Optional[float] = None,
                       commission: Optional[float] = None,
                       orders: Optional[float] = None,
                       positions: Optional[float] = None):
    if denom not in wallet["balance"].keys():
        wallet["balance"][denom] = {
            "available": "0.0",
            "staking": "0.0",
            "unbonding": "0.0",
            "rewards": "0.0",
            "commission": "0.0",
            "orders": "0.0",
            "positions": "0.0",
            "total": "0.0"
        }

    if available is not None:
        wallet["balance"][denom]["available"] = add_floats_to_str(denom, available, 0.0)

    if staking is not None:
        wallet["balance"][denom]["staking"] = add_floats_to_str(denom, staking, 0.0)

    if unbonding is not None:
        wallet["balance"][denom]["unbonding"] = add_floats_to_str(denom, unbonding, 0.0)

    if rewards is not None:
        wallet["balance"][denom]["rewards"] = add_floats_to_str(denom, rewards, 0.0)

    if commission is not None:
        wallet["balance"][denom]["commission"] = add_floats_to_str(denom, commission, 0.0)

    if orders is not None:
        wallet["balance"][denom]["orders"] = add_floats_to_str(denom, orders, 0.0)

    if positions is not None:
        wallet["balance"][denom]["positions"] = add_floats_to_str(denom, positions, 0.0)

    # update total
    keys = list(wallet["balance"][denom].keys())
    keys.remove("total")
    total: str = "0.0"
    for key in keys:
        total = add_floats_to_str(denom, total, wallet["balance"][denom][key])
    wallet["balance"][denom]["total"] = total


def request_validators():
    json_validators = get_all_validators()
    LOGGER.info(f"Found {len(json_validators)} Validators in total")
    for json_val in json_validators:
        wallet_address = json_val["WalletAddress"]
        moniker = json_val["Description"]["moniker"]
        swthval_address = json_val["OperatorAddress"]
        validator = get_wallet(wallet_address)
        validator["validator"] = swthval_address
        validator["username"] = moniker
        delegations = get_validator_delegations(swthval_address)["result"]
        LOGGER.info(f"Validator {moniker} with wallet {wallet_address} has {len(delegations)} delegators")
        for delegator in delegations:
            swth_address = delegator["delegator_address"]
            # use get wallet to initialize wallet if not existed yet
            get_wallet(swth_address)
    LOGGER.info(f"Total fetched wallets via staking: {len(WALLETS.values())}")


def update_wallet(wallet: dict):
    try:
        LOGGER.info(f"Start updating {wallet['address']}")

        # Rest balance
        wallet["balance"] = {}

        update_delegator_balance(wallet)

        update_delegations(wallet)

        update_wallet_info(wallet)

        update_delegator_unbonding_delegation(wallet)
        if wallet["validator"]:
            update_validator_distribution(wallet)
        else:
            update_delegator_distribution(wallet)

    except RequestTimedOut:
        LOGGER.info(f"Request timed out while updating delegator: {wallet['address']}. Wait 30sec and continue")
        time.sleep(30)
        update_wallet(wallet)
    except (DelegationDoesNotExist, ValidatorDoesNotExist):
        LOGGER.info(f"Validator {wallet['username']} has no more delegations. Treat them as usual wallet.")
        wallet["validator"] = None
        wallet["username"] = None
        update_wallet(wallet)
    except NodeIsCatchingUp:
        LOGGER.info(f"Node is catching up while updating wallet: {wallet['address']}. Wait 60sec and continue")
        time.sleep(60)
        update_wallet(wallet)


def update_delegator_balance(wallet: dict):
    balance = get_balance(wallet["address"])

    if balance:
        for coin in balance.values():
            denom: str = coin["denom"]
            available: float = float(coin["available"])
            order: float = float(coin["order"])
            position: float = float(coin["position"])
            set_wallet_balance(wallet, denom, available=available, orders=order, positions=position)


def update_delegations(wallet: dict):

    delegations: dict = get_delegator_delegations(wallet["address"])
    totals: dict = {}
    for delegation_json in delegations["result"]:
        denom: str = delegation_json["balance"]["denom"]
        if denom not in totals.keys():
            totals[denom] = 0.0
        amount: float = big_float_to_real_float(denom, float(delegation_json["balance"]["amount"]))
        totals[denom] += amount

    for denom in totals.keys():
        set_wallet_balance(wallet, denom, staking=totals[denom])


def update_wallet_info(wallet: dict):

    info = get_profile(wallet["address"])

    if info["username"]:
        wallet["username"] = info["username"]

    wallet["last_seen_height"] = int(info["last_seen_block"])
    wallet["last_seen_time"] = info["last_seen_time"]


def update_delegator_unbonding_delegation(wallet: dict):
    unbonding = get_delegator_unbonding_delegations(wallet["address"])
    total: float = 0.0
    for unbond_process in unbonding["result"]:
        for i in range(len(unbond_process["entries"])):
            # TODO no info about denom in response
            denom: str = "swth"
            amount: float = big_float_to_real_float(denom, float(unbond_process["entries"][i]["balance"]))
            total += amount
    set_wallet_balance(wallet, denom="swth", unbonding=total)


def update_validator_distribution(wallet: dict):
    commission = get_validator_distribution(wallet["validator"])
    if "result" in commission.keys():
        if commission["result"]:
            if "self_bond_rewards" in commission["result"].keys():
                for token in commission["result"]["self_bond_rewards"]:
                    denom: str = token["denom"]
                    amount: float = big_float_to_real_float(denom, float(token["amount"]))
                    set_wallet_balance(wallet, denom, rewards=amount)
            if "val_commission" in commission["result"].keys():
                for token in commission["result"]["val_commission"]:
                    denom: str = token["denom"]
                    amount: float = big_float_to_real_float(denom, float(token["amount"]))
                    set_wallet_balance(wallet, denom, commission=amount)


def update_delegator_distribution(wallet: dict):
    rewards = get_delegator_distribution(wallet["address"])
    if rewards["result"]["total"]:
        for denom_dict in rewards["result"]["total"]:
            denom: str = denom_dict["denom"]
            amount: float = big_float_to_real_float(denom, float(denom_dict["amount"]))
            set_wallet_balance(wallet, denom, rewards=amount)


def add_floats_to_str(denom: str, number_1: Union[str, float], number_2: Union[str, float]):
    if isinstance(number_1, str):
        number_1: float = float(number_1)

    if isinstance(number_2, str):
        number_2: float = float(number_2)

    number: float = number_1 + number_2
    decimals: int = get_denom_decimals(denom)
    return ("%%.%df" % decimals) % number


def get_denom_decimals(denom: str):
    global TOKENS
    if not TOKENS:
        response = get_tokens()
        for token in response:
            asset: str = token["denom"]
            TOKENS[asset] = token
    if denom not in TOKENS.keys():
        raise RuntimeError(f"Could not find token info about {denom}")

    return TOKENS[denom]["decimals"]


def big_float_to_real_float(denom: str, amount: float):
    decimals = get_denom_decimals(denom)
    return amount / pow(10, decimals)


if __name__ == "__main__":
    global SHARED_MEMORY_DICT
    SHARED_MEMORY_DICT["delegators"] = []
    Thread(target=main).start()
    app = FastAPI()
    app.include_router(API_ROUTER, prefix="/richlist", tags=["Richlist"])
    uvicorn.run(app, host="0.0.0.0", port=8001, loop="asyncio")