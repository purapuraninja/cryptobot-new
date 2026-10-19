"""
tools/probe_exchange_access.py — can this machine actually reach the exchange?

Answers one question with evidence instead of guesswork: from THIS host and
THIS IP, will Bybit / OKX accept our API calls?

Written because jurisdiction lists go stale and "country X is restricted" is
unreliable trivia. Restrictions move, ISPs hijack DNS, and cloud regions get
geo-blocked without announcement. Measure, do not assume.

Four independent gates, each of which can fail alone:

  1. DNS      — does the hostname resolve, and to a plausible address?
                Several exchange hostnames collapsing onto one local IP is the
                signature of ISP-level DNS hijacking.
  2. TLS      — does the certificate actually validate for that hostname?
                A hostname mismatch means something is terminating TLS in the
                middle. Never "fix" this by disabling verification: that hands
                your API keys to whoever is intercepting.
  3. PUBLIC   — does an unauthenticated market-data endpoint return data?
                Geo-blocks often appear here as HTTP 403.
  4. PRIVATE  — does a SIGNED endpoint get accepted?
                This is the only gate that proves the exchange will let this
                IP trade. Public data frequently works from regions where
                trading is refused, so gates 1-3 passing means nothing on
                their own.

Usage
    python tools/probe_exchange_access.py                 # public gates only
    python tools/probe_exchange_access.py --private       # + signed endpoints
    python tools/probe_exchange_access.py --exchange okx
    python tools/probe_exchange_access.py --json          # machine-readable

Credentials for --private come from the environment or .env via config.Config
when importable, so this file can also be scp'd to a bare probe box and run
with plain env vars.

Exit codes: 0 = every attempted gate passed, 1 = at least one failed.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import socket
import ssl
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

TIMEOUT = 12

# Public IP lookup services, tried in order. Knowing the egress IP is what
# makes a "works from region X" claim checkable later.
_IP_SERVICES = (
    "https://checkip.amazonaws.com",
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
)


class Probe:
    """One exchange's endpoint set and signing scheme."""

    name = "?"
    host = "?"
    base = "?"
    public_path = "?"
    private_path = "?"

    def sign(self, method: str, path: str, body: str = "") -> Optional[Dict[str, str]]:
        raise NotImplementedError

    @staticmethod
    def creds_present() -> bool:
        raise NotImplementedError


def _cfg(name: str, *fallbacks: str) -> str:
    """Read a credential from Config if importable, else from the environment.

    Config is preferred because it already loads .env, but this tool must also
    work on a bare box where the project is not installed.
    """
    try:
        from config import Config   # type: ignore
        val = getattr(Config, name, "") or ""
        if val:
            return str(val)
    except Exception:
        pass
    for key in (name, *fallbacks):
        val = os.environ.get(key, "")
        if val:
            return val
    return ""


