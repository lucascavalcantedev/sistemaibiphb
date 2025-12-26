import os
from flask import Flask, request, jsonify, send_file, render_template
from flask_cors import CORS
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime
import mercadopago
from fpdf import FPDF
from thefuzz import process

app = Flask(__name__)
CORS(app)

# --- CONFIGURAÇÕES DE AMBIENTE ---
# No Render, a variável DATABASE_URL conecta automaticamente ao Supabase
DATABASE_URL = os.getenv("DATABASE_URL")
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN")

# Inicializa SDK apenas se tiver token
sdk = mercadopago.SDK(MP_ACCESS_TOKEN) if MP_ACCESS_TOKEN else None

def get_db_connection():
    if not DATABASE_URL:
        raise Exception("A variável de ambiente DATABASE_URL não foi definida.")
    # sslmode='require' é obrigatório para conexão com Supabase
    conn = psycopg2.connect(DATABASE_URL, sslmode='require')
    return conn

# --- ROTA PARA O FRONTEND (ESSENCIAL PARA FULLSTACK) ---
@app.route('/')
def index():
    # O Flask busca automaticamente na pasta 'templates'
    return render_template('index.html')

# --- CRIAÇÃO AUTOMÁTICA DAS TABELAS ---
def create_tables():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Tabela Membros
        cur.execute("""
            CREATE TABLE IF NOT EXISTS members (
                id SERIAL PRIMARY KEY,
                code VARCHAR(10) UNIQUE NOT NULL,
                full_name VARCHAR(255) NOT NULL,
                birth_date DATE
            );
        """)
        
        # Tabela Transações (Entradas/PIX)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id SERIAL PRIMARY KEY,
                mp_id VARCHAR(50) UNIQUE,
                payer_name VARCHAR(255),
                member_id INTEGER REFERENCES members(id) ON DELETE SET NULL,
                amount NUMERIC(10, 2),
                transaction_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status VARCHAR(20) DEFAULT 'pendente', -- pendente, confirmado
                origin VARCHAR(20), -- pix, manual
                type VARCHAR(20) -- dizimo, oferta
            );
        """)
        
        # Tabela Despesas (Saídas)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS expenses (
                id SERIAL PRIMARY KEY,
                description VARCHAR(255) NOT NULL,
                category VARCHAR(100),
                amount NUMERIC(10, 2) NOT NULL,
                expense_date DATE DEFAULT CURRENT_DATE
            );
        """)
        
        conn.commit()
        cur.close()
        conn.close()
        print("--- Banco de Dados Conectado e Tabelas Verificadas ---")
    except Exception as e:
        print(f"Erro ao criar tabelas: {e}")

# Executa criação ao iniciar
if DATABASE_URL:
    create_tables()

# --- WEBHOOK MERCADO PAGO ---
@app.route('/webhook/mercadopago', methods=['POST'])
def mp_webhook():
    if not sdk: return jsonify({"error": "SDK não configurado"}), 500
    
    data = request.json
    # O Mercado Pago pode enviar diferentes estruturas dependendo da versão da API
    # Aqui verificamos se é um pagamento ou uma notificação de criação
    if data.get("type") == "payment" or data.get("action") == "payment.created":
        # Tenta pegar o ID do local correto (data.id é o padrão v1/v2)
        payment_id = data.get("data", {}).get("id")
        
        if not payment_id:
             return jsonify({"status": "ignored", "reason": "no id found"}), 200

        try:
            # Buscar detalhes do pagamento
            payment_info = sdk.payment().get(payment_id)
            payment = payment_info["response"]
            
            if payment["status"] == "approved":
                payer = payment.get("payer", {})
                # Monta nome completo
                first = payer.get("first_name", "")
                last = payer.get("last_name", "")
                payer_name = f"{first} {last}".strip()
                
                amount = payment["transaction_amount"]
                date_created = payment["date_created"] 

                conn = get_db_connection()
                cur = conn.cursor(cursor_factory=RealDictCursor)
                
                # --- Lógica de Fuzzy Match (Vínculo Automático) ---
                cur.execute("SELECT id, full_name FROM members")
                members = cur.fetchall()
                
                best_match_id = None
                if members:
                    choices = {m['full_name']: m['id'] for m in members}
                    # Tenta achar o nome mais parecido na lista de membros
                    match = process.extractOne(payer_name, choices.keys())
                    # Se tiver mais de 85% de semelhança, vincula
                    if match and match[1] >= 85: 
                        best_match_id = choices[match[0]]

                # Insere Transação
                cur.execute("""
                    INSERT INTO transactions (mp_id, payer_name, member_id, amount, transaction_date, status, origin)
                    VALUES (%s, %s, %s, %s, %s, 'pendente', 'pix')
                    ON CONFLICT (mp_id) DO NOTHING
                """, (str(payment_id), payer_name, best_match_id, amount, date_created))
                conn.commit()
                cur.close()
                conn.close()
        except Exception as e:
            print(f"Erro no webhook: {e}")

    return jsonify({"status": "ok"}), 200

