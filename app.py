import os
import json
import re
import hashlib
import time
from flask import Flask, render_template, request, redirect, url_for, session, flash, Response, stream_with_context
from flask_sqlalchemy import SQLAlchemy
from functools import wraps
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build

load_dotenv()

app = Flask(__name__)

# --- CONFIGURAÇÕES ---
db_url = os.getenv("DATABASE_URL")
salt = os.getenv("SECURITY_SALT")
admin_pass = os.getenv("ADMIN_PASSWORD")
app.secret_key = os.getenv("SECRET_KEY")

if not db_url or not salt or not admin_pass or not app.secret_key:
    raise ValueError("❌ Erro: Verifique as variáveis de ambiente.")

app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = { "pool_pre_ping": True, "pool_recycle": 300 }

db = SQLAlchemy(app)

# ==============================================================================
# 1. MODELOS DO BANCO (Definidos no topo para o Python conhecer)
# ==============================================================================

class Certificado(db.Model):
    __tablename__ = 'certificados' 
    id = db.Column(db.Integer, primary_key=True)
    cpf_aluno = db.Column(db.String(100), index=True) 
    nome_aluno = db.Column(db.String(150))
    cod_turma = db.Column(db.String(50)) # A chave de ligação (FK simulada)
    link_drive = db.Column(db.String(500))
    ativo = db.Column(db.Boolean, default=True)

class Curso(db.Model):
    __tablename__ = 'cursos'  # <--- CONFIRA NO NEON SE O NOME É ESSE MESMO
    # Ajuste os nomes das colunas abaixo conforme seu Neon:
    id = db.Column(db.Integer, primary_key=True) 
    cod_turma = db.Column(db.String, index=True) # Ex: 'T01' (Tem que bater com cod_turma)
    nome_curso = db.Column(db.String)            # Ex: 'Excel Avançado'

# ==============================================================================
# 2. FUNÇÕES AUXILIARES (Drive e Login) - Devem vir ANTES das rotas
# ==============================================================================

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session:
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

def get_drive_service():
    creds_dict = None
    creds_json_env = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if creds_json_env:
        try: creds_dict = json.loads(creds_json_env)
        except: pass
    
    if not creds_dict and os.path.exists("credenciais_drive.json"):
        try:
            with open("credenciais_drive.json", "r") as f: creds_dict = json.load(f)
        except: pass
    
    if not creds_dict: return None

    try:
        creds = service_account.Credentials.from_service_account_info(
            creds_dict, scopes=['https://www.googleapis.com/auth/drive']
        )
        return build('drive', 'v3', credentials=creds)
    except: return None

def deletar_arquivo_drive(link):
    if not link or "drive.google.com" not in link: return False
    match = re.search(r'/d/([a-zA-Z0-9_-]+)', link)
    if not match: return False
    file_id = match.group(1)
    service = get_drive_service()
    if service:
        try:
            # Tenta deletar (Se for dono)
            service.files().delete(fileId=file_id).execute()
            return True
        except:
            # Se não for dono, tenta remover da pasta (Ejetar)
            try:
                file = service.files().get(fileId=file_id, fields='parents').execute()
                parents = file.get('parents')
                if parents:
                    service.files().update(fileId=file_id, removeParents=",".join(parents)).execute()
                    return True
                return True # Já estava órfão
            except: return False
    return False

# ==============================================================================
# 3. ROTAS DO SITE
# ==============================================================================

@app.route('/', methods=['GET', 'POST'])
def index():
    resultados = None
    erro = None
    
    # BUSCA DE CURSOS DISPONÍVEIS (JOIN para pegar o nome do curso na badge)
    cursos_disponiveis = db.session.query(Curso.nome_curso, Certificado.cod_turma)\
        .select_from(Certificado)\
        .join(Curso, Certificado.cod_turma == Curso.cod_turma)\
        .filter(Certificado.ativo == True)\
        .distinct()\
        .all()

    if request.method == 'POST':
        cpf_digitado = request.form.get('cpf')
        if cpf_digitado:
            cpf_limpo = "".join(filter(str.isdigit, cpf_digitado)).zfill(11)
            texto_para_hash = cpf_limpo + salt
            cpf_hash_busca = hashlib.sha256(texto_para_hash.encode()).hexdigest()
            
            # BUSCA DO ALUNO COM PROCV (JOIN)
            resultados = db.session.query(Certificado, Curso.nome_curso)\
                .outerjoin(Curso, Certificado.cod_turma == Curso.cod_turma)\
                .filter(Certificado.cpf_aluno == cpf_hash_busca, Certificado.ativo == True)\
                .all()
            # ...
                
            if not resultados:
                erro = f"Não encontramos certificados para o CPF final ...{cpf_limpo[-4:]}."
        else:
            erro = "CPF inválido."
            
    return render_template('index.html', resultados=resultados, erro=erro, cursos_disponiveis=cursos_disponiveis)

