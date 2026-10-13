import os
import json
import threading
import time
import requests
import websocket
from flask import Flask

app = Flask(__name__)


@app.route('/')
def home():
    return "Bot Telegram M5 Trend Sweep operacional!", 200


BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")
WS_URL = os.getenv("DERIV_WS_URL", "wss://api.derivws.com/trading/v1/options/ws/public")
PARES = [p.strip() for p in os.getenv("PARES", "frxEURUSD,frxGBPUSD,frxUSDJPY,frxEURJPY").split(",") if p.strip()]

ultimo_sinal = {}
ws_lock = threading.Lock()
_ws = None
_req_id = 0


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
    """Envia um pedido à Deriv (uma conexão, um pedido por vez)."""
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


def volumes_m1(par):
    """Conta ticks por minuto. Retorna (minuto atual, minuto anterior)."""
    resp = pedir({"ticks_history": par, "count": 2000, "end": "latest", "style": "ticks"})
    try:
        tempos = [int(t) for t in resp["history"]["times"]]
    except Exception:
        return None
    if not tempos:
        return None
    minuto = tempos[-1] // 60
    atual = sum(1 for t in tempos if t // 60 == minuto)
    anterior = sum(1 for t in tempos if t // 60 == minuto - 1)
    return atual, anterior


def analisar_estrategia(par):
    try:
        m5 = pegar_velas(par, 300, 6)
        if not m5 or len(m5) < 5:
            return None
        m1 = pegar_velas(par, 60, 5)
        if not m1 or len(m1) < 3:
            return None

        topo_m5 = max(float(c['high']) for c in m5[:-1])
        fundo_m5 = min(float(c['low']) for c in m5[:-1])
        m5_atual = m5[-1]

        a = m1[-1]
        a_open, a_close = float(a['open']), float(a['close'])
        a_high, a_low = float(a['high']), float(a['low'])

        vols = volumes_m1(par)
        vol_menor = bool(vols) and vols[0] < vols[1]

        sweep_fundo = float(m5_atual['low']) < fundo_m5 and float(m5_atual['close']) > fundo_m5
        exaustao_venda = vol_menor and a_close < a_open
        rejeicao_call = a_close > a_open or (a_high - a_close) < (a_close - a_low)
        if sweep_fundo and exaustao_venda and rejeicao_call:
            return "CALL"

        sweep_topo = float(m5_atual['high']) > topo_m5 and float(m5_atual['close']) < topo_m5
        exaustao_compra = vol_menor and a_close > a_open
        rejeicao_put = a_close < a_open or (a_close - a_low) < (a_high - a_close)
        if sweep_topo and exaustao_compra and rejeicao_put:
            return "PUT"

    except Exception as e:
        print(f"[ERRO ANALISE] {par}: {e}")
    return None


def executar_sinal(par, sinal):
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

        time.sleep(300)

        preco_fechamento = preco_atual(par)
        if preco_fechamento is None:
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

        detalhes = f"\n\n*PAR:* `{nome}`\n*ENTRADA:* `{preco_entrada}`\n*FECHAMENTO:* `{preco_fechamento}`"
        if resultado == "WIN":
            enviar_mensagem("✅ *RESULTADO: WIN!* 🟢" + detalhes)
        elif resultado == "LOSS":
            enviar_mensagem("❌ *RESULTADO: LOSS* 🔴" + detalhes)
        else:
            enviar_mensagem("⚪ *RESULTADO: EMPATE (DOJI)* ⚪" + detalhes)

    except Exception as e:
        print(f"[ERRO EXECUCAO] {par}: {e}")


def loop_principal():
    teste = pegar_velas(PARES[0], 60, 3)
    if teste:
        print(f"✅ Dados da Deriv funcionando. Pares: {PARES}")
    else:
        print("❌ Não consegui ler dados da Deriv (confira o par ou o mercado aberto)")

    while True:
        for par in PARES:
            if time.time() - ultimo_sinal.get(par, 0) < 300:
                continue
            sinal = analisar_estrategia(par)
            if sinal:
                ultimo_sinal[par] = time.time()
                threading.Thread(target=executar_sinal, args=(par, sinal), daemon=True).start()
        time.sleep(15)


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
