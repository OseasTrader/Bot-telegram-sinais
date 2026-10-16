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
QTD_VELAS = 300
PIVOT_K = 6
PIVOT_LOOKBACK = 288

ultimo_processado = {}
quebras_usadas = set()
ws_lock = threading.Lock()
_ws = None
_req_id = 0

placar = {"dia": None, "WIN": 0, "LOSS": 0, "EMPATE": 0}
placar_lock = threading.Lock()

# ⏱️ CONFIGURAÇÃO DE EXPIRAÇÃO POR ESTRATÉGIA
EXPIRACAO = {
    "M5 Trend Sweep": 1,           # 5 minutos
    "Rejeição Dupla na Zona Chave": 3,    # 15 minutos ✅
    "Quebra e Reteste Limpos": 4,         # 20 minutos ✅
    "Absorção de Força": 3                 # 15 minutos ✅
}

@app.route('/')
def home():
    return "Bot Telegram M5 — Expirações Corretas Operacional!", 200


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
    resp = pedir({"ticks_history": par, "count": 5000, "end": "latest", "style": "ticks"})
    try:
        tempos = [int(t) for t in resp["history"]["times"]]
    except Exception:
        return {}
    vols = {}
    for t in tempos:
        b = t // 300 * 300
        vols[b] = vols.get(b, 0) + 1
    if len(vols) > 1:
        vols.pop(min(vols))
    return vols


def ema(valores, periodo):
    k = 2 / (periodo + 1)
    e = sum(valores[:periodo]) / periodo
    for v in valores[periodo:]:
        e = v * k + e * (1 - k)
    return e


def rsi_serie(closes, periodo=14):
    a = 1.0 / periodo
    au = ad = 0.0
    out = [50.0]
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        au += a * (max(d, 0.0) - au)
        ad += a * (max(-d, 0.0) - ad)
        out.append(100 - 100 / (1 + au / (ad if ad else 1e-9)))
    return out


def atr_serie(H, L, C, periodo=14):
    a = 1.0 / periodo
    out = []
    atr = 0.0
    for i in range(len(C)):
        pc = C[i - 1] if i > 0 else C[i]
        tr = max(H[i] - L[i], abs(H[i] - pc), abs(L[i] - pc))
        atr = tr if i == 0 else atr + a * (tr - atr)
        out.append(atr)
    return out


def tamanho_pip(par):
    return 0.01 if "JPY" in par else 0.0001


def calcular_pivots(H, L, k=PIVOT_K):
    piv = []
    for j in range(k, len(H) - k):
        if H[j] == max(H[j - k:j + k + 1]):
            piv.append((j + k, H[j]))
        if L[j] == min(L[j - k:j + k + 1]):
            piv.append((j + k, L[j]))
    piv.sort(key=lambda x: x[0])
    return piv


def niveis_em(piv, i):
    return [lv for (ci, lv) in piv if i - PIVOT_LOOKBACK <= ci <= i]


# ---------------------------------------------------------------- estratégias

def estrategia_trend_sweep(par, fechadas, ema20, ema50, vols):
    s = fechadas[-1]
    p1, p2, p3 = fechadas[-2], fechadas[-3], fechadas[-4]
    o, h, l, c = float(s['open']), float(s['high']), float(s['low']), float(s['close'])
    faixa = h - l
    if faixa <= 0:
        return None

    vs = vols.get(int(s['epoch']), 0)
    v1 = vols.get(int(p1['epoch']), 0)
    v2 = vols.get(int(p2['epoch']), 0)

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