@app.route('/admin/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if request.form.get('senha') == admin_pass:
            session['logged_in'] = True
            return redirect(url_for('dashboard'))
        flash('Senha incorreta!')
    return render_template('login.html')

@app.route('/admin/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('index'))

@app.route('/admin/dashboard')
@login_required
def dashboard():
    termo = request.args.get('q')
    query = Certificado.query
    if termo:
        from sqlalchemy import or_
        filtros = [
            Certificado.nome_aluno.ilike(f"%{termo}%"),
            Certificado.cod_turma.ilike(f"%{termo}%")
        ]
        query = query.filter(or_(*filtros))
    
    certificados = query.order_by(Certificado.id.desc()).limit(100).all()
    
    # Resumo também precisa ser ajustado se quiser mostrar nome do curso no admin
    # Mas mantive simples aqui para não dar erro de SQL complexo agora
    resumo_turmas = db.session.execute(
        db.text("SELECT cod_turma, COUNT(*) as total, SUM(CASE WHEN ativo THEN 1 ELSE 0 END) as ativos FROM certificados GROUP BY cod_turma ORDER BY cod_turma DESC")
    ).fetchall()
    
    return render_template('dashboard.html', certificados=certificados, resumo_turmas=resumo_turmas, busca_atual=termo)

@app.route('/admin/edit/<int:id>', methods=['GET', 'POST'])
@login_required
def edit_certificado(id):
    cert = Certificado.query.get_or_404(id)
    if request.method == 'POST':
        cert.nome_aluno = request.form['nome']
        cert.cod_turma = request.form['turma']
        cert.link_drive = request.form['link']
        cert.ativo = True if request.form.get('ativo') else False
        db.session.commit()
        flash(f'Dados atualizados!')
        return redirect(url_for('dashboard'))
    return render_template('edit.html', cert=cert)

@app.route('/admin/toggle_turma/<turma>/<acao>')
@login_required
def toggle_turma(turma, acao):
    novo_status = True if acao == "ativar" else False
    Certificado.query.filter_by(cod_turma=turma).update({Certificado.ativo: novo_status})
    db.session.commit()
    return redirect(url_for('dashboard'))

@app.route('/admin/delete/<int:id>')
@login_required
def delete_certificado(id):
    cert = Certificado.query.get(id)
    if cert:
        deletar_arquivo_drive(cert.link_drive)
        db.session.delete(cert)
        db.session.commit()
        flash('Registro removido!')
    return redirect(url_for('dashboard'))

@app.route('/admin/delete_turma_inteira/<turma>')
@login_required
def delete_turma_inteira(turma):
    def gerar_log():
        yield "<html><head><meta charset='utf-8'></head><body style='background:#1e1e1e;color:#00ff00;font-family:monospace;padding:20px;'><h2>🗑️ Limpando Turma " + turma + "...</h2>"
        alunos = Certificado.query.filter_by(cod_turma=turma).all()
        count = 0
        for aluno in alunos:
            time.sleep(0.05)
            # Tenta Drive
            if deletar_arquivo_drive(aluno.link_drive):
                msg_drive = "✅ Drive"
            else:
                msg_drive = "⚠️ Drive (Não encontrado/Erro)"
            
            # Tenta Banco
            db.session.delete(aluno)
            db.session.commit()
            
            yield f"<div>[{count+1}] {aluno.nome_aluno}: {msg_drive} ... Banco OK.</div><script>window.scrollTo(0,document.body.scrollHeight);</script>"
            count += 1
            
        yield f"<br><h3>🏁 FIM! {count} alunos removidos.</h3><a href='/admin/dashboard' style='background:#fff;color:#000;padding:10px;text-decoration:none;border-radius:4px;'>⬅ Voltar ao Painel</a></body></html>"
        
    return Response(stream_with_context(gerar_log()))

if __name__ == '__main__':
    app.run(debug=True, port=5000)