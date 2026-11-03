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

# ------------------------------------------------------------ configuração
JANELA_ENTRADA = 60          # segundos após abrir a vela nova em que ainda aceita sinal
QTD_VELAS = 300              # candles M5 analisados
PAVIO_MIN = 0.4              # pavio mínimo (fração da vela) na rejeição

NOME_ESTRATEGIA = "Retração em Zona Forte"
EXPIRACAO_VELAS = int(os.getenv("EXPIRACAO_VELAS", "3"))      # 3 velas M5 = 15 min
SCORE_MIN = int(os.getenv("SCORE_MIN", "6"))                  # força mínima da zona (máx. 9)
COOLDOWN_ZONA = int(os.getenv("COOLDOWN_ZONA", "2700"))       # seg. sem repetir sinal na mesma zona
ZONAS_ATUALIZA = 900                                          # seg. entre recálculos das zonas

# Proteções
MAX_LOSS_SEGUIDOS = int(os.getenv("MAX_LOSS_SEGUIDOS", "3"))  # pausa após X losses seguidos
PAUSA_MIN = int(os.getenv("PAUSA_MIN", "60"))                 # minutos de pausa
HORA_INI = int(os.getenv("HORA_INI", "5"))                    # horário BRT (Londres ~05h)
HORA_FIM = int(os.getenv("HORA_FIM", "17"))                   # horário BRT (NY até ~17h)
# Janelas de notícia a evitar (BRT), ex.: "09:25-09:50,15:25-15:50"
BLOQUEIOS_TXT = os.getenv("JANELAS_BLOQUEIO", "")


def _parse_bloqueios(txt):
    out = []
    for item in txt.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            a, b = item.split("-")
            h1, m1 = a.split(":")
            h2, m2 = b.split(":")
            out.append((int(h1) * 60 + int(m1), int(h2) * 60 + int(m2)))
        except Exception:
            print(f"[AVISO] bloqueio inválido ignorado: {item}")
    return out


BLOQUEIOS = _parse_bloqueios(BLOQUEIOS_TXT)

# ------------------------------------------------------------ estado
ultimo_processado = {}
ws_lock = threading.Lock()
_ws = None
_req_id = 0

zonas_cache = {}
ultimo_sinal_zona = {}
sinais_abertos = set()
estado_lock = threading.Lock()

placar = {"dia": None, "WIN": 0, "LOSS": 0, "EMPATE": 0}
placar_lock = threading.Lock()
protecao = {"loss_seguidos": 0, "pausado_ate": 0.0}


@app.route('/')
def home():
    return "Bot Telegram M5 — Retração em Zonas Fortes operacional!", 200


@app.route('/placar')
def ver_placar():
    with placar_lock:
        return f"{placar['dia']}: {texto_placar(placar)}", 200


@app.route('/zonas')
def ver_zonas():
    linhas = []
    for par, d in zonas_cache.items():
        linhas.append(par)
        for z in d["zonas"]:
            if z["score"] >= SCORE_MIN:
                linhas.append(f"  {z['low']:.5f} - {z['high']:.5f} | força {z['score']} | {z['origem']}")
    return "\n".join(linhas) or "sem zonas ainda", 200, {"Content-Type": "text/plain; charset=utf-8"}


# ------------------------------------------------------------ utilidades
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
            protecao["loss_seguidos"] = 0
            protecao["pausado_ate"] = 0.0
    if resumo:
        enviar_mensagem(resumo)


def registrar_resultado(resultado):
    virar_dia_se_preciso()
    aviso_pausa = False
    with placar_lock:
        placar[resultado] += 1
        linha = texto_placar(placar)
        if resultado == "LOSS":
            protecao["loss_seguidos"] += 1
            if protecao["loss_seguidos"] >= MAX_LOSS_SEGUIDOS:
                protecao["pausado_ate"] = time.time() + PAUSA_MIN * 60
                protecao["loss_seguidos"] = 0
                aviso_pausa = True
        elif resultado == "WIN":
            protecao["loss_seguidos"] = 0
    if aviso_pausa:
        enviar_mensagem(
            f"⏸️ *BOT EM PAUSA*\n\n{MAX_LOSS_SEGUIDOS} losses seguidos. "
            f"Sem novos sinais por {PAUSA_MIN} minutos."
        )
    return linha


def em_pausa():
    with placar_lock:
        return time.time() < protecao["pausado_ate"]


def horario_permitido():
    agora = datetime.now(BRT)
    if agora.weekday() >= 5:
        return False
    if not (HORA_INI <= agora.hour < HORA_FIM):
        return False
    hm = agora.hour * 60 + agora.minute
    for ini, fim in BLOQUEIOS:
        if ini <= hm <= fim:
            return False
    return True


