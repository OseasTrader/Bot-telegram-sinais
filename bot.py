import os
import json
import threading
import time
from datetime import datetime, timedelta, timezone
import requests
import websocket
from flask import Flask

app = Flask(__name__)

BRT = timezone(timedelta(hours=-3))

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")
WS_URL = os.getenv("DERIV_WS_URL", "wss://api.derivws.com/trading/v1/options/ws/public")
PARES = [p.strip() for p in os.getenv("PARES", "frxEURUSD,frxGBPUSD,frxUSDJPY,frxEURJPY").split(",") if p.strip()]

JANELA_ENTRADA = 60
PAVIO_MIN = 0.4

ultimo_processado = {}
ws_lock = threading.Lock()
_ws = None
_req_id = 0

placar = {"dia": None, "WIN": 0, "LOSS": 0, "EMPATE": 0}
placar_lock = threading.Lock()


@app.route('/')
def home():
    return "Bot Telegram M5 Trend Sweep operacional!", 200


@app.route('/placar')
def ver_placar():
    with placar_lock:
        return f"{placar['dia']}: {texto_placar(placar)}", 200


def hoje_brt():
    return datetime.now(BRT).strftime("%d/%m/%Y")


def texto_placar(p):
    decididos = p["WIN"] + p["LOSS"]
    taxa = f"{p['WIN'] / decididos * 100:.0f}%" if decididos else "-"
    return f"✅ {p['WIN']} | ❌ {p['LOSS']} | ⚪ {p['EMPATE']} | Acerto: {taxa}"


def virar_dia_se_preciso():
    dia = hoje_brt()
    resumo = None
    with placar_lock:
        if placar["dia"] != dia:
            total = placar["WIN"] + placar["LOSS"] + placar["EMPATE"]
            if placar["dia"] and total > 0:
                resumo = f"📋 *RESUMO DO DIA {placar['dia']}*\n\n" + texto_placar(placar)
            placar.update({"dia": dia, "WIN": 0, "LOSS": 0, "EMPATE": 0})
    if resumo:
        enviar_mensagem(resumo)


def registrar_resultado(resultado):
    virar_dia_se_preciso()
    with placar_lock:
        placar[resultado] += 1
        return texto_placar(placar)


def enviar_mensagem(texto):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": texto, "parse_mode": "Markdown"}
    try:
        res = requests.post(url, data=payload, timeout=10)
        return res.json()
    except Exception as e:
        print(f"[ERRO TELEGRAM] {e}")
        return None


def pedir(payload):
    global _ws, _req_id
    with ws_lock:
        for _ in range(2):
            try:
                if _ws is None:
                    _ws = websocket.create_connection(WS_URL, timeout=15)
                _req_id += 1
                payload["req_id"] = _req_id
                _ws.send(json.dumps(payload))
                for _ in range(10):
                    resp = json.loads(_ws.recv())
                    if resp.get("req_id") == _req_id:
                        return resp
            except Exception as e:
                print(f"[ERRO WS] {e}")
                try:
                    if _ws:
                        _ws.close()
                except Exception:
                    pass
                _ws = None
        return None


def pegar_velas(par, granularidade, quantidade):
    resp = pedir({
        "ticks_history": par,
        "adjust_start_time": 1,
        "count": quantidade,
        "end": "latest",
        "granularity": granularidade,
        "style": "candles",
    })
    if not resp or "error" in resp:
        erro = resp.get("error") if resp else "sem resposta"
        print(f"[ERRO DERIV] {par}: {erro}")
        return None
    return resp.get("candles")


def preco_atual(par):
    resp = pedir({"ticks_history": par, "count": 1, "end": "latest", "style": "ticks"})
    try:
        return float(resp["history"]["prices"][-1])
    except Exception as e:
        print(f"[ERRO PRECO] {par}: {e}")
        return None


def volumes_por_vela(par):
    resp = pedir({"ticks_history": par, "count": 3000, "end": "latest", "style": "ticks"})
    try:
        tempos = [int(t) for t in resp["history"]["times"]]
    except Exception:
        return {}
    vols = {}
    for t in tempos:
        b = t // 300 * 300
        vols[b] = vols.get(b, 0) + 1
    return vols


def ema(valores, periodo):
    k = 2 / (periodo + 1)
    e = sum(valores[:periodo]) / periodo
    for v in valores[periodo:]:
        e = v * k + e * (1 - k)
    return e


