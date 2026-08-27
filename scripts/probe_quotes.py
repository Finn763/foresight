"""连通性探针：逐端点拉一个报价，打印结果。跑完把可用端点填进 quotes.py。

用法: .venv/Scripts/python.exe scripts/probe_quotes.py
每个端点一行 HTTP 状态 + 原始响应前 120 字符；sina/tencent 另打印字段序（index: value），
用于定稿 _parse_sina/_parse_tencent 的字段位置。
"""

import httpx

SINA_HDR = {"Referer": "https://finance.sina.com.cn"}

PROBES = [
    ("sina_gb_inx", "https://hq.sinajs.cn/list=gb_$inx", SINA_HDR),
    ("tencent_usinx", "https://qt.gtimg.cn/q=usINX", {}),
    ("tencent_usdji", "https://qt.gtimg.cn/q=usDJI", {}),
    ("tencent_sh", "https://qt.gtimg.cn/q=s_sh000001", {}),
    ("sina_hf_gc", "https://hq.sinajs.cn/list=hf_GC", SINA_HDR),
    ("sina_hf_oil", "https://hq.sinajs.cn/list=hf_OIL", SINA_HDR),
    ("sina_hf_brent", "https://hq.sinajs.cn/list=hf_B", SINA_HDR),
    ("sina_fx_cnh", "https://hq.sinajs.cn/list=fx_susdcnh", SINA_HDR),
    (
        "coingecko_btc",
        "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd",
        {},
    ),
    ("binance_btc", "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", {}),
]

# 逗号分隔类响应（sina）打印字段序，用于定稿 index
FIELD_SPLIT = {
    "sina_gb_inx": ",",
    "sina_hf_gc": ",",
    "sina_hf_oil": ",",
    "sina_hf_brent": ",",
    "sina_fx_cnh": ",",
}
# 波浪号分隔类响应（tencent）打印字段序
TILDE_SPLIT = {"tencent_usinx", "tencent_usdji", "tencent_sh"}


def main() -> None:
    for name, url, headers in PROBES:
        try:
            r = httpx.get(url, headers=headers, timeout=15)
            text = r.text
            print(f"{name}: HTTP {r.status_code} -> {text[:120]!r}")
            if r.status_code >= 400:
                continue
            if name in FIELD_SPLIT and '"' in text:
                body = text.split('"')[1]
                parts = body.split(",")
                print(
                    "  fields: " + " | ".join(f"{i}:{parts[i]}" for i in range(min(12, len(parts))))
                )
            elif name in TILDE_SPLIT and '"' in text:
                body = text.split('"')[1]
                parts = body.split("~")
                print(
                    "  fields: " + " | ".join(f"{i}:{parts[i]}" for i in range(min(10, len(parts))))
                )
        except Exception as e:
            print(f"{name}: FAIL {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