# --- ROTAS API (DADOS) ---

@app.route('/api/dashboard', methods=['GET'])
def get_dashboard():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    # Soma entradas confirmadas do mês atual
    cur.execute("""
        SELECT SUM(amount) as total FROM transactions 
        WHERE status = 'confirmado' AND EXTRACT(MONTH FROM transaction_date) = EXTRACT(MONTH FROM CURRENT_DATE)
    """)
    res_in = cur.fetchone()
    inflow = res_in['total'] if res_in and res_in['total'] else 0
    
    # Soma saídas do mês atual
    cur.execute("""
        SELECT SUM(amount) as total FROM expenses 
        WHERE EXTRACT(MONTH FROM expense_date) = EXTRACT(MONTH FROM CURRENT_DATE)
    """)
    res_out = cur.fetchone()
    outflow = res_out['total'] if res_out and res_out['total'] else 0
    
    cur.close()
    conn.close()
    return jsonify({"inflow": float(inflow), "outflow": float(outflow), "balance": float(inflow - outflow)})

@app.route('/api/members', methods=['GET', 'POST', 'DELETE'])
def manage_members():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    if request.method == 'GET':
        cur.execute("SELECT * FROM members ORDER BY full_name")
        res = cur.fetchall()
        for r in res:
            if r['birth_date']: r['birth_date'] = r['birth_date'].strftime('%Y-%m-%d')
    
    elif request.method == 'POST':
        data = request.json
        cur.execute("INSERT INTO members (code, full_name, birth_date) VALUES (%s, %s, %s)",
                    (data['code'], data['full_name'], data['birth_date']))
        conn.commit()
        res = {"msg": "Membro adicionado"}

    elif request.method == 'DELETE':
        m_id = request.args.get('id')
        cur.execute("DELETE FROM members WHERE id = %s", (m_id,))
        conn.commit()
        res = {"msg": "Membro removido"}

    cur.close()
    conn.close()
    return jsonify(res)

@app.route('/api/transactions', methods=['GET', 'POST'])
def manage_transactions():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    if request.method == 'GET':
        cur.execute("""
            SELECT t.*, m.code as member_code, m.full_name as member_real_name 
            FROM transactions t 
            LEFT JOIN members m ON t.member_id = m.id
            ORDER BY t.transaction_date DESC
        """)
        res = cur.fetchall()
        for r in res:
            if r['transaction_date']: r['transaction_date'] = r['transaction_date'].isoformat()
            if r['amount']: r['amount'] = float(r['amount'])
        
    elif request.method == 'POST':
        data = request.json
        action = data.get('action')
        
        if action == 'confirm':
            cur.execute("UPDATE transactions SET type = %s, status = 'confirmado' WHERE id = %s",
                        (data['type'], data['id']))
        elif action == 'manual_add':
            cur.execute("""
                INSERT INTO transactions (payer_name, amount, type, status, origin, transaction_date)
                VALUES (%s, %s, %s, 'confirmado', 'manual', NOW())
            """, (data['name'], data['amount'], data['type']))
            
        conn.commit()
        res = {"msg": "Sucesso"}

    cur.close()
    conn.close()
    return jsonify(res)

@app.route('/api/expenses', methods=['GET', 'POST', 'DELETE'])
def manage_expenses():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    if request.method == 'GET':
        cur.execute("SELECT * FROM expenses ORDER BY expense_date DESC")
        res = cur.fetchall()
        for r in res:
            if r['expense_date']: r['expense_date'] = r['expense_date'].strftime('%Y-%m-%d')
            if r['amount']: r['amount'] = float(r['amount'])
        
    elif request.method == 'POST':
        data = request.json
        cur.execute("INSERT INTO expenses (description, category, amount, expense_date) VALUES (%s, %s, %s, %s)",
                    (data['description'], data['category'], data['amount'], data['date']))
        conn.commit()
        res = {"msg": "Despesa salva"}

    elif request.method == 'DELETE':
        e_id = request.args.get('id')
        cur.execute("DELETE FROM expenses WHERE id = %s", (e_id,))
        conn.commit()
        res = {"msg": "Despesa removida"}

    cur.close()
    conn.close()
    return jsonify(res)