def estrategia_rejeicao_dupla(par, O, H, L, C, rsi, ema20, ema50, piv):
    pip = tamanho_pip(par)
    tol = 2 * pip
    i = len(C) - 1
    a, b = i - 1, i
    faixa_a, faixa_b = H[a] - L[a], H[b] - L[b]
    if faixa_a < 1.5 * pip or faixa_b < 1.5 * pip:
        return None
    corpo_a, corpo_b = abs(C[a] - O[a]), abs(C[b] - O[b])
    lv = niveis_em(piv, i)

    if ema20 > ema50:
        wa = min(O[a], C[a]) - L[a]
        wb = min(O[b], C[b]) - L[b]
        if wa >= 2 * corpo_a and wa >= .5 * faixa_a and wb >= 2 * corpo_b and wb >= .5 * faixa_b:
            if min(rsi[i - 3:i + 1]) < 30 and rsi[i] > 30 and rsi[i] > rsi[i - 1]:
                m = min(L[a], L[b])
                if any(m - tol <= x <= m + tol and x < C[a] and x < C[b] for x in lv):
                    return "CALL"

    if ema20 < ema50:
        wa = H[a] - max(O[a], C[a])
        wb = H[b] - max(O[b], C[b])
        if wa >= 2 * corpo_a and wa >= .5 * faixa_a and wb >= 2 * corpo_b and wb >= .5 * faixa_b:
            if max(rsi[i - 3:i + 1]) > 70 and rsi[i] < 70 and rsi[i] < rsi[i - 1]:
                m = max(H[a], H[b])
                if any(m - tol <= x <= m + tol and x > C[a] and x > C[b] for x in lv):
                    return "PUT"
    return None


def estrategia_quebra_reteste(par, epochs, O, H, L, C, rsi, atr, piv, vols):
    pip = tamanho_pip(par)
    tol = 2 * pip
    j = len(C) - 1
    for k in range(2, 5):
        i = j - k
        if i < 60:
            continue
        if (par, epochs[i]) in quebras_usadas:
            continue
        vi = vols.get(epochs[i], 0)
        ref = [vols.get(epochs[x], 0) for x in range(i - 20, i)]
        ref = [v for v in ref if v > 0]
        if vi == 0 or len(ref) < 8:
            continue
        if vi < 1.3 * (sum(ref) / len(ref)):
            continue
        if abs(C[i] - O[i]) < .5 * atr[i]:
            continue
        lv = niveis_em(piv, i - 1)

        cand = [x for x in lv if C[i] > x and C[i - 1] <= x]
        if cand:
            lvl = cand[0]
            if not any(C[x] < lvl for x in range(i + 1, j)):
                if L[j] <= lvl + tol and C[j] > lvl and 50 < rsi[j] < 70:
                    quebras_usadas.add((par, epochs[i]))
                    return "CALL"

        cand = [x for x in lv if C[i] < x and C[i - 1] >= x]
        if cand:
            lvl = cand[-1]
            if not any(C[x] > lvl for x in range(i + 1, j)):
                if H[j] >= lvl - tol and C[j] < lvl and 30 < rsi[j] < 50:
                    quebras_usadas.add((par, epochs[i]))
                    return "PUT"
    return None


def estrategia_absorcao(par, O, H, L, C, rsi, ema20, ema50):
    pip = tamanho_pip(par)
    i = len(C) - 1
    a, b = i - 1, i
    if a < 21:
        return None
    corpos = [abs(C[x] - O[x]) for x in range(a - 20, a)]
    media_corpo = sum(corpos) / len(corpos)
    corpo_a, corpo_b = abs(C[a] - O[a]), abs(C[b] - O[b])
    faixa_a = H[a] - L[a]
    if faixa_a <= 0:
        return None
    forte = corpo_a >= 1.2 * media_corpo and corpo_a >= .7 * faixa_a and corpo_a >= 2 * pip
    if not forte or corpo_b >= .3 * corpo_a:
        return None
    meio = (O[a] + C[a]) / 2

    if C[a] > O[a]:
        if (C[a] >= H[a] - .15 * faixa_a and min(C[b], O[b]) >= meio and rsi[i] > rsi[a]
                and rsi[i] < 70 and C[i] > ema20 and C[i] > ema50):
            return "CALL"
    else:
        if (C[a] <= L[a] + .15 * faixa_a and max(C[b], O[b]) <= meio and rsi[i] < rsi[a]
                and rsi[i] > 30 and C[i] < ema20 and C[i] < ema50):
            return "PUT"
    return None


