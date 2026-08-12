import json
import os
import shutil


def foundry_bin(name):
    on_path = shutil.which(name)
    if on_path:
        return on_path
    for ext in (".exe", ""):
        p = os.path.expanduser(f"~/.foundry/bin/{name}{ext}")
        if os.path.exists(p):
            return p
    return name


_NETWORKS = {
    "testnet": {
        "rpc": "https://rpc.testnet.chain.robinhood.com",
        "forge_alias": "robinhood_testnet",
        "deployments": "testnet.json",
    },
    "mainnet": {
        "rpc": "https://rpc.mainnet.chain.robinhood.com",
        "forge_alias": "robinhood_mainnet",
        "deployments": "mainnet.json",
    },
}


def net_config():
    net = os.environ.get("BANKNOTE_NETWORK", "testnet").strip().lower()
    if net not in _NETWORKS:
        net = "testnet"
    cfg = dict(_NETWORKS[net])
    cfg["network"] = net
    cfg["rpc"] = os.environ.get("BANKNOTE_RPC", cfg["rpc"])
    cfg["forge_alias"] = os.environ.get("BANKNOTE_FORGE_ALIAS", cfg["forge_alias"])
    cfg["deployments"] = os.environ.get("BANKNOTE_DEPLOYMENTS", cfg["deployments"])
    return cfg


def write_json_atomic(path, obj, **dump_kwargs):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, **dump_kwargs)
    os.replace(tmp, path)
