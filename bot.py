"""
HYPEUSDT Futures — DCA Long Bot  (EMA8 + Volumen → Telegram)
═════════════════════════════════════════════════════════════
Estrategia LONG-only en HYPEUSDT Perpetual (paper / simulación):

  Al cierre de cada vela 1m:
  ├─ 0 posiciones:  vol > 70K  AND  cierre < EMA8  →  Pos 1 (12 USDT)
  ├─ 1 posición:    ROI < -1.6%  AND  vol > 70K  AND  cierre < EMA8  →  DCA Pos 2 (12 USDT)
  └─ 2 posiciones:  ROI < -1.6%  AND  vol > 70K  AND  cierre < EMA8  →  DCA Pos 3 (24 USDT)

  ROI combinado ≥ 1%  →  Take Profit (cerrar todo)

  • 1 500 velas históricas cargadas al inicio via REST  →  EMA8 inicial
  • Al cierre de cada vela 1m (WebSocket kline): recalcula EMA8 con los nuevos datos
  • Precio en tiempo real via WS markPrice@1s  →  monitoreo continuo de TP
  • Refresca el histórico cada 30 min para evitar deriva de datos
  • Dashboard HTML en /  |  /health  |  /api/state
"""

import asyncio
import aiohttp
from aiohttp import web
import logging
import time
import os
import json
from collections import deque
from datetime import datetime, timezone

# ══════════════════════════════════════════════════════════
#  CONFIGURACIÓN  (ajustar via variables de entorno en Render)
# ══════════════════════════════════════════════════════════
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID",   "")

SYMBOL        = "HYPEUSDT"
EMA_PERIOD    = int(os.environ.get("EMA_PERIOD",    "8"))

# Volumen mínimo en USDT (campo "q" del kline = quote asset volume)
VOL_THRESHOLD = float(os.environ.get("VOL_THRESHOLD", "70000"))

# ROI combinado mínimo para activar DCA (negativo = pérdida)
DCA_ROI_TH    = float(os.environ.get("DCA_ROI_TH", "-1.6"))

# ROI combinado para cerrar todas las posiciones (Take Profit)
TP_ROI        = float(os.environ.get("TP_ROI", "1.0"))

# Tamaños de cada DCA en USDT
DCA1 = float(os.environ.get("DCA1", "12"))
DCA2 = float(os.environ.get("DCA2", "12"))
DCA3 = float(os.environ.get("DCA3", "24"))
DCA_SIZES = [DCA1, DCA2, DCA3]

BINANCE_REST   = "https://fapi.binance.com"
BINANCE_WS     = "wss://fstream.binance.com"
KLINES_LIMIT   = 1500
INTERVAL       = "1m"
PORT           = int(os.environ.get("PORT", "10000"))
WS_RECONNECT   = 5      # segundos entre reconexiones WS
REFRESH_MIN    = 2     # minutos entre refrescos del histórico REST

# ══════════════════════════════════════════════════════════
#  ESTADO GLOBAL
# ══════════════════════════════════════════════════════════
closes        : deque = deque(maxlen=KLINES_LIMIT)  # precios de cierre
volumes       : deque = deque(maxlen=KLINES_LIMIT)  # volumen quote (USDT)
current_price : float = 0.0
current_ema8  : float = 0.0
positions     : list  = []   # posiciones DCA abiertas [{dca, price, size, qty, time}]
closed_trades : list  = []   # historial de TPs cerrados