def enviar_mensagem(texto):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": texto, "parse_mode": "Markdown"}
    try:
        res = requests.post(url, data=payload, timeout=10)
        return res.json()
    except Exception as e:
        print(f"[ERRO TELEGRAM] {e}")
        return None


# ------------------------------------------------------------ Deriv
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


# ------------------------------------------------------------ indicadores
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


def casas(par):
    return 3 if "JPY" in par else 5


# ------------------------------------------------------------ zonas fortes
def pivots_simples(H, L, k):
    """Fractais: topo/fundo maior/menor que k velas de cada lado."""
    piv = []
    for j in range(k, len(H) - k):
        if H[j] == max(H[j - k:j + k + 1]):
            piv.append((j, "H", H[j]))
        if L[j] == min(L[j - k:j + k + 1]):
            piv.append((j, "L", L[j]))
    return piv


def montar_zonas(par, h1, h4, d1):
    """Agrupa pivôs de H1/H4 em zonas e dá nota de força (0 a 9).

    Pontuação:
      + toques (1 por pivô agrupado, máx. 4)
      + 2 se saiu impulso forte (>= 1,5x ATR) a partir da zona
      + 1 se a zona tem pivô de H4
      + 1 se coincide com número redondo (múltiplo de 50 pips)
      + 1 se coincide com máxima/mínima do dia anterior
    """
    pip = tamanho_pip(par)
    pts = []
    atr_ref = None

    for tf, velas, k in (("H1", h1, 3), ("H4", h4, 3)):
        if not velas:
            continue
        fech = velas[:-1]
        if len(fech) < 30:
            continue
        H = [float(c["high"]) for c in fech]
        L = [float(c["low"]) for c in fech]
        C = [float(c["close"]) for c in fech]
        atr = atr_serie(H, L, C)
        if tf == "H1":
            atr_ref = atr[-1]
        for j, tipo, nivel in pivots_simples(H, L, k):
            fim = min(len(H), j + 7)
            if fim > j + 1:
                if tipo == "H":
                    mov = nivel - min(L[j + 1:fim])
                else:
                    mov = max(H[j + 1:fim]) - nivel
            else:
                mov = 0.0
            pts.append({"nivel": nivel, "tf": tf, "imp": mov >= 1.5 * atr[j]})

    if not pts or not atr_ref:
        return []

    # níveis do dia anterior
    d1_niveis = []
    if d1 and len(d1) >= 2:
        d1_niveis = [float(d1[-2]["high"]), float(d1[-2]["low"])]

    tol = 0.5 * atr_ref
    pts.sort(key=lambda p: p["nivel"])
    grupos, atual = [], [pts[0]]
    for p in pts[1:]:
        if p["nivel"] - atual[0]["nivel"] <= tol:
            atual.append(p)
        else:
            grupos.append(atual)
            atual = [p]
    grupos.append(atual)

    passo_redondo = 50 * pip
    zonas = []
    for g in grupos:
        low = min(p["nivel"] for p in g)
        high = max(p["nivel"] for p in g)
        if high - low < 0.2 * atr_ref:
            pad = 0.1 * atr_ref
            low -= pad
            high += pad
        centro = (low + high) / 2

        score = min(len(g), 4)
        origem = set(p["tf"] for p in g)
        if any(p["imp"] for p in g):
            score += 2
        if "H4" in origem:
            score += 1
        m = round(centro / passo_redondo) * passo_redondo
        if low - 0.1 * atr_ref <= m <= high + 0.1 * atr_ref:
            score += 1
        if any(low - 0.1 * atr_ref <= n <= high + 0.1 * atr_ref for n in d1_niveis):
            score += 1

        zonas.append({
            "low": low, "high": high, "score": score,
            "origem": "+".join(sorted(origem)), "toques": len(g),
        })

    zonas.sort(key=lambda z: z["score"], reverse=True)
    return zonas


def obter_zonas(par):
    agora = time.time()
    c = zonas_cache.get(par)
    if c and agora - c["ts"] < ZONAS_ATUALIZA:
        return c["zonas"]
    h1 = pegar_velas(par, 3600, 500)
    h4 = pegar_velas(par, 14400, 250)
    d1 = pegar_velas(par, 86400, 5)
    if not h1 and not h4:
        return c["zonas"] if c else []
    zonas = montar_zonas(par, h1, h4, d1)
    zonas_cache[par] = {"ts": agora, "zonas": zonas}
    fortes = [z for z in zonas if z["score"] >= SCORE_MIN]
    print(f"[ZONAS] {par}: {len(zonas)} zonas, {len(fortes)} fortes (>= {SCORE_MIN})")
    return zonas


