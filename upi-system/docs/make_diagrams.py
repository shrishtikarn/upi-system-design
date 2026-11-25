"""Generates docs/architecture.svg and docs/data-flow.svg (stdlib only).

    python docs/make_diagrams.py
    # optional PNGs (needs rsvg-convert or Inkscape):
    rsvg-convert -w 2000 docs/architecture.svg -o docs/architecture.png

Edit the labels below and re-run to update the diagrams.
"""
from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import escape

OUT = Path(__file__).parent
FONT = "Inter, 'Segoe UI', Helvetica, Arial, sans-serif"
MONO = "'JetBrains Mono', Consolas, 'Courier New', monospace"

INK, MUTED, LINE = "#1f2933", "#5f6b7a", "#3d4752"
BLUE, GREEN, ORANGE, RED, PG, AMBER = "#2a6fdb", "#1d9e75", "#e07a1f", "#d1453b", "#336791", "#b7791f"
FILL = {"blue": "#eef3fd", "green": "#e9f7f1", "red": "#fdeeee", "pg": "#eaf1f9", "amber": "#fff5e1", "grey": "#f5f6f8"}


def _markers() -> str:
    out = ["<defs>"]
    for name, color in (("ink", LINE), ("green", GREEN), ("orange", ORANGE), ("grey", "#8a94a3"), ("blue", BLUE)):
        out.append(f'<marker id="arrow-{name}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
                   f'orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="{color}"/></marker>')
    out.append("</defs>")
    return "".join(out)


def text(x, y, s, size=13, color=INK, weight="normal", anchor="start", family=FONT, italic=False) -> str:
    style = ' font-style="italic"' if italic else ""
    return (f'<text x="{x}" y="{y}" font-family="{family}" font-size="{size}" fill="{color}" '
            f'font-weight="{weight}" text-anchor="{anchor}"{style}>{escape(s)}</text>')


def box(x, y, w, h, fill, stroke, rx=10, dash=False, sw=1.5) -> str:
    d = ' stroke-dasharray="6 4"' if dash else ""
    return f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"{d}/>'


def path(d, color=LINE, marker="ink", dash=False, width=1.6, start_marker=False) -> str:
    dd = ' stroke-dasharray="5 4"' if dash else ""
    sm = f' marker-start="url(#arrow-{marker})"' if start_marker else ""
    return (f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{width}"{dd} '
            f'marker-end="url(#arrow-{marker})"{sm}/>')


def label(x, y, s, color=MUTED, size=11.5, anchor="middle", bg=True) -> str:
    out = ""
    if bg:
        w = len(s) * size * 0.56 + 8
        bx = x - w / 2 if anchor == "middle" else (x - 4 if anchor == "start" else x - w + 4)
        out += f'<rect x="{bx:.0f}" y="{y - size + 1:.0f}" width="{w:.0f}" height="{size + 5:.0f}" fill="#ffffff" opacity="0.92" rx="3"/>'
    return out + text(x, y, s, size=size, color=color, anchor=anchor)