class BybitProbe(Probe):
    name = "bybit"
    host = "api.bybit.com"
    base = "https://api.bybit.com"
    public_path = "/v5/market/tickers?category=linear&symbol=BTCUSDT"
    # Wallet balance needs a valid key AND an allowed region, so it separates
    # "key is wrong" from "region is refused".
    private_path = "/v5/account/wallet-balance?accountType=UNIFIED"

    @staticmethod
    def creds_present() -> bool:
        return bool(_cfg("BYBIT_API_KEY") and _cfg("BYBIT_SECRET"))

    def sign(self, method: str, path: str, body: str = "") -> Optional[Dict[str, str]]:
        key, secret = _cfg("BYBIT_API_KEY"), _cfg("BYBIT_SECRET")
        if not (key and secret):
            return None
        ts = str(int(time.time() * 1000))
        recv = "5000"
        query = path.split("?", 1)[1] if "?" in path else ""
        payload = f"{ts}{key}{recv}{query if method == 'GET' else body}"
        sig = hmac.new(
            secret.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        return {
            "X-BAPI-API-KEY": key,
            "X-BAPI-SIGN": sig,
            "X-BAPI-SIGN-TYPE": "2",
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": recv,
        }


class OKXProbe(Probe):
    name = "okx"
    host = "www.okx.com"
    base = "https://www.okx.com"
    public_path = "/api/v5/market/ticker?instId=BTC-USDT-SWAP"
    private_path = "/api/v5/account/balance?ccy=USDT"

    @staticmethod
    def creds_present() -> bool:
        return bool(
            _cfg("OKX_API_KEY", "OK_ACCESS_KEY")
            and _cfg("OKX_SECRET_KEY", "OK_ACCESS_SECRET")
            and _cfg("OKX_PASSPHRASE", "OK_ACCESS_PASSPHRASE")
        )

    def sign(self, method: str, path: str, body: str = "") -> Optional[Dict[str, str]]:
        key = _cfg("OKX_API_KEY", "OK_ACCESS_KEY")
        secret = _cfg("OKX_SECRET_KEY", "OK_ACCESS_SECRET")
        phrase = _cfg("OKX_PASSPHRASE", "OK_ACCESS_PASSPHRASE")
        if not (key and secret and phrase):
            return None
        ts = (
            datetime.now(timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        )
        # OKX signs the path WITH its query string, and the body exactly as sent.
        msg = f"{ts}{method.upper()}{path}{body}"
        sig = base64.b64encode(
            hmac.new(secret.encode(), msg.encode(), hashlib.sha256).digest()
        ).decode()
        headers = {
            "OK-ACCESS-KEY": key,
            "OK-ACCESS-SIGN": sig,
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": phrase,
        }
        project = _cfg("OKX_PROJECT_ID", "OK_ACCESS_PROJECT")
        if project:
            headers["OK-ACCESS-PROJECT"] = project
        # Demo trading is a header on the production host for OKX. Probing demo
        # tells you nothing about live geo-access, so it is deliberately OFF.
        return headers


PROBES: Dict[str, Probe] = {"bybit": BybitProbe(), "okx": OKXProbe()}


# ── Gate 1: DNS ────────────────────────────────────────────────────────────

def check_dns(host: str) -> Dict:
    out: Dict = {"gate": "dns", "host": host}
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        ips = sorted({i[4][0] for i in infos})
        out["ips"] = ips
        out["ok"] = bool(ips)
        if not ips:
            out["detail"] = "resolved to no addresses"
    except Exception as exc:
        out["ok"] = False
        out["ips"] = []
        out["detail"] = f"{type(exc).__name__}: {exc}"
    return out


# ── Gate 2: TLS ────────────────────────────────────────────────────────────

# DER tag bytes for the two attributes worth naming an interceptor by.
_OID_CN = b"\x06\x03\x55\x04\x03"    # 2.5.4.3  commonName
_OID_ORG = b"\x06\x03\x55\x04\x0a"   # 2.5.4.10 organizationName
_STRING_TAGS = {0x0C, 0x13, 0x16, 0x1E}  # UTF8 / Printable / IA5 / BMP


def _der_values(der: bytes, oid: bytes) -> List[str]:
    """Pull every string value following `oid` in a DER blob.

    Deliberately a byte scan, not a real X.509 parser: the only job is to name
    whoever presented an invalid certificate, and pulling in a crypto
    dependency would stop this tool from running on a bare probe box.
    Best-effort by design — a miss degrades to "unreadable", never a crash.
    """
    out: List[str] = []
    idx = 0
    while True:
        found = der.find(oid, idx)
        if found == -1:
            return out
        pos = found + len(oid)
        if pos + 2 <= len(der) and der[pos] in _STRING_TAGS:
            length = der[pos + 1]
            start = pos + 2
            if length < 0x80 and start + length <= len(der):
                try:
                    out.append(der[start:start + length].decode("utf-8", "replace"))
                except Exception:
                    pass
        idx = found + 1


def _names_from_der(der: Optional[bytes]) -> Dict[str, str]:
    """Best-effort {presented_cn, presented_issuer} from raw certificate bytes.

    In an X.509 cert the issuer Name precedes the subject Name, so the first
    commonName belongs to the issuer and the last to the subject.
    """
    if not der:
        return {"presented_cn": "unreadable"}
    cns = _der_values(der, _OID_CN)
    orgs = _der_values(der, _OID_ORG)
    return {
        "presented_cn": cns[-1] if cns else "unreadable",
        "presented_issuer": (orgs[0] if orgs else (cns[0] if cns else "?")),
    }


def check_tls(host: str) -> Dict:
    """Validate the certificate, and on failure identify who answered."""
    out: Dict = {"gate": "tls", "host": host}
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=TIMEOUT) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                cert = tls.getpeercert() or {}
                subject = dict(
                    x[0] for x in cert.get("subject", ()) if x
                )
                issuer = dict(
                    x[0] for x in cert.get("issuer", ()) if x
                )
                out["ok"] = True
                out["cert_cn"] = subject.get("commonName", "?")
                out["issuer"] = issuer.get("organizationName", "?")
                out["tls_version"] = tls.version()
        return out
    except ssl.SSLCertVerificationError as exc:
        out["ok"] = False
        out["detail"] = str(exc)
        out["interception_suspected"] = True
        # Read the presented cert without verifying, purely to name the
        # interceptor. Nothing is sent over this socket.
        try:
            unverified = ssl._create_unverified_context()
            with socket.create_connection((host, 443), timeout=TIMEOUT) as sock:
                with unverified.wrap_socket(sock, server_hostname=host) as tls:
                    # With CERT_NONE, getpeercert() returns {} — the parsed
                    # form is only populated when verification ran. The DER
                    # bytes are still available, so pull the names from there.
                    der = tls.getpeercert(binary_form=True)
                    out.update(_names_from_der(der))
        except Exception:
            out["presented_cn"] = "unreadable"
        return out
    except Exception as exc:
        out["ok"] = False
        out["detail"] = f"{type(exc).__name__}: {exc}"
        return out


# ── Gates 3 & 4: HTTP ──────────────────────────────────────────────────────

def _http_get(url: str, headers: Dict[str, str]) -> Tuple[int, str]:
    """Plain GET returning (status, body). urllib keeps this dependency-free
    so the probe can run on a bare box with no pip install."""
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, resp.read(4096).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        # 403 is the interesting one: reached the exchange, was refused.
        return exc.code, exc.read(4096).decode("utf-8", "replace")


def _verdict(exchange: str, status: int, body: str) -> Tuple[bool, str]:
    """Interpret an exchange response.

    Both exchanges answer HTTP 200 for business errors, so the body has to be
    parsed. A signature/key error still proves the region is reachable; a
    geo-block does not.
    """
    try:
        payload = json.loads(body)
    except ValueError:
        return False, f"HTTP {status}, non-JSON body: {body[:120]}"

    if exchange == "bybit":
        code = payload.get("retCode")
        msg = payload.get("retMsg", "")
        if code == 0:
            return True, "accepted"
        # 10010 = unmatched IP, 10029/403 = restricted location.
        if code in (10010, 10029) or status == 403:
            return False, f"BLOCKED retCode={code} {msg!r}"
        return False, f"rejected retCode={code} {msg!r}"

    code = str(payload.get("code", ""))
    msg = payload.get("msg", "")
    if code == "0":
        return True, "accepted"
    if status == 403 or code in ("50110", "50111"):
        return False, f"BLOCKED code={code} {msg!r}"
    return False, f"rejected code={code} {msg!r}"


def check_public(probe: Probe) -> Dict:
    out: Dict = {"gate": "public", "url": probe.base + probe.public_path}
    try:
        status, body = _http_get(out["url"], {"User-Agent": "probe/1.0"})
        ok, detail = _verdict(probe.name, status, body)
        out.update(ok=ok, status=status, detail=detail)
    except Exception as exc:
        out.update(ok=False, detail=f"{type(exc).__name__}: {exc}")
    return out


def check_private(probe: Probe) -> Dict:
    """The only gate that proves this IP may trade."""
    out: Dict = {"gate": "private", "url": probe.base + probe.private_path}
    if not probe.creds_present():
        out.update(ok=None, detail="skipped: credentials not set")
        return out
    headers = probe.sign("GET", probe.private_path)
    if not headers:
        out.update(ok=None, detail="skipped: could not build signature")
        return out
    headers["User-Agent"] = "probe/1.0"
    try:
        status, body = _http_get(out["url"], headers)
        ok, detail = _verdict(probe.name, status, body)
        out.update(ok=ok, status=status, detail=detail)
    except Exception as exc:
        out.update(ok=False, detail=f"{type(exc).__name__}: {exc}")
    return out


# ── Context ────────────────────────────────────────────────────────────────

def egress_ip() -> Optional[str]:
    """Public egress IP — the address the exchange actually sees."""
    for url in _IP_SERVICES:
        try:
            _, body = _http_get(url, {"User-Agent": "probe/1.0"})
            ip = body.strip().split("\n")[0].strip()
            if ip and len(ip) <= 45:
                return ip
        except Exception:
            continue
    return None


def run(exchanges: List[str], private: bool) -> Dict:
    results: Dict = {
        "probed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "egress_ip": egress_ip(),
        "exchanges": {},
    }

    hijack_check: Dict[str, List[str]] = {}
    for name in exchanges:
        probe = PROBES[name]
        gates = [check_dns(probe.host)]
        hijack_check[probe.host] = gates[0].get("ips", [])
        # Only continue down the chain while the previous gate held; a failed
        # TLS handshake makes HTTP results meaningless.
        if gates[-1]["ok"]:
            gates.append(check_tls(probe.host))
            if gates[-1]["ok"]:
                gates.append(check_public(probe))
                if private:
                    gates.append(check_private(probe))
        results["exchanges"][name] = {
            "host": probe.host,
            "gates": gates,
            "reachable": all(
                g["ok"] for g in gates if g.get("ok") is not None
            ),
        }

    # Distinct exchange hostnames sharing one IP is the DNS-hijack signature.
    seen: Dict[str, List[str]] = {}
    for host, ips in hijack_check.items():
        for ip in ips:
            seen.setdefault(ip, []).append(host)
    results["shared_ips"] = {
        ip: hosts for ip, hosts in seen.items() if len(hosts) > 1
    }
    return results


def _print_report(results: Dict) -> None:
    print("=" * 74)
    print("EXCHANGE ACCESS PROBE")
    print("=" * 74)
    print(f"  time      : {results['probed_at']}")
    print(f"  egress IP : {results.get('egress_ip') or 'unknown'}")
    print("  (record this IP: it is what the exchange geo-checks)")

    for name, data in results["exchanges"].items():
        print(f"\n{'-' * 74}\n  {name.upper()}  ({data['host']})\n{'-' * 74}")
        for gate in data["gates"]:
            ok = gate.get("ok")
            mark = "SKIP" if ok is None else ("PASS" if ok else "FAIL")
            print(f"    [{mark}] {gate['gate']:<8}", end="")
            if gate["gate"] == "dns":
                print(f" {', '.join(gate.get('ips', [])) or '-'}")
            elif gate["gate"] == "tls" and ok:
                print(f" cn={gate.get('cert_cn')} issuer={gate.get('issuer')}")
            else:
                print(f" {gate.get('detail', '')}")
            if gate.get("interception_suspected"):
                print(
                    f"           -> TLS intercepted. Presented cert: "
                    f"cn={gate.get('presented_cn')} "
                    f"issuer={gate.get('presented_issuer', '?')}"
                )
                print(
                    "           -> Do NOT disable verification; that exposes "
                    "your API keys."
                )
        verdict = "REACHABLE" if data["reachable"] else "NOT USABLE"
        print(f"    => {verdict}")

    if results["shared_ips"]:
        print(f"\n{'=' * 74}\n  WARNING: DNS HIJACK SIGNATURE\n{'=' * 74}")
        for ip, hosts in results["shared_ips"].items():
            print(f"    {ip} <- {', '.join(hosts)}")
        print("    Distinct exchange hostnames resolving to one IP means DNS")
        print("    is being redirected, almost always ISP-level filtering.")
        print("    A different network or region is required; no code change")
        print("    can work around it safely.")

    any_private = any(
        g["gate"] == "private" and g.get("ok") is not None
        for d in results["exchanges"].values() for g in d["gates"]
    )
    if not any_private:
        print(
            f"\n  NOTE: private gate not evaluated. Public data often works "
            f"from\n        regions where TRADING is refused — rerun with "
            f"--private for\n        a trustworthy answer."
        )

    print(f"\n{'=' * 74}")
    print("  SCOPE: this tool measures NETWORK REACHABILITY ONLY")
    print("=" * 74)
    print("  A PASS means the exchange answered this IP. It does NOT mean you")
    print("  are permitted to trade. Two independent gates exist:")
    print()
    print("    1. IP / region   <- what this tool measures")
    print("    2. Account KYC   <- set by YOUR residency, not the server's")
    print()
    print("  Gate 2 is unmeasurable from here and is the one that gets")
    print("  accounts frozen. Hosting in a permitted region does not change")
    print("  the residency on your account. Check the exchange's own")
    print("  restricted-jurisdiction page for the authoritative answer.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Probe whether this host can reach Bybit / OKX APIs.",
    )
    parser.add_argument(
        "--exchange", choices=sorted(PROBES) + ["all"], default="all",
    )
    parser.add_argument(
        "--private", action="store_true",
        help="also test a SIGNED endpoint (proves this IP may trade)",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    args = parser.parse_args()

    names = sorted(PROBES) if args.exchange == "all" else [args.exchange]
    results = run(names, args.private)

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        _print_report(results)

    return 0 if all(
        d["reachable"] for d in results["exchanges"].values()
    ) else 1


if __name__ == "__main__":
    sys.exit(main())