def analisar_estrategia(par, velas):
    fechadas = velas[:-1]
    if len(fechadas) < 100:
        return None

    epochs = [int(c['epoch']) for c in fechadas]
    O = [float(c['open']) for c in fechadas]
    H = [float(c['high']) for c in fechadas]
    L = [float(c['low']) for c in fechadas]
    C = [float(c['close']) for c in fechadas]

    ema20, ema50 = ema(C, 20), ema(C, 50)
    rsi = rsi_serie(C)
    atr = atr_serie(H, L, C)
    piv = calcular_pivots(H, L)
    vols = volumes_por_vela(par)

    sinal = estrategia_trend_sweep(par, fechadas, ema20, ema50, vols)
    if sinal:
        nome = "M5 Trend Sweep"
        return sinal, nome, EXPIRACAO[nome]

    sinal = estrategia_rejeicao_dupla(par, O, H, L, C, rsi, ema20, ema50, piv)
    if sinal:
        nome = "Rejeição Dupla na Zona Chave"
        return sinal, nome, EXPIRACAO[nome]

    sinal = estrategia_quebra_reteste(par, epochs, O, H, L, C, rsi, atr, piv, vols)
    if sinal:
        nome = "Quebra e Reteste Limpos"
        return sinal, nome, EXPIRACAO[nome]

    sinal = estrategia_absorcao(par, O, H, L, C, rsi, ema20, ema50)
    if sinal:
        nome = "Absorção de Força"
        return sinal, nome, EXPIRACAO[nome]

    return None


# ---------------------------------------------------------------- execução

def executar_sinal(par, sinal, epoch_vela, estrategia, exp_velas):
    try:
        preco_entrada = preco_atual(par)
        if preco_entrada is None:
            return
        emoji = "🟢 (COMPRA)" if sinal == "CALL" else "🔴 (VENDA)"
        nome = par.replace("frx", "")
        minutos = 5 * exp_velas
        tempo = f"{minutos} Minutos ({exp_velas} vela{'s' if exp_velas > 1 else ''} M5)"

        enviar_mensagem(f"""
🚨 *SINAL DETECTADO* 📊

*ESTRATÉGIA:* {estrategia}
*PAR:* `{nome}`
*AÇÃO:* {sinal} {emoji}
*ENTRADA:* `{preco_entrada}`
*EXPIRAÇÃO:* {tempo}

⚠️ _Respeite o seu gerenciamento!_
""")

        espera = (epoch_vela + 300 * exp_velas + 3) - time.time()
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
            f"\n\n*ESTRATÉGIA:* {estrategia}\n*PAR:* `{nome}`\n*ENTRADA:* `{preco_entrada}`"
            f"\n*FECHAMENTO:* `{preco_fechamento}`"
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
        print(f"⏱️ Expirações configuradas: Rejeição=15min | Quebra=20min | Absorção=15min | Trend=5min")
    else:
        print("❌ Não consegui ler dados da Deriv (confira o par ou o mercado aberto)")

    while True:
        virar_dia_se_preciso()
        for par in PARES:
            try:
                velas = pegar_velas(par, 300, QTD_VELAS)
                if not velas or len(velas) < 100:
                    continue
                epoch = int(velas[-1]['epoch'])
                if ultimo_processado.get(par) == epoch:
                    continue
                ultimo_processado[par] = epoch
                if time.time() - epoch > JANELA_ENTRADA:
                    continue
                resultado = analisar_estrategia(par, velas)
                if resultado:
                    sinal, estrategia, exp_velas = resultado
                    threading.Thread(
                        target=executar_sinal,
                        args=(par, sinal, epoch, estrategia, exp_velas),
                        daemon=True,
                    ).start()
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
