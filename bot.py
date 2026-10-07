import os
import time
import requests
from iqoptionapi.stable_api import IQ_Option

# ==================== CONFIGURAÇÕES ====================
BOT_TOKEN = os.getenv(
    "BOT_TOKEN", "8820964764:AAF0-VvQQEJvFJiLZaQA9nSWkvk5ZWoCGtU"
)
CHAT_ID = os.getenv("CHAT_ID", "-1003565774427")

# Credenciais lidas do ambiente (Render)
IQ_USER = os.getenv("IQ_USER", "seu_email@exemplo.com")
IQ_PASS = os.getenv("IQ_PASS", "sua_senha_aqui")
IQ_MODE = os.getenv("IQ_MODE", "PRACTICE")  # PRACTICE ou REAL

PARES = ["EURUSD", "GBPUSD", "USDJPY", "EURJPY"]


# ==================== TELEGRAM ====================
def enviar_mensagem(texto):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": texto, "parse_mode": "Markdown"}
    try:
        res = requests.post(url, data=payload, timeout=10)
        return res.json()
    except Exception as e:
        print(f"[ERRO TELEGRAM] {e}")
        return None


# ==================== IQ OPTION ====================
def conectar_iq_option():
    print(f"Conectando à IQ Option (Modo: {IQ_MODE})...")
    API = IQ_Option(IQ_USER, IQ_PASS)
    API.connect()

    if API.check_connect():
        print("✅ Conectado com sucesso à IQ Option!")
        API.change_balance(IQ_MODE)
        return API
    else:
        print("❌ Falha na conexão com a IQ Option.")
        return None


# ==================== ESTRATÉGIA OFICIAL ====================
def analisar_estrategia(API, par):
    """M5 Trend Sweep & Exaustão de Volume em M1."""
    try:
        # 1. Dados M5 para Trend Sweep
        candles_m5 = API.get_candles(par, 300, 6, time.time())
        if not candles_m5 or len(candles_m5) < 5:
            return None

        topo_m5 = max(c["max"] for c in candles_m5[:-1])
        fundo_m5 = min(c["min"] for c in candles_m5[:-1])
        c_m5_atual = candles_m5[-1]

        # 2. Dados M1 para Exaustão e Reversão
        candles_m1 = API.get_candles(par, 60, 5, time.time())
        if not candles_m1 or len(candles_m1) < 3:
            return None

        m1_atual = candles_m1[-1]
        m1_anterior = candles_m1[-2]

        # CALL (Compra)
        sweep_fundo = (
            c_m5_atual["min"] < fundo_m5 and c_m5_atual["close"] > fundo_m5
        )
        exaustao_venda_m1 = (
            m1_atual["volume"] < m1_anterior["volume"]
            and m1_atual["close"] < m1_atual["open"]
        )
        rejeicao_m1_call = (m1_atual["close"] > m1_atual["open"]) or (
            (m1_atual["max"] - m1_atual["close"])
            < (m1_atual["close"] - m1_atual["min"])
        )

        if sweep_fundo and exaustao_venda_m1 and rejeicao_m1_call:
            return "CALL"

        # PUT (Venda)
        sweep_topo = (
            c_m5_atual["max"] > topo_m5 and c_m5_atual["close"] < topo_m5
        )
        exaustao_compra_m1 = (
            m1_atual["volume"] < m1_anterior["volume"]
            and m1_atual["close"] > m1_atual["open"]
        )
        rejeicao_m1_put = (m1_atual["close"] < m1_atual["open"]) or (
            (m1_atual["close"] - m1_atual["min"])
            < (m1_atual["max"] - m1_atual["close"])
        )

        if sweep_topo and exaustao_compra_m1 and rejeicao_m1_put:
            return "PUT"

    except Exception as e:
        print(f"[ERRO ANALISE] {par}: {e}")

    return None


# ==================== PROCESSAMENTO ====================
def executar_sinal(API, par, sinal):
    try:
        candles_atuais = API.get_candles(par, 60, 1, time.time())
        preco_entrada = candles_atuais[-1]["close"]
        emoji_direcao = "🟢 (COMPRA)" if sinal == "CALL" else "🔴 (VENDA)"

        msg_sinal = f"""
🚨 *SINAL DETECTADO - M5 TREND SWEEP* 📊

*PAR:* `{par}`
*AÇÃO:* {sinal} {emoji_direcao}
*ENTRADA:* `{preco_entrada}`
*TEMPO:* 5 Minutos (M5)

⚠️ _Respeite o seu gerenciamento!_
"""
        enviar_mensagem(msg_sinal)

        # Aguarda os 5 minutos (300 segundos) da vela
        time.sleep(300)

        c_fechamento = API.get_candles(par, 60, 1, time.time())
        preco_fechamento = c_fechamento[-1]["close"]

        resultado = "EMPATE"
        if sinal == "CALL":
            if preco_fechamento > preco_entrada:
                resultado = "WIN"
            elif preco_fechamento < preco_entrada:
                resultado = "LOSS"
        elif sinal == "PUT":
            if preco_fechamento < preco_entrada:
                resultado = "WIN"
            elif preco_fechamento > preco_entrada:
                resultado = "LOSS"

        if resultado == "WIN":
            msg_res = f"✅ *RESULTADO: WIN!* 🟢\n\n*PAR:* `{par}`\n*ENTRADA:* `{preco_entrada}`\n*FECHAMENTO:* `{preco_fechamento}`"
        elif resultado == "LOSS":
            msg_res = f"❌ *RESULTADO: LOSS* 🔴\n\n*PAR:* `{par}`\n*ENTRADA:* `{preco_entrada}`\n*FECHAMENTO:* `{preco_fechamento}`"
        else:
            msg_res = f"⚪ *RESULTADO: EMPATE (DOJI)* ⚪\n\n*PAR:* `{par}`\n*ENTRADA:* `{preco_entrada}`\n*FECHAMENTO:* `{preco_fechamento}`"

        enviar_mensagem(msg_res)

    except Exception as e:
        print(f"[ERRO EXECUCAO] {par}: {e}")


# ==================== LOOP PRINCIPAL ====================
if __name__ == "__main__":
    API = conectar_iq_option()

    while True:
        if not API or not API.check_connect():
            API = conectar_iq_option()
            time.sleep(5)
            continue

        for par in PARES:
            sinal = analisar_estrategia(API, par)
            if sinal:
                executar_sinal(API, par, sinal)

        time.sleep(15)