@app.route('/api/report/final', methods=['POST'])
def generate_report():
    data = request.json
    month = int(data['month'])
    year = int(data['year'])
    prev_balance = float(data['prev_balance'])
    
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    # Entradas Confirmadas
    cur.execute("""
        SELECT t.amount, t.transaction_date, t.type, m.code 
        FROM transactions t
        LEFT JOIN members m ON t.member_id = m.id
        WHERE status = 'confirmado' 
        AND EXTRACT(MONTH FROM transaction_date) = %s 
        AND EXTRACT(YEAR FROM transaction_date) = %s
    """, (month, year))
    inflows = cur.fetchall()
    
    # Saídas
    cur.execute("""
        SELECT * FROM expenses 
        WHERE EXTRACT(MONTH FROM expense_date) = %s 
        AND EXTRACT(YEAR FROM expense_date) = %s
    """, (month, year))
    outflows = cur.fetchall()
    
    cur.close()
    conn.close()
    
    total_in = sum([float(i['amount']) for i in inflows])
    total_out = sum([float(o['amount']) for o in outflows])
    final_balance = (prev_balance + total_in) - total_out
    
    # Geração do PDF
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", 'B', 16)
    pdf.cell(0, 10, txt=f"Relatorio Financeiro - {month}/{year}", ln=True, align='C')
    
    pdf.set_font("Arial", size=12)
    pdf.ln(10)
    
    pdf.set_fill_color(230, 240, 255)
    pdf.cell(0, 10, f"Saldo Mes Anterior: R$ {prev_balance:.2f}", ln=True, fill=True)
    pdf.cell(0, 10, f"(+) Total Entradas: R$ {total_in:.2f}", ln=True, fill=True)
    pdf.cell(0, 10, f"(-) Total Saidas: R$ {total_out:.2f}", ln=True, fill=True)
    pdf.set_font("Arial", 'B', 12)
    pdf.cell(0, 10, f"(=) Saldo a Transportar: R$ {final_balance:.2f}", ln=True, border=1)
    
    pdf.ln(10)
    pdf.set_font("Arial", 'B', 14)
    pdf.cell(0, 10, "Detalhe - Entradas", ln=True)
    pdf.set_font("Arial", size=10)
    
    pdf.cell(30, 7, "Data", 1)
    pdf.cell(30, 7, "Codigo", 1)
    pdf.cell(30, 7, "Tipo", 1)
    pdf.cell(40, 7, "Valor", 1)
    pdf.ln()
    
    for row in inflows:
        d = row['transaction_date'].strftime("%d/%m/%Y")
        c = row['code'] if row['code'] else "---"
        pdf.cell(30, 7, d, 1)
        pdf.cell(30, 7, c, 1)
        pdf.cell(30, 7, row['type'], 1)
        pdf.cell(40, 7, f"R$ {float(row['amount']):.2f}", 1)
        pdf.ln()

    pdf.ln(10)
    pdf.set_font("Arial", 'B', 14)
    pdf.cell(0, 10, "Detalhe - Saidas", ln=True)
    pdf.set_font("Arial", size=10)

    pdf.cell(30, 7, "Data", 1)
    pdf.cell(100, 7, "Descricao", 1)
    pdf.cell(40, 7, "Valor", 1)
    pdf.ln()
    for row in outflows:
        d = row['expense_date'].strftime("%d/%m/%Y") if row['expense_date'] else "-"
        pdf.cell(30, 7, d, 1)
        pdf.cell(100, 7, row['description'], 1)
        pdf.cell(40, 7, f"R$ {float(row['amount']):.2f}", 1)
        pdf.ln()

    # Salva no diretório temporário do sistema (obrigatório para Render/Serverless)
    file_name = f"/tmp/relatorio_{month}_{year}.pdf"
    pdf.output(file_name)
    
    return send_file(file_name, as_attachment=True)

if __name__ == '__main__':
    # Roda localmente para testes
    app.run(debug=True, port=8000)