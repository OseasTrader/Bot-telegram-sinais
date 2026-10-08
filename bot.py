import os
import threading
import time
from flask import Flask
from iqoptionapi.api import IQOptionAPI
import requests

# Servidor Web para manter o Web Service do Render ativo gratuitamente
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot Telegram IQ Option operacional!", 200

# ==================== CONFIGURAÇÕES ====================
BOT_TOKEN = os.getenv("BOT_TOKEN", "8820964764:AAF0-VvQQEJvFJiLZaQA9nSWkvk5ZWoCGtU")
CHAT_ID = os.getenv("CHAT_ID", "-1003565774427")

IQ_USER = os.getenv("IQ_USER", "")
IQ_PASS = os.getenv("IQ_PASS", "")
IQ_MODE = os.getenv("IQ_MODE", "PRACTICE")

PARES = ["EURUSD", "GBPUSD", "USDJPY", "EURJPY"]

# ==================== TELEGRAM ====================
def enviar_mensagem(texto):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": texto,
        "parse_mode": "Markdown"
    }
    try:
        res = requests.post(url, data=payload, timeout=10)
        return res.json()
    except Exception as e:
        print(f"[ERRO TELEGRAM] {e}")
        return None

# ==================== IQ OPTION ====================
def conectar_iq_option():
    print(f"Conectando à IQ Option (Modo: {IQ_MODE})...")
    API = IQOptionAPI("iqoption.com", IQ_USER, IQ_PASS)
    check, reason = API.connect()
    
    if check:
        print("✅ Conectado com sucesso à IQ Option!")
        API.change_balance(IQ_MODE)
        return API
    else:
        print(f"❌ Falha na conexão com a IQ Option: {reason}")
        return None

# ==================== ESTRATÉGIA ====================
def analisar_estrategia(API, par):
    try:
        candles_m5 = API.get_candles(par, 300, 6, time.time())
        if not candles_m5 or len(candles_m5) < 5:
            return None
        
        topo_m5 = max(c['max'] for c in candles_m5[:-1])
        fundo_m5 = min(c['min'] for c in candles_m5[:-1])
        c_m5_atual = candles_m5[-1]
        
        candles_m1 = API.get_candles(par, 60, 5, time.time())
        if not candles_m1 or len(candles_m1) < 3:
            return None
            
        m1_atual = candles_m1[-1]
        m1_anterior = candles_m1[-2]
        
        # CALL
        sweep_fundo = (c_m5_atual['min'] < fundo_m5) and (c_m5_atual['close'] > fundo_m5)
        exaustao_venda_m1 = (m1_atual['volume'] < m1_anterior['volume']) and (m1_atual['close'] < m1_atual['open'])
        rejeicao_m1_call = (m1_atual['close'] > m1_atual['open']) or ((m1_atual['max'] - m1_atual['close']) < (m1_atual['close'] - m1_atual['min']))
        
        if sweep_fundo and exaustao_venda_m1 and rejeicao_m1_call:
            return "CALL"
            
        # PUT
        sweep_topo = (c_m5_atual['max'] > topo_m5) and (c_m5_atual['close'] < topo_m5)
        exaustao_compra_m1 = (m1_atual['volume'] < m1_anterior['volume']) and (m1_atual['close'] > m1_atual['open'])
        rejeicao_m1_put = (m1_atual['close'] < m1_atual['open']) or ((m1_atual['close'] - m1_atual['min']) < (m1_atual['max'] - m1_atual['close']))
        
        if sweep_topo and exaustao_compra_m1 and rejeicao_m1_put:
            return "PUT"
            
    except Exception as e:
        print(f"[ERRO ANALISE] {par}: {e}")
        
    return None

def executar_sinal(API, par, sinal):
    try:
        candles_atuais = API.get_candles(par, 60, 1, time.time())
        preco_entrada = candles_atuais[-1]['close']
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
        
        time.sleep(300)
        
        c_fechamento = API.get_candles(par, 60, 1, time.time())
        preco_fechamento = c_fechamento[-1]['close']
        
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

# ==================== LOOP DE EXECUÇÃO ====================
def loop_principal():
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

# ==================== INICIALIZAÇÃO ====================
if __name__ == "__main__":
    t = threading.Thread(target=loop_principal)
    t.daemon = True
    t.start()
    
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