def analisar_estrategia(par, velas):
    nome = par.replace("frx", "")
    fechadas = velas[:-1]
    if len(fechadas) < 60:
        return None

    s, p1, p2, p3 = fechadas[-1], fechadas[-2], fechadas[-3], fechadas[-4]
    closes = [float(c['close']) for c in fechadas]
    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)

    o, h, l, c = float(s['open']), float(s['high']), float(s['low']), float(s['close'])
    faixa = h - l
    if faixa <= 0:
        return None

    vols = volumes_por_vela(par)
    vs = vols.get(int(s['epoch']), 0)
    v1 = vols.get(int(p1['epoch']), 0)
    v2 = vols.get(int(p2['epoch']), 0)
    print(f"[{nome}] M5 fechada | EMA20={ema20:.5f} EMA50={ema50:.5f} | volume {v2}>{v1}>{vs}")

    if min(vs, v1, v2) == 0:
        return None
    if not (vs < v1 and vs < v2):
        return None

    pavio_sup = h - max(o, c)
    pavio_inf = min(o, c) - l

    if ema20 < ema50:
        pullback_alta = float(p1['close']) > float(p3['close'])
        testou = h >= float(p1['high']) or h >= ema20
        rejeicao = pavio_sup >= PAVIO_MIN * faixa and c <= l + faixa / 3
        if pullback_alta and testou and rejeicao:
            return "PUT"

    if ema20 > ema50:
        pullback_baixa = float(p1['close']) < float(p3['close'])
        testou = l <= float(p1['low']) or l <= ema20
        rejeicao = pavio_inf >= PAVIO_MIN * faixa and c >= h - faixa / 3
        if pullback_baixa and testou and rejeicao:
            return "CALL"

    return None


def executar_sinal(par, sinal, epoch_vela):
    try:
        preco_entrada = preco_atual(par)
        if preco_entrada is None:
            return
        emoji = "🟢 (COMPRA)" if sinal == "CALL" else "🔴 (VENDA)"
        nome = par.replace("frx", "")

        enviar_mensagem(f"""
🚨 *SINAL DETECTADO - M5 TREND SWEEP* 📊

*PAR:* `{nome}`
*AÇÃO:* {sinal} {emoji}
*ENTRADA:* `{preco_entrada}`
*TEMPO:* 5 Minutos (M5)

⚠️ _Respeite o seu gerenciamento!_
""")

        espera = (epoch_vela + 300 + 3) - time.time()
        if espera > 0:
            time.sleep(espera)

        preco_fechamento = None
        for _ in range(3):
            preco_fechamento = preco_atual(par)
            if preco_fechamento is not None:
                break
            time.sleep(5)
        if preco_fechamento is None:
            enviar_mensagem(f"⚠️ Não consegui obter o fechamento de `{nome}`. Confira o resultado no gráfico.")
            return

        resultado = "EMPATE"
        if sinal == "CALL":
            if preco_fechamento > preco_entrada:
                resultado = "WIN"
            elif preco_fechamento < preco_entrada:
                resultado = "LOSS"
        else:
            if preco_fechamento < preco_entrada:
                resultado = "WIN"
            elif preco_fechamento > preco_entrada:
                resultado = "LOSS"

        linha_placar = registrar_resultado(resultado)
        detalhes = (
            f"\n\n*PAR:* `{nome}`\n*ENTRADA:* `{preco_entrada}`\n*FECHAMENTO:* `{preco_fechamento}`"
            f"\n\n📊 *Placar do dia:* {linha_placar}"
        )
        if resultado == "WIN":
            enviar_mensagem("✅ *RESULTADO: WIN!* 🟢" + detalhes)
        elif resultado == "LOSS":
            enviar_mensagem("❌ *RESULTADO: LOSS* 🔴" + detalhes)
        else:
            enviar_mensagem("⚪ *RESULTADO: EMPATE (DOJI)* ⚪" + detalhes)

    except Exception as e:
        print(f"[ERRO EXECUCAO] {par}: {e}")


def loop_principal():
    teste = pegar_velas(PARES[0], 300, 3)
    if teste:
        print(f"✅ Dados da Deriv funcionando. Pares: {PARES}")
    else:
        print("❌ Não consegui ler dados da Deriv (confira o par ou o mercado aberto)")

    while True:
        virar_dia_se_preciso()
        for par in PARES:
            try:
                velas = pegar_velas(par, 300, 120)
                if not velas or len(velas) < 60:
                    continue
                epoch = int(velas[-1]['epoch'])
                if ultimo_processado.get(par) == epoch:
                    continue
                ultimo_processado[par] = epoch
                if time.time() - epoch > JANELA_ENTRADA:
                    continue
                sinal = analisar_estrategia(par, velas)
                if sinal:
                    threading.Thread(target=executar_sinal, args=(par, sinal, epoch), daemon=True).start()
            except Exception as e:
                print(f"[ERRO LOOP] {par}: {e}")
        time.sleep(10)


_iniciado = False


def iniciar_bot():
    global _iniciado
    if not _iniciado:
        _iniciado = True
        threading.Thread(target=loop_principal, daemon=True).start()


iniciar_bot()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