# ------------------------------------------------------------ gatilho M5
def sinal_retracao(par, O, H, L, C, zonas, atr5):
    """Preço volta à zona forte e a vela M5 fechada mostra rejeição."""
    pip = tamanho_pip(par)
    i = len(C) - 1
    faixa = H[i] - L[i]
    if faixa < 1.5 * pip:
        return None
    pavio_inf = min(O[i], C[i]) - L[i]
    pavio_sup = H[i] - max(O[i], C[i])
    tol = max(2 * pip, 0.3 * atr5)
    anteriores = C[i - 8:i]

    for z in zonas:
        if z["score"] < SCORE_MIN:
            continue
        chave = (par, round(z["low"], 5))
        if time.time() - ultimo_sinal_zona.get(chave, 0) < COOLDOWN_ZONA:
            continue

        # SUPORTE: veio de cima, pavio entrou na zona, fechou acima dela
        if sum(1 for x in anteriores if x > z["high"]) >= 6:
            if (L[i] <= z["high"] + tol and L[i] >= z["low"] - tol
                    and C[i] > z["high"] and C[i] > O[i]
                    and C[i] >= L[i] + 0.5 * faixa
                    and pavio_inf >= PAVIO_MIN * faixa):
                ultimo_sinal_zona[chave] = time.time()
                return "CALL", z

        # RESISTÊNCIA: veio de baixo, pavio entrou na zona, fechou abaixo dela
        if sum(1 for x in anteriores if x < z["low"]) >= 6:
            if (H[i] >= z["low"] - tol and H[i] <= z["high"] + tol
                    and C[i] < z["low"] and C[i] < O[i]
                    and C[i] <= H[i] - 0.5 * faixa
                    and pavio_sup >= PAVIO_MIN * faixa):
                ultimo_sinal_zona[chave] = time.time()
                return "PUT", z
    return None


def analisar_estrategia(par, velas):
    fechadas = velas[:-1]
    if len(fechadas) < 60:
        return None
    O = [float(c["open"]) for c in fechadas]
    H = [float(c["high"]) for c in fechadas]
    L = [float(c["low"]) for c in fechadas]
    C = [float(c["close"]) for c in fechadas]
    atr5 = atr_serie(H, L, C)[-1]

    zonas = obter_zonas(par)
    if not zonas:
        return None

    r = sinal_retracao(par, O, H, L, C, zonas, atr5)
    if r:
        sinal, zona = r
        return sinal, NOME_ESTRATEGIA, EXPIRACAO_VELAS, zona
    return None


# ------------------------------------------------------------ execução
def executar_sinal(par, sinal, epoch_vela, estrategia, exp_velas, zona):
    try:
        preco_entrada = preco_atual(par)
        if preco_entrada is None:
            return
        emoji = "🟢 (COMPRA)" if sinal == "CALL" else "🔴 (VENDA)"
        nome = par.replace("frx", "")
        minutos = 5 * exp_velas
        tempo = f"{minutos} Minutos ({exp_velas} vela{'s' if exp_velas > 1 else ''} M5)"
        d = casas(par)
        faixa_zona = f"{zona['low']:.{d}f} - {zona['high']:.{d}f}"

        enviar_mensagem(f"""
🚨 *SINAL DETECTADO* 📊

*ESTRATÉGIA:* {estrategia}
*PAR:* `{nome}`
*AÇÃO:* {sinal} {emoji}
*ENTRADA:* `{preco_entrada}`
*EXPIRAÇÃO:* {tempo}

*ZONA:* `{faixa_zona}`
*FORÇA:* {zona['score']}/9 ({zona['origem']}, {zona['toques']} toque(s))

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
    finally:
        with estado_lock:
            sinais_abertos.discard(par)


def loop_principal():
    teste = pegar_velas(PARES[0], 300, 3)
    if teste:
        print(f"✅ Dados da Deriv funcionando. Pares: {PARES}")
        print(f"🎯 {NOME_ESTRATEGIA} | força mínima {SCORE_MIN} | expiração {EXPIRACAO_VELAS} velas M5")
    else:
        print("❌ Não consegui ler dados da Deriv (confira o par ou o mercado aberto)")

    while True:
        virar_dia_se_preciso()
        for par in PARES:
            try:
                velas = pegar_velas(par, 300, QTD_VELAS)
                if not velas or len(velas) < 100:
                    continue
                epoch = int(velas[-1]["epoch"])
                if ultimo_processado.get(par) == epoch:
                    continue
                ultimo_processado[par] = epoch
                if time.time() - epoch > JANELA_ENTRADA:
                    continue
                if em_pausa() or not horario_permitido():
                    continue
                with estado_lock:
                    if par in sinais_abertos:
                        continue
                resultado = analisar_estrategia(par, velas)
                if resultado:
                    sinal, estrategia, exp_velas, zona = resultado
                    with estado_lock:
                        sinais_abertos.add(par)
                    threading.Thread(
                        target=executar_sinal,
                        args=(par, sinal, epoch, estrategia, exp_velas, zona),
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
