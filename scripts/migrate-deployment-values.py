#!/usr/bin/env python3
"""Copy current deployment identity to new OpenBao KV v2 paths without logging it."""

import argparse
import json
import subprocess
from urllib.parse import urlparse


def run(command, payload=None):
    return subprocess.run(command, input=payload, capture_output=True, text=True)


def resource(kind, name):
    result = run(["kubectl", "get", kind, name, "-n", "default", "-o", "json"])
    if result.returncode:
        raise RuntimeError(f"Cannot read {kind}/{name}; check cluster access privately.")
    return json.loads(result.stdout)


def current_values():
    league = resource("configmap", "fantasy-league-config")["data"]
    emails = resource("configmap", "oauth2-proxy-authenticated-emails")["data"]
    deployment = resource("deployment", "oauth2-proxy")
    container = next(c for c in deployment["spec"]["template"]["spec"]["containers"]
                     if c["name"] == "oauth2-proxy")
    args = dict(arg[2:].split("=", 1) for arg in container["args"] if "=" in arg)
    redirect, domain = args["redirect-url"], args["cookie-domain"]
    if urlparse(redirect).scheme != "https" or urlparse(redirect).hostname != domain:
        raise RuntimeError("OAuth redirect URL and cookie domain must agree and use HTTPS.")
    allowlist = emails["authenticated-emails-list"]
    if not allowlist.strip() or any("@" not in line for line in allowlist.splitlines() if line.strip()):
        raise RuntimeError("The current email allow-list is empty or invalid.")
    ids = {key: league[key] for key in ("ESPN_LEAGUE_ID", "ESPN_TEAM_ID")}
    if not all(value.isdecimal() and int(value) > 0 for value in ids.values()):
        raise RuntimeError("The current league/team IDs must be positive integers.")
    return {
        "secret/fantasy/deployment": ids,
        "secret/fantasy/oauth2-proxy-deployment": {
            "authenticated-emails-list": allowlist,
            "redirect-url": redirect,
            "cookie-domain": domain,
        },
    }


def migrate(values, apply=False):
    # Validate every destination before writing either path. CLI errors can include
    # private addresses, so report only sanitized failures.
    pending = {}
    for path, expected in values.items():
        result = run(["bao", "kv", "get", "-format=json", path])
        if result.returncode:
            if "No value found" not in result.stderr + result.stdout:
                raise RuntimeError(f"Cannot read {path}; check OpenBao access privately.")
            pending[path] = expected
        elif json.loads(result.stdout)["data"]["data"] != expected:
            raise RuntimeError(f"{path} already contains different data; refusing to overwrite.")
    if apply:
        for path, expected in pending.items():
            # CAS=0 makes creation atomic and refuses any concurrent existing value.
            result = run(["bao", "kv", "put", "-cas=0", path, "-"], json.dumps(expected))
            if result.returncode:
                raise RuntimeError(f"Cannot create {path}; no existing value was overwritten.")
        for path, expected in values.items():
            result = run(["bao", "kv", "get", "-format=json", path])
            if result.returncode or json.loads(result.stdout)["data"]["data"] != expected:
                raise RuntimeError(f"Verification failed for {path}; inspect privately before merging.")
        print("Private deployment values provisioned and verified; no values printed.")
    else:
        print(f"Preflight passed: {len(pending)} new private paths needed. No writes performed.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Create and verify missing private paths")
    options = parser.parse_args()
    try:
        migrate(current_values(), options.apply)
    except (RuntimeError, KeyError, ValueError, StopIteration, OSError) as error:
        message = str(error) if isinstance(error, RuntimeError) else "Invalid source data or unavailable CLI; inspect privately."
        parser.exit(1, f"ERROR: {message}\n")


if __name__ == "__main__":
    main()