# --------------------------------------------------------------------------- architecture
def architecture() -> str:
    W, H = 1400, 880
    s = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}">',
         _markers(), f'<rect width="{W}" height="{H}" fill="#ffffff"/>',
         text(30, 42, "UPI Payment System - Architecture", 24, weight="bold"),
         text(30, 66, "docker compose: 1 gateway · 2 payment nodes · 1 account node · 2 worker nodes · mock NPCI switch · PostgreSQL · Redis",
              13, MUTED)]

    # clients
    s.append(box(30, 140, 180, 80, "#ffffff", "#8a94a3"))
    s.append(text(120, 172, "UPI mobile app", 14, weight="bold", anchor="middle"))
    s.append(text(120, 192, "X-Client-ID: phone id", 11.5, MUTED, anchor="middle"))
    s.append(text(120, 208, "Idempotency-Key", 11.5, MUTED, anchor="middle"))
    s.append(box(30, 260, 180, 70, "#ffffff", "#8a94a3", dash=True))
    s.append(text(120, 290, "Locust load test", 14, weight="bold", anchor="middle"))
    s.append(text(120, 310, "steady · spike · chaos", 11.5, MUTED, anchor="middle"))

    # gateway
    gx, gy, gw, gh = 270, 110, 300, 390
    s.append(box(gx, gy, gw, gh, FILL["blue"], BLUE, sw=2))
    s.append(text(gx + gw / 2, gy + 30, "API Gateway  :8080", 17, BLUE, "bold", "middle"))
    s.append(text(gx + gw / 2, gy + 48, "the only public entry point", 11.5, MUTED, anchor="middle"))
    steps = [
        ("1", "assign X-Request-ID (trace id)", INK),
        ("2", "route match: longest prefix", INK),
        ("3", "token bucket rate limit -> 429", ORANGE),
        ("", "per-client + global, atomic in Redis", MUTED),
        ("4", "inspect: 400 / 405 / 413 / 415", INK),
        ("5", "round robin over healthy nodes", INK),
        ("6", "circuit breaker per node", RED),
        ("7", "proxy; retry other node if safe", INK),
        ("8", "JSON log line + metrics", INK),
    ]
    y = gy + 82
    for num, line, color in steps:
        if num:
            s.append(f'<circle cx="{gx + 26}" cy="{y - 4}" r="9" fill="{BLUE}"/>')
            s.append(text(gx + 26, y, num, 11, "#ffffff", "bold", "middle"))
        s.append(text(gx + 44, y, line, 13 if num else 11.5, color))
        y += 28 if num else 24
    s.append(f'<line x1="{gx + 16}" y1="{gy + gh - 52}" x2="{gx + gw - 16}" y2="{gy + gh - 52}" stroke="#c9d6f2"/>')
    s.append(text(gx + gw / 2, gy + gh - 28, "GET /health   /metrics   /stats", 12.5, BLUE, anchor="middle", family=MONO))
    s.append(text(gx + gw / 2, gy + gh - 11, "health monitor polls every node every 3 s", 11, MUTED, anchor="middle"))

    # services
    sx, sw_ = 650, 230
    for i, (name, sub) in enumerate((("payment-service-1", "POST /payments -> 202"), ("payment-service-2", "GET /payments/{id}"))):
        y0 = 110 + i * 82
        s.append(box(sx, y0, sw_, 68, FILL["grey"], "#5f6b7a"))
        s.append(text(sx + 14, y0 + 27, name, 14, weight="bold"))
        s.append(text(sx + 14, y0 + 49, sub, 11.5, MUTED, family=MONO))
    s.append(text(sx + sw_ - 8, 270, "stateless replicas", 11, MUTED, anchor="end", italic=True))
    s.append(box(sx, 300, sw_, 92, FILL["grey"], "#5f6b7a"))
    s.append(text(sx + 14, 327, "account-service", 14, weight="bold"))
    for k, line in enumerate(("GET /vpa/{vpa}", "GET /accounts/{vpa}/balance", "GET /accounts/{vpa}/transactions")):
        s.append(text(sx + 14, 348 + k * 15, line, 10.5, MUTED, family=MONO))

    # redis
    rx, ry, rw, rh = 970, 110, 400, 282
    s.append(box(rx, ry, rw, rh, FILL["red"], RED, sw=2))
    s.append(text(rx + 16, ry + 28, "Redis 7", 17, RED, "bold"))
    s.append(text(rx + rw - 16, ry + 28, "AOF on · volatile-lru", 11.5, MUTED, anchor="end"))
    sections = [
        ("Rate-limit buckets", "rl:client:<policy>:<id>   rl:global   (TTL)", ORANGE),
        ("Cache-aside (TTL)", "cache:vpa 5m · cache:balance 30s · cache:txn", BLUE),
        ("Queue: Redis Stream", "upi:payments:stream -> group payment-workers", GREEN),
        ("", "PEL + XAUTOCLAIM re-delivery · upi:payments:dlq", GREEN),
    ]
    yy = ry + 52
    for title, sub, color in sections:
        if title:
            s.append(box(rx + 14, yy, rw - 28, 62 if "Stream" in title else 52, "#ffffff", color, rx=7, sw=1.2))
            s.append(text(rx + 26, yy + 21, title, 13, color, "bold"))
            s.append(text(rx + 26, yy + 40, sub, 11, MUTED, family=MONO))
            yy += 62 if "Stream" not in title else 0
        else:
            s.append(text(rx + 26, yy + 56, sub, 11, MUTED, family=MONO))
    # postgres
    px, py, pw, ph = 970, 560, 400, 250
    s.append(box(px, py, pw, ph, FILL["pg"], PG, sw=2))
    s.append(text(px + 16, py + 28, "PostgreSQL 16", 17, PG, "bold"))
    s.append(text(px + pw - 16, py + 28, "source of truth", 11.5, MUTED, anchor="end"))
    tables = [("users", "who"), ("bank_accounts", "balance_paise  CHECK >= 0"), ("vpas", "alice@okaxis -> account"),
              ("transactions", "UNIQUE idempotency_key · lease"),
              ("ledger_entries", "double entry: 1 debit + 1 credit"), ("transaction_events", "audit log of every status change")]
    for k, (t, d) in enumerate(tables):
        s.append(text(px + 20, py + 60 + k * 30, t, 12.5, INK, "bold", family=MONO))
        s.append(text(px + 168, py + 60 + k * 30, d, 11.5, MUTED))
    s.append(text(px + 20, py + ph - 14, "migrations: db/migrations/*.sql (migrate container)", 11, MUTED, italic=True))

    # workers
    wx, wy, ww, wh = 650, 560, 230, 250
    s.append(box(wx, wy, ww, wh, FILL["green"], GREEN, sw=2))
    s.append(text(wx + ww / 2, wy + 26, "Worker pool", 16, GREEN, "bold", "middle"))
    s.append(text(wx + ww / 2, wy + 44, "consumer group: payment-workers", 11, MUTED, anchor="middle"))
    for i in range(2):
        y0 = wy + 58 + i * 64
        s.append(box(wx + 14, y0, ww - 28, 54, "#ffffff", GREEN, rx=7, sw=1.2))
        s.append(text(wx + 26, y0 + 22, f"worker-{i + 1}", 13.5, weight="bold"))
        s.append(text(wx + 26, y0 + 41, "8 consumers + reclaimer", 11, MUTED))
    for k, line in enumerate(("retry + exponential backoff", "circuit breaker (npci-switch)", "lease claim -> exactly-once effect")):
        s.append(text(wx + 18, wy + 205 + k * 16, "• " + line, 11, INK))

    # bank
    bx, by, bw, bh = 300, 600, 240, 150
    s.append(box(bx, by, bw, bh, FILL["amber"], AMBER, sw=2))
    s.append(text(bx + bw / 2, by + 28, "Mock NPCI switch / bank", 15, AMBER, "bold", "middle"))
    s.append(text(bx + bw / 2, by + 50, "POST /npci/authorize", 11.5, MUTED, anchor="middle", family=MONO))
    s.append(text(bx + bw / 2, by + 74, "20-60 ms latency, 1% errors", 11.5, INK, anchor="middle"))
    s.append(text(bx + bw / 2, by + 94, "chaos: flaky · slow · down", 11.5, RED, anchor="middle"))
    s.append(text(bx + bw / 2, by + 114, "POST :8090/admin/chaos", 11, MUTED, anchor="middle", family=MONO))
    s.append(text(bx + bw / 2, by + 134, "amount ending .13 -> DECLINED", 11, MUTED, anchor="middle"))

    # arrows - sync path
    s.append(path("M210,180 H268", BLUE, "blue", width=2))
    s.append(label(239, 172, "HTTP", BLUE))
    s.append(path("M210,295 H268", BLUE, "blue", width=2, dash=True))
    for y0 in (144, 226):
        s.append(path(f"M570,{y0} H648", LINE))
    s.append(path("M570,346 H648", LINE))
    s.append(label(609, 136, "round robin"))
    s.append(label(609, 338, "proxy"))
    # gateway -> redis (rate limit)
    s.append(path("M420,110 V88 H1120 V108", ORANGE, "orange", width=1.8))
    s.append(label(770, 92, "EVALSHA token_bucket.lua (client + global bucket, one atomic step)", ORANGE))
    # services -> redis cache / stream
    s.append(path("M880,330 L968,228", BLUE, "blue"))
    s.append(label(905, 278, "GET/SET", BLUE, anchor="middle"))
    s.append(path("M880,160 C925,160 925,300 968,300", GREEN, "green", width=2))
    s.append(label(928, 205, "XADD", GREEN))
    # services -> postgres (bus)
    s.append(path("M880,250 H935 V600 H968", PG, "ink"))
    s.append(path("M880,380 H935", PG, "ink"))
    s.append(label(935, 520, "SQL", PG))
    # workers <-> redis stream
    s.append(path("M800,560 V470 H1210 V394", GREEN, "green", width=2, start_marker=True))
    s.append(label(948, 492, "XREADGROUP · XACK · XAUTOCLAIM · DEL cache keys", GREEN, anchor="start", bg=False))
    # workers -> postgres
    s.append(path("M880,700 H968", PG, "ink", width=2))
    s.append(label(924, 692, "settle", PG))
    s.append(label(924, 720, "1 txn", PG, bg=False))
    # workers -> bank
    s.append(path("M650,675 H542", AMBER, "orange", width=2))
    s.append(label(596, 667, "authorize", AMBER))
    # health checks
    s.append(path("M330,500 V520 H420 V598", "#8a94a3", "grey", dash=True))
    s.append(path("M510,500 V530 H720 V558", "#8a94a3", "grey", dash=True))
    s.append(label(615, 545, "GET /health", "#6b7684"))

    # legend
    lx, ly = 30, 600
    s.append(box(lx, ly, 240, 210, "#ffffff", "#d5dae1", rx=8, sw=1))
    s.append(text(lx + 14, ly + 24, "Legend", 13, weight="bold"))
    items = [(BLUE, False, "synchronous request path"), (GREEN, False, "asynchronous path (queue)"),
             (ORANGE, False, "rate limiting (token bucket)"), (PG, False, "database access"),
             ("#8a94a3", True, "health checks (every 3 s)")]
    for k, (color, dash, name) in enumerate(items):
        y0 = ly + 50 + k * 26
        d = ' stroke-dasharray="5 4"' if dash else ""
        s.append(f'<line x1="{lx + 14}" y1="{y0 - 4}" x2="{lx + 52}" y2="{y0 - 4}" stroke="{color}" stroke-width="2"{d}/>')
        s.append(text(lx + 62, y0, name, 12, INK))
    s.append(text(lx + 14, ly + 192, "only :8080 is public", 11.5, MUTED, italic=True))
    s.append("</svg>")
    return "\n".join(s)