bot_state = {
    "status"      : "⏳ Iniciando...",
    "last_candle" : "—",
    "last_vol"    : 0.0,
    "candles_ok"  : 0,
    "alerts_count": 0,
    "tp_count"    : 0,
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("HYPE-DCA")


# ══════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════
def calc_ema(data: list, period: int) -> float:
    """EMA estándar: SMA de los primeros `period` valores, luego EMA incremental."""
    if len(data) < period:
        return 0.0
    k   = 2.0 / (period + 1)
    ema = sum(data[:period]) / period       # SMA inicial
    for price in data[period:]:
        ema = price * k + ema * (1.0 - k)
    return ema


def combined_roi(price: float) -> float:
    """ROI % de todas las posiciones abiertas valuadas al precio actual."""
    if not positions:
        return 0.0
    cost  = sum(p["size"] for p in positions)
    value = sum(p["qty"]  * price for p in positions)
    return (value - cost) / cost * 100.0


def avg_entry_price() -> float:
    """Precio promedio ponderado de las posiciones abiertas."""
    if not positions:
        return 0.0
    total_cost = sum(p["size"] for p in positions)
    total_qty  = sum(p["qty"]  for p in positions)
    return total_cost / total_qty if total_qty else 0.0


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


# ══════════════════════════════════════════════════════════
#  TELEGRAM
# ══════════════════════════════════════════════════════════
async def send_telegram(session: aiohttp.ClientSession, text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.info(f"[Telegram-skip] {text[:80]}")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        async with session.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=aiohttp.ClientTimeout(total=12),
        ) as r:
            if r.status != 200:
                body = await r.text()
                log.warning(f"Telegram {r.status}: {body[:100]}")
    except Exception as e:
        log.error(f"Telegram error: {e}")


# ══════════════════════════════════════════════════════════
#  BINANCE REST — carga histórica de velas
# ══════════════════════════════════════════════════════════
async def fetch_klines(session: aiohttp.ClientSession) -> bool:
    """
    Descarga las últimas KLINES_LIMIT velas 1m de HYPEUSDT Futures.
    Rellena los deques `closes` y `volumes`.
    """
    global closes, volumes, current_ema8
    url    = f"{BINANCE_REST}/fapi/v1/klines"
    params = {"symbol": SYMBOL, "interval": INTERVAL, "limit": KLINES_LIMIT}
    try:
        async with session.get(
            url, params=params, timeout=aiohttp.ClientTimeout(total=20)
        ) as r:
            if r.status != 200:
                log.error(f"klines HTTP {r.status}")
                return False
            data = await r.json()
            closes.clear()
            volumes.clear()
            # Excluimos la última vela: puede estar AÚN ABIERTA (parcial)
            # k[5] = base asset volume (HYPE tokens) — lo que se ve en el chart
            # k[7] = quote asset volume (USDT) — NO usar, no coincide con el chart
            for k in data[:-1]:
                closes.append(float(k[4]))   # [4] close price
                volumes.append(float(k[5]))  # [5] base asset volume (HYPE tokens)
            current_ema8 = calc_ema(list(closes), EMA_PERIOD)
            bot_state["candles_ok"] = len(closes)
            log.info(
                f"✅ {len(closes)} velas cargadas | "
                f"último cierre: {closes[-1]:.6f} | "
                f"EMA{EMA_PERIOD}: {current_ema8:.6f}"
            )
            return True
    except Exception as e:
        log.error(f"fetch_klines error: {e}")
        return False


# ══════════════════════════════════════════════════════════
#  LÓGICA DE TRADING
# ══════════════════════════════════════════════════════════
async def on_candle_close(
    session: aiohttp.ClientSession, close_price: float, volume: float
):
    """
    Callback al cierre de cada vela 1m.
    1. Actualiza el histórico de cierres y volúmenes.
    2. Recalcula EMA8.
    3. Evalúa condiciones y abre posición / DCA si aplica.
    """
    global current_ema8

    closes.append(close_price)
    volumes.append(volume)

    ema8          = calc_ema(list(closes), EMA_PERIOD)
    current_ema8  = ema8
    bot_state["last_candle"] = now_utc()
    bot_state["last_vol"]    = volume

    n   = len(positions)
    roi = combined_roi(close_price)

    vol_ok   = volume      > VOL_THRESHOLD    # ¿vela con suficiente volumen?
    price_ok = close_price < ema8             # ¿precio por debajo de EMA8?

    log.info(
        f"🕯  VELA | close={close_price:.4f}  vol={volume:,.2f} HYPE  "
        f"EMA{EMA_PERIOD}={ema8:.4f}  pos={n}/3  roi={roi:+.3f}%  "
        f"vol✓={vol_ok}  price<EMA✓={price_ok}"
    )

    # ── Pos 1: entrada inicial ────────────────────────────
    if n == 0 and vol_ok and price_ok:
        await open_position(session, close_price, 0, volume, ema8)

    # ── Pos 2: DCA (ROI < umbral + condiciones de mercado) ──
    elif n == 1 and roi < DCA_ROI_TH and vol_ok and price_ok:
        await open_position(session, close_price, 1, volume, ema8)

    # ── Pos 3: DCA final ─────────────────────────────────
    elif n == 2 and roi < DCA_ROI_TH and vol_ok and price_ok:
        await open_position(session, close_price, 2, volume, ema8)

    # n == 3: máximo DCA alcanzado, solo monitorear TP via markPrice WS


async def open_position(
    session: aiohttp.ClientSession,
    price: float,
    idx: int,
    volume: float,
    ema8: float,
):
    """Registra una nueva posición DCA y notifica por Telegram."""
    size = DCA_SIZES[idx]
    qty  = size / price

    positions.append({
        "dca"  : idx + 1,
        "price": price,
        "size" : size,
        "qty"  : qty,
        "time" : now_utc(),
    })
    bot_state["alerts_count"] += 1

    roi_now    = combined_roi(price)
    entry_avg  = avg_entry_price()
    total_cost = sum(p["size"] for p in positions)

    labels = ["🟢 ENTRADA LONG", "🔵 DCA #2", "🟡 DCA #3"]
    label  = labels[idx]

    log.info(
        f"{label} @ {price:.6f} | size={size} USDT | qty={qty:.4f} | "
        f"avg={entry_avg:.6f} | roi={roi_now:+.3f}%"
    )

    await send_telegram(
        session,
        f"{label} — HYPEUSDT Futures\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Precio entrada:  <b>${price:.6f}</b>\n"
        f"📦 Tamaño pos:      <b>{size:.0f} USDT</b>  [{idx+1}/3]\n"
        f"💼 Total invertido: <b>{total_cost:.0f} USDT</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📈 EMA {EMA_PERIOD}:          <b>${ema8:.6f}</b>\n"
        f"📊 Volumen vela:    <b>{volume:,.2f} HYPE</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 Entry promedio:  <b>${entry_avg:.6f}</b>\n"
        f"📉 ROI combinado:   <b>{roi_now:+.3f}%</b>\n"
        f"🎯 TP objetivo:     <b>+{TP_ROI}%</b>  |  🔄 DCA si ROI &lt; {DCA_ROI_TH}%\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏱ Timeframe: 1 Minuto  |  🕐 {now_utc()}",
    )


async def check_tp(session: aiohttp.ClientSession, price: float):
    """
    Verifica si el ROI combinado alcanzó el Take Profit.
    Se llama cada vez que llega un update de markPrice (~1/s).
    """
    global positions

    if not positions:
        return

    roi = combined_roi(price)
    if roi < TP_ROI:
        return  # aún no llegamos al TP

    # ── TP alcanzado — cerrar todo ──────────────────────
    total_cost = sum(p["size"] for p in positions)
    total_qty  = sum(p["qty"]  for p in positions)
    pnl        = total_qty * price - total_cost
    entry_avg  = avg_entry_price()
    n          = len(positions)

    log.info(
        f"✅ TAKE PROFIT | roi={roi:+.3f}% | pnl=+{pnl:.4f} USDT | "
        f"{n} pos cerradas @ {price:.6f}"
    )

    closed_trades.append({
        "time"     : now_utc(),
        "n"        : n,
        "entry_avg": entry_avg,
        "exit"     : price,
        "cost"     : total_cost,
        "pnl"      : pnl,
        "roi"      : roi,
    })
    bot_state["tp_count"] += 1
    positions.clear()

    await send_telegram(
        session,
        f"✅ <b>TAKE PROFIT — HYPEUSDT</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Precio salida:   <b>${price:.6f}</b>\n"
        f"📊 Entry promedio:  <b>${entry_avg:.6f}</b>\n"
        f"📦 Posiciones:      <b>{n}/3</b>\n"
        f"💵 Invertido:       <b>{total_cost:.0f} USDT</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🟩 PnL:             <b>+{pnl:.4f} USDT</b>\n"
        f"📈 ROI combinado:   <b>+{roi:.3f}%</b>\n"
        f"🏆 TPs totales:     <b>{bot_state['tp_count']}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 {now_utc()}",
    )


# ══════════════════════════════════════════════════════════
#  WEBSOCKET — Combined stream Binance Futures
# ══════════════════════════════════════════════════════════
async def ws_loop(session: aiohttp.ClientSession):
    """
    WebSocket combinado (una sola conexión):
      • hypeusdt@kline_1m     → detecta cierre de vela y evalúa estrategia
      • hypeusdt@markPrice@1s → precio mark cada segundo para monitorear TP
    Se reconecta automáticamente ante cualquier error.
    """
    global current_price

    streams = (
        f"{SYMBOL.lower()}@kline_1m"
        f"/{SYMBOL.lower()}@markPrice@1s"
    )
    url = f"{BINANCE_WS}/stream?streams={streams}"

    while True:
        try:
            log.info(f"🔌 Conectando WS → {url}")
            async with session.ws_connect(
                url, heartbeat=30, receive_timeout=90
            ) as ws:
                bot_state["status"] = "✅ WS conectado"
                log.info("WebSocket activo")

                async for msg in ws:

                    if msg.type == aiohttp.WSMsgType.TEXT:
                        data   = json.loads(msg.data)
                        stream = data.get("stream", "")
                        d      = data.get("data",   {})

                        # ── Kline: cierre de vela ───────────────────
                        if "kline" in stream:
                            k = d.get("k", {})
                            if k.get("x"):   # x = True  →  vela cerrada
                                await on_candle_close(
                                    session,
                                    float(k["c"]),  # close price
                                    float(k["v"]),  # base asset volume (HYPE tokens)
                                    # k["q"] = USDT (precio×qty) — NO usar,
                                    # k["v"] = HYPE coins, coincide con el chart
                                )

                        # ── MarkPrice: precio cada segundo ──────────
                        elif "markPrice" in stream:
                            p = float(d.get("p", 0))
                            if p > 0:
                                current_price = p
                                await check_tp(session, current_price)

                    elif msg.type in (
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.ERROR,
                    ):
                        log.warning(f"WS cerrado/error: {msg.type}")
                        break

        except asyncio.CancelledError:
            log.info("ws_loop cancelado")
            break
        except Exception as e:
            bot_state["status"] = f"⚠️ WS error: {type(e).__name__}"
            log.error(f"WebSocket error: {e}")

        log.info(f"WS desconectado — reconectando en {WS_RECONNECT}s...")
        await asyncio.sleep(WS_RECONNECT)


# ══════════════════════════════════════════════════════════
#  HTTP DASHBOARD
# ══════════════════════════════════════════════════════════
def build_dashboard() -> str:
    roi_now   = combined_roi(current_price)
    cost_now  = sum(p["size"] for p in positions)
    pnl_now   = sum(p["qty"]  for p in positions) * current_price - cost_now
    roi_color = "#3fb950" if roi_now >= 0 else "#f85149"
    pnl_color = "#3fb950" if pnl_now >= 0 else "#f85149"

    # ── filas de posiciones abiertas ─────────────────────
    pos_rows = ""
    for p in positions:
        p_pnl   = p["qty"] * current_price - p["size"]
        p_roi   = (p["qty"] * current_price - p["size"]) / p["size"] * 100
        color   = "#3fb950" if p_pnl >= 0 else "#f85149"
        pos_rows += (
            f"<tr>"
            f"<td>DCA #{p['dca']}</td>"
            f"<td>${p['price']:.6f}</td>"
            f"<td>${current_price:.6f}</td>"
            f"<td>{p['size']:.0f} USDT</td>"
            f"<td style='color:{color}'>{p_pnl:+.4f} U</td>"
            f"<td style='color:{color}'>{p_roi:+.3f}%</td>"
            f"<td>{p['time']}</td>"
            f"</tr>"
        )

    # ── filas de TPs cerrados ─────────────────────────────
    closed_rows = ""
    for t in reversed(closed_trades[-15:]):
        closed_rows += (
            f"<tr>"
            f"<td>{t['n']} pos</td>"
            f"<td>${t['entry_avg']:.6f}</td>"
            f"<td>${t['exit']:.6f}</td>"
            f"<td style='color:#3fb950'>+{t['pnl']:.4f} U</td>"
            f"<td style='color:#3fb950'>+{t['roi']:.3f}%</td>"
            f"<td>{t['time']}</td>"
            f"</tr>"
        )

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="10">
<title>HYPEUSDT DCA Bot</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:#0d1117;color:#c9d1d9;font-family:'Courier New',monospace;padding:1rem}}
  h1{{color:#58a6ff;font-size:1.1rem;margin-bottom:.2rem}}
  h2{{color:#79c0ff;font-size:.8rem;margin:1rem 0 .3rem}}
  .sub{{font-size:.65rem;color:#8b949e;margin-bottom:.8rem}}
  .cards{{display:flex;flex-wrap:wrap;gap:.4rem;margin:.5rem 0}}
  .card{{background:#161b22;border:1px solid #30363d;border-radius:6px;
         padding:.4rem .7rem;min-width:130px}}
  .card .lbl{{font-size:.6rem;color:#8b949e}}
  .card .val{{font-size:.92rem;font-weight:bold;margin-top:.1rem}}
  .ok{{color:#3fb950}} .err{{color:#f85149}} .warn{{color:#d29922}}
  .wrap{{overflow-x:auto;margin-top:.3rem}}
  table{{border-collapse:collapse;width:100%;font-size:.72rem}}
  th,td{{border:1px solid #21262d;padding:.3rem .5rem;text-align:right;white-space:nowrap}}
  th{{background:#161b22;color:#8b949e;text-align:center}}
  td:first-child{{text-align:center}}
  .footer{{font-size:.62rem;color:#484f58;margin-top:.8rem}}
</style>
</head>
<body>

<h1>🤖 HYPEUSDT Futures — DCA Long Bot</h1>
<p class="sub">
  EMA{EMA_PERIOD} | Vol &gt; {VOL_THRESHOLD:,.0f} <b>HYPE</b>/vela | DCA si ROI &lt; {DCA_ROI_TH}% |
  TP {TP_ROI}% | Sizes: {DCA1:.0f}+{DCA2:.0f}+{DCA3:.0f} USDT | 1m | Binance Futures
</p>

<div class="cards">
  <div class="card">
    <div class="lbl">Estado WS</div>
    <div class="val ok">{bot_state['status']}</div>
  </div>
  <div class="card">
    <div class="lbl">Precio HYPE (mark)</div>
    <div class="val">${current_price:.6f}</div>
  </div>
  <div class="card">
    <div class="lbl">EMA {EMA_PERIOD}</div>
    <div class="val">${current_ema8:.6f}</div>
  </div>
  <div class="card">
    <div class="lbl">Precio vs EMA{EMA_PERIOD}</div>
    <div class="val {'ok' if current_price < current_ema8 else 'err'}">
      {'↓ BAJO EMA' if current_price < current_ema8 else '↑ SOBRE EMA'}
    </div>
  </div>
  <div class="card">
    <div class="lbl">Posiciones DCA</div>
    <div class="val {'ok' if positions else ''}">{len(positions)}/3</div>
  </div>
  <div class="card">
    <div class="lbl">ROI combinado</div>
    <div class="val" style="color:{roi_color}">{roi_now:+.3f}%</div>
  </div>
  <div class="card">
    <div class="lbl">PnL no realizado</div>
    <div class="val" style="color:{pnl_color}">{pnl_now:+.4f} USDT</div>
  </div>
  <div class="card">
    <div class="lbl">TPs logrados</div>
    <div class="val ok">{bot_state['tp_count']}</div>
  </div>
  <div class="card">
    <div class="lbl">Entradas totales</div>
    <div class="val warn">{bot_state['alerts_count']}</div>
  </div>
  <div class="card">
    <div class="lbl">Vol última vela</div>
    <div class="val {'ok' if bot_state['last_vol'] > VOL_THRESHOLD else ''}">{bot_state['last_vol']:,.2f} HYPE</div>
  </div>
  <div class="card">
    <div class="lbl">Última vela cerrada</div>
    <div class="val" style="font-size:.62rem">{bot_state['last_candle']}</div>
  </div>
  <div class="card">
    <div class="lbl">Velas históricas</div>
    <div class="val">{bot_state['candles_ok']}</div>
  </div>
</div>

<h2>📊 Posiciones Abiertas</h2>
<div class="wrap">
  <table>
    <thead>
      <tr>
        <th>DCA</th><th>Entrada</th><th>Precio actual</th>
        <th>Tamaño</th><th>PnL</th><th>ROI ind.</th><th>Abierto</th>
      </tr>
    </thead>
    <tbody>
      {pos_rows or
       '<tr><td colspan="7" style="text-align:center;color:#8b949e;padding:.6rem">'
       'Sin posiciones abiertas</td></tr>'}
    </tbody>
  </table>
</div>

<h2>✅ Take Profits cerrados (últimos 15)</h2>
<div class="wrap">
  <table>
    <thead>
      <tr>
        <th>Posiciones</th><th>Entry avg</th><th>Salida</th>
        <th>PnL</th><th>ROI</th><th>Hora</th>
      </tr>
    </thead>
    <tbody>
      {closed_rows or
       '<tr><td colspan="6" style="text-align:center;color:#8b949e;padding:.6rem">'
       'Sin TPs aún</td></tr>'}
    </tbody>
  </table>
</div>

<p class="footer">
  Actualización automática cada 10 s | {now_utc()} |
  Estrategia: HYPEUSDT Long DCA | EMA{EMA_PERIOD} + Vol&gt;{VOL_THRESHOLD:,.0f} HYPE | TP {TP_ROI}%
</p>

</body>
</html>"""


async def health_handler(request):
    return web.Response(text=build_dashboard(), content_type="text/html")


async def api_state_handler(request):
    return web.Response(
        text=json.dumps({
            "status"      : bot_state["status"],
            "symbol"      : SYMBOL,
            "price"       : current_price,
            "ema8"        : current_ema8,
            "positions"   : len(positions),
            "roi"         : round(combined_roi(current_price), 4),
            "tp_count"    : bot_state["tp_count"],
            "alerts_count": bot_state["alerts_count"],
            "last_candle" : bot_state["last_candle"],
            "last_vol"    : bot_state["last_vol"],
        }),
        content_type="application/json",
    )


async def start_http_server():
    app = web.Application()
    app.router.add_get("/",          health_handler)
    app.router.add_get("/health",    health_handler)
    app.router.add_get("/api/state", api_state_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info(f"📡 Dashboard activo → http://0.0.0.0:{PORT}")


# ══════════════════════════════════════════════════════════
#  BOT LOOP PRINCIPAL
# ══════════════════════════════════════════════════════════
async def bot_loop():
    async with aiohttp.ClientSession() as session:

        # ── Mensaje de inicio ─────────────────────────────
        await send_telegram(
            session,
            f"🤖 <b>HYPEUSDT DCA Bot — Iniciado</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 Par:         <b>HYPEUSDT</b> Futures Perpetual\n"
            f"📈 EMA:         <b>EMA {EMA_PERIOD}</b>  |  Timeframe: <b>1m</b>\n"
            f"📊 Vol mínimo:  <b>{VOL_THRESHOLD:,.0f} HYPE/vela</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💼 DCA Pos 1:   <b>{DCA1:.0f} USDT</b>  (vol&gt;{VOL_THRESHOLD:,.0f} + precio&lt;EMA{EMA_PERIOD})\n"
            f"💼 DCA Pos 2:   <b>{DCA2:.0f} USDT</b>  (ROI &lt; {DCA_ROI_TH}% + condición vol/EMA)\n"
            f"💼 DCA Pos 3:   <b>{DCA3:.0f} USDT</b>  (ROI &lt; {DCA_ROI_TH}% + condición vol/EMA)\n"
            f"💰 Máx expuesto:<b>{DCA1+DCA2+DCA3:.0f} USDT</b>\n"
            f"🎯 Take Profit: <b>ROI combinado ≥ {TP_ROI}%</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📡 WS: kline_1m + markPrice@1s\n"
            f"📚 Histórico: {KLINES_LIMIT} velas  |  Refresco: {REFRESH_MIN} min\n"
            f"🕐 {now_utc()}",
        )

        # ── Carga histórica inicial ───────────────────────
        log.info("Cargando velas históricas...")
        while not await fetch_klines(session):
            log.warning("Reintentando carga de klines en 15s...")
            await asyncio.sleep(15)

        # ── Lanzar WebSocket en tarea paralela ───────────
        asyncio.create_task(ws_loop(session))

        # ── Refresco periódico del histórico ─────────────
        last_refresh = time.time()
        while True:
            await asyncio.sleep(60)
            if time.time() - last_refresh >= REFRESH_MIN * 60:
                log.info("🔄 Refrescando histórico de velas...")
                await fetch_klines(session)
                last_refresh = time.time()


# ══════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════
async def main():
    log.info("╔══════════════════════════════════════════════════════╗")
    log.info("║      HYPEUSDT Futures — DCA Long Bot                 ║")
    log.info(f"║  EMA{EMA_PERIOD} | Vol>{VOL_THRESHOLD:,.0f} USDT | DCA@{DCA_ROI_TH}% | TP@{TP_ROI}%    ║")
    log.info(f"║  Sizes: {DCA1:.0f}+{DCA2:.0f}+{DCA3:.0f}={DCA1+DCA2+DCA3:.0f} USDT max | 1m | Binance Futures ║")
    log.info("╚══════════════════════════════════════════════════════╝")

    await asyncio.gather(
        start_http_server(),
        bot_loop(),
    )


if __name__ == "__main__":
    asyncio.run(main())
