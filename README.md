# banknote-public

Published data for [banknote.lol](https://banknote.lol), and the scheduled jobs
that post to [@banknotelolai](https://x.com/banknotelolai).

Everything here is generated. Files are overwritten on each refresh, so changes
made by hand do not survive.

## What this repo is not

It holds no wallet, no signing key, and no contract code. Nothing in it can move
funds, change an on-chain value, or alter anything the protocol depends on.
Reads of the chain and writes to it happen elsewhere.

## Contents

| Path | |
|---|---|
| `feeds/`, `news/`, `points/` | data the site fetches at page load |
| `oracle/` | the scheduled jobs and the data they read |
| `data/` | reference tables |