# --------------------------------------------------------------------------- data flow
def data_flow() -> str:
    lanes = [("Mobile app", 90, "#8a94a3"), ("API Gateway", 285, BLUE), ("Payment svc", 480, "#5f6b7a"),
             ("Redis", 675, RED), ("PostgreSQL", 870, PG), ("Worker", 1065, GREEN), ("NPCI mock", 1265, AMBER)]
    x = {name: lx for name, lx, _ in lanes}
    W, top = 1370, 92
    body: list[str] = []
    y = top + 76
    step = [0]

    def msg(a, b, title, note="", color=LINE, marker="ink", reply=False):
        nonlocal y
        step[0] += 1
        xa, xb = x[a], x[b]
        body.append(path(f"M{xa},{y} H{xb + (-6 if xb > xa else 6)}", color, marker, dash=reply, width=1.7))
        mid = (xa + xb) / 2
        body.append(label(mid, y - 7, f"{step[0]}.  {title}", INK, 12))
        if note:
            body.append(label(mid, y + 17, note, MUTED, 11, bg=False))
        y += 52 if note else 42

    def band(title, color):
        nonlocal y
        y += 4
        body.append(f'<rect x="20" y="{y - 18}" width="{W - 40}" height="26" fill="{color}" opacity="0.10" rx="5"/>')
        body.append(text(32, y, title, 12.5, color, "bold"))
        y += 36

    band("SYNCHRONOUS - the user waits only for this part (~10 ms)", BLUE)
    msg("Mobile app", "API Gateway", "POST /api/v1/payments", "Idempotency-Key, X-Client-ID", BLUE, "blue")
    msg("API Gateway", "Redis", "EVALSHA token bucket (client + global)", "no token -> 429 + Retry-After, request ends here", ORANGE, "orange")
    msg("API Gateway", "Payment svc", "forward + X-Request-ID", "round robin, breaker", BLUE, "blue")
    msg("Payment svc", "Redis", "GET cache:vpa:bob@oksbi", "payee name (miss -> SELECT, SET 5 min)")
    msg("Payment svc", "PostgreSQL", "INSERT txn ... ON CONFLICT (idempotency_key)", "status PENDING + audit event", PG)
    msg("Payment svc", "Redis", "XADD upi:payments:stream", "{txn_id, request_id, enqueued_at}", GREEN, "green")
    msg("Payment svc", "API Gateway", "202 Accepted", "", BLUE, "blue", reply=True)
    msg("API Gateway", "Mobile app", "202 {txn_id, PENDING}", "the bank has not been called yet", BLUE, "blue", reply=True)

    band("ASYNCHRONOUS - worker pool, decoupled from the request", GREEN)
    msg("Worker", "Redis", "XREADGROUP payment-workers >", "Redis hands each message to one consumer", GREEN, "green")
    msg("Worker", "PostgreSQL", "CLAIM lease", "PENDING -> PROCESSING (duplicates: 0 rows)", PG)
    msg("Worker", "NPCI mock", "POST /npci/authorize", "retry 0.2s/0.4s/0.8s + breaker", AMBER, "orange")
    msg("NPCI mock", "Worker", "APPROVED, bank_ref", "", AMBER, "orange", reply=True)
    msg("Worker", "PostgreSQL", "settle in ONE transaction", "lock rows, debit, credit, ledger x2, SUCCESS, COMMIT", PG)
    msg("Worker", "Redis", "XACK + DEL cache keys", "balance / txn / history of both parties", GREEN, "green")

    band("CLIENT POLLS STATUS", BLUE)
    msg("Mobile app", "API Gateway", "GET /payments/{txn_id}", "", BLUE, "blue")
    msg("API Gateway", "Payment svc", "forward", "", BLUE, "blue")
    msg("Payment svc", "Redis", "GET cache:txn:{id}", "miss -> SELECT; final status cached 5 min")
    msg("Payment svc", "Mobile app", "200 {status: SUCCESS, bank_ref}", "via the gateway, X-Cache: HIT or MISS", BLUE, "blue", reply=True)

    bottom = y - 10
    fy = bottom + 20
    H = fy + 112 + 24
    s = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}">', _markers(),
         f'<rect width="{W}" height="{H}" fill="#ffffff"/>',
         text(30, 40, "Data flow - \"send Rs 150 from alice@okaxis to bob@oksbi\"", 22, weight="bold"),
         text(30, 63, "One X-Request-ID travels through every hop below; `python scripts/demo.py trace <id>` prints exactly this path from the logs.",
              12.5, MUTED)]
    for name, lx, color in lanes:
        s.append(box(lx - 70, top, 140, 36, "#ffffff", color, rx=8, sw=1.8))
        s.append(text(lx, top + 23, name, 13.5, color, "bold", "middle"))
        s.append(f'<line x1="{lx}" y1="{top + 36}" x2="{lx}" y2="{bottom}" stroke="#d5dae1" stroke-width="1.5" stroke-dasharray="4 4"/>')
    s += body
    s.append(box(30, fy, W - 60, 112, "#fff8f6", RED, rx=8, sw=1.2))
    s.append(text(46, fy + 24, "When step 11 fails", 13.5, RED, "bold"))
    notes = [
        "Bank 5xx/timeout -> retried in-process with exponential backoff (equal jitter). 5 consecutive failures open the circuit breaker: calls fail fast for 10 s, then one trial call.",
        "Still failing / breaker open -> txn back to PENDING and the message is NOT acknowledged; it stays in the Pending Entries List and XAUTOCLAIM re-delivers it after 15 s.",
        "After 3 delivery attempts (or 120 s) -> FAILED + copy to upi:payments:dlq. Worker crash mid-way -> lease expires, another worker re-claims. Money moves at most once.",
    ]
    for k, n in enumerate(notes):
        s.append(text(46, fy + 48 + k * 21, "• " + n, 11.5, INK))
    s.append("</svg>")
    return "\n".join(s)


if __name__ == "__main__":
    (OUT / "architecture.svg").write_text(architecture(), encoding="utf-8")
    (OUT / "data-flow.svg").write_text(data_flow(), encoding="utf-8")
    print("wrote docs/architecture.svg and docs/data-flow.svg")
