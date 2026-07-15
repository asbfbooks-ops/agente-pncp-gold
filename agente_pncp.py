#!/usr/bin/env python3
"""
agente_pncp.py — Agente local do Gold Enterprise Engine

O que este script faz de verdade (nada simulado):
  1. Expõe GET /editais  -> consulta AO VIVO a API pública do PNCP
     (https://pncp.gov.br/api/consulta/v1/contratacoes/publicacao) e filtra
     os resultados pela palavra-chave informada.
  2. Expõe POST /ai      -> repassa a conversa para a API real da Anthropic
     (https://api.anthropic.com/v1/messages) usando a chave informada pelo
     usuário na tela Configurações do sistema.
  3. Resolve o problema de CORS que o navegador aplica quando o HTML tenta
     chamar essas APIs diretamente (a API do PNCP não libera CORS para
     chamadas feitas a partir de um arquivo local file://).

Como usar:
  1) pip install -r requirements.txt
  2) python agente_pncp.py
  3) Deixe este terminal aberto. No sistema (index.html), os módulos
     "Monitor de Editais PNCP", "Captador Inteligente" e "Gold AI CEO"
     passam a funcionar com dados reais automaticamente.

Este é um servidor local simples (Flask), pensado para rodar na máquina do
usuário durante o expediente — não é um serviço de produção multiusuário.
"""

import os
import re
import imaplib
import email as email_lib
from email.header import decode_header
import unicodedata
from datetime import datetime, timedelta

import requests
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
# CORS liberado para qualquer origem — necessário porque o index.html roda
# como arquivo local (Origin: null) ou via file://, e o navegador bloqueia
# fetch() para portas diferentes por padrão.
CORS(app, resources={r"/*": {"origins": "*"}})


@app.after_request
def liberar_acesso_rede_local(resp):
    """Navegadores recentes (Chrome/Edge 2025+) bloqueiam silenciosamente
    uma página aberta como arquivo local (file://) de acessar localhost,
    a menos que o servidor libere explicitamente via este cabeçalho
    (Private Network Access). Sem isso, o agente fica rodando normalmente
    no terminal mas o navegador recusa a conexão sem mostrar erro claro —
    é a causa mais comum de 'Agente Python offline' com o servidor de pé."""
    resp.headers["Access-Control-Allow-Private-Network"] = "true"
    resp.headers["Access-Control-Allow-Origin"] = resp.headers.get("Access-Control-Allow-Origin", "*")
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


@app.route("/editais", methods=["OPTIONS"])
@app.route("/ai", methods=["OPTIONS"])
@app.route("/emails-licitacoes", methods=["OPTIONS"])
def preflight_ok():
    """Responde ao preflight OPTIONS que o navegador manda antes do POST/GET
    real, já com a liberação de rede local incluída pelo after_request acima."""
    return ("", 204)

PNCP_BASE_URL = "https://pncp.gov.br/api/consulta/v1/contratacoes/publicacao"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
# Modelo padrão para o Gold AI CEO. Pode ser sobrescrito pela variável de
# ambiente GOLD_MODEL caso a Anthropic lance um modelo mais novo.
ANTHROPIC_MODEL = os.environ.get("GOLD_MODEL", "claude-sonnet-5")

# Gemini (Google AI Studio) — usado como provedor alternativo, selecionável
# pelo usuário nas telas que suportam múltiplos provedores de IA.
GEMINI_MODEL = os.environ.get("GOLD_GEMINI_MODEL", "gemini-3.5-flash")
GEMINI_URL_TEMPLATE = "https://generativelanguage.googleapis.com/v1beta/models/{modelo}:generateContent?key={chave}"

# Modalidades de contratação mais relevantes para o negócio (revistas,
# periódicos, publicações técnicas) — códigos oficiais da tabela de domínio
# do PNCP. Usado quando o usuário não escolhe uma modalidade específica.
MODALIDADES_PADRAO = [6, 8, 9, 4]  # Pregão Eletrônico, Dispensa, Inexigibilidade, Concorrência


def normalizar(texto: str) -> str:
    """Remove acentos e baixa a caixa, para permitir busca por palavra-chave
    tolerante a acentuação (ex.: 'periodicos' encontra 'periódicos')."""
    if not texto:
        return ""
    nfkd = unicodedata.normalize("NFKD", texto)
    sem_acento = "".join(c for c in nfkd if not unicodedata.combining(c))
    return sem_acento.lower()


def calcular_score(objeto: str, termos: list) -> int:
    """Score de relevância real (não decorativo): conta quantos termos de
    busca aparecem no objeto da contratação e dá peso extra se aparecem
    logo no início do texto."""
    objeto_norm = normalizar(objeto)
    score = 40
    for termo in termos:
        t = normalizar(termo)
        if not t:
            continue
        if t in objeto_norm:
            score += 15
            if objeto_norm.find(t) < 40:
                score += 10
    return min(score, 99)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "servico": "agente_pncp", "hora": datetime.now().isoformat()})


@app.route("/editais", methods=["GET"])
def buscar_editais():
    termo = request.args.get("termo", "revistas")
    termos = [t.strip() for t in re.split(r"[+\s,]+", termo) if t.strip()]
    ano = request.args.get("ano", str(datetime.now().year))
    modalidade_param = request.args.get("modalidade", "")
    tamanho_desejado = int(request.args.get("tamanho", 20))

    if modalidade_param:
        try:
            modalidades = [int(m) for m in modalidade_param.split(",") if m.strip()]
        except ValueError:
            modalidades = MODALIDADES_PADRAO
        if not modalidades:
            modalidades = MODALIDADES_PADRAO
    else:
        modalidades = MODALIDADES_PADRAO

    # Janela de datas: últimos 90 dias dentro do ano solicitado (consulta
    # recente é o que interessa para captação de oportunidades; consultar
    # o ano inteiro de uma vez é lento e a API pagina os resultados).
    hoje = datetime.now()
    data_final = hoje if str(hoje.year) == ano else datetime(int(ano), 12, 31)
    data_inicial = data_final - timedelta(days=90)
    data_inicial_str = data_inicial.strftime("%Y%m%d")
    data_final_str = data_final.strftime("%Y%m%d")

    encontrados = []
    erros = []

    for modalidade in modalidades:
        if len(encontrados) >= tamanho_desejado:
            break
        for pagina in range(1, 4):  # até 3 páginas por modalidade
            if len(encontrados) >= tamanho_desejado:
                break
            try:
                resp = requests.get(
                    PNCP_BASE_URL,
                    params={
                        "dataInicial": data_inicial_str,
                        "dataFinal": data_final_str,
                        "codigoModalidadeContratacao": modalidade,
                        "pagina": pagina,
                        "tamanhoPagina": 50,
                    },
                    headers={"Accept": "application/json"},
                    timeout=15,
                )
                if resp.status_code != 200:
                    erros.append(f"modalidade {modalidade} pág {pagina}: HTTP {resp.status_code}")
                    break
                corpo = resp.json()
                itens = corpo.get("data", [])
                if not itens:
                    break  # sem mais páginas para essa modalidade

                for item in itens:
                    objeto = item.get("objetoCompra", "") or ""
                    objeto_norm = normalizar(objeto)
                    if not any(normalizar(t) in objeto_norm for t in termos):
                        continue
                    encontrados.append({
                        "numeroCompra": item.get("numeroCompra"),
                        "objetoCompra": objeto,
                        "orgaoEntidade": item.get("orgaoEntidade"),
                        "unidadeOrgao": item.get("unidadeOrgao"),
                        "modalidadeNome": item.get("modalidadeNome"),
                        "valorTotalEstimado": item.get("valorTotalEstimado"),
                        "dataPublicacaoPncp": item.get("dataPublicacaoPncp"),
                        "situacaoCompra": item.get("situacaoCompraNome") or item.get("situacaoCompra"),
                        "numeroControlePNCP": item.get("numeroControlePNCP"),
                        "scoreRelevancia": calcular_score(objeto, termos),
                    })
                    if len(encontrados) >= tamanho_desejado:
                        break

                # Se a página veio incompleta, não há próxima página.
                if len(itens) < 50:
                    break
            except requests.RequestException as e:
                erros.append(f"modalidade {modalidade} pág {pagina}: {e}")
                break

    encontrados.sort(key=lambda x: x["scoreRelevancia"], reverse=True)

    return jsonify({
        "sucesso": True,
        "data": encontrados,
        "totalEncontrado": len(encontrados),
        "periodoConsultado": f"{data_inicial_str} a {data_final_str}",
        "modalidadesConsultadas": modalidades,
        "avisos": erros if erros else None,
    })


@app.route("/ai", methods=["POST"])
def gold_ai_ceo():
    body = request.get_json(force=True, silent=True) or {}
    provider = (body.get("provider") or "anthropic").lower()
    system_prompt = body.get("system", "")
    mensagens = body.get("messages", [])

    if provider == "gemini":
        return _chamar_gemini(body, system_prompt, mensagens)
    return _chamar_anthropic(body, system_prompt, mensagens)


def _chamar_anthropic(body, system_prompt, mensagens):
    api_key = body.get("api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return jsonify({"sucesso": False, "erro": "API Key da Anthropic não configurada."}), 400

    payload = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 1500,
        "system": system_prompt,
        "messages": [
            {"role": m.get("role", "user"), "content": m.get("content", "")}
            for m in mensagens
            if m.get("content")
        ],
    }

    try:
        resp = requests.post(
            ANTHROPIC_URL,
            headers={
                "x-api-key": api_key,
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            json=payload,
            timeout=40,
        )
    except requests.RequestException as e:
        return jsonify({"sucesso": False, "erro": f"Falha de conexão com a Anthropic: {e}"}), 502

    if resp.status_code != 200:
        detalhe = resp.text[:300]
        return jsonify({"sucesso": False, "erro": f"Anthropic retornou HTTP {resp.status_code}: {detalhe}"}), 502

    dados = resp.json()
    texto = "".join(
        bloco.get("text", "") for bloco in dados.get("content", []) if bloco.get("type") == "text"
    )
    return jsonify({"sucesso": True, "resposta": texto or "(resposta vazia)", "provedor": "anthropic"})


def _chamar_gemini(body, system_prompt, mensagens):
    api_key = body.get("api_key") or os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return jsonify({"sucesso": False, "erro": "API Key do Gemini (Google AI Studio) não configurada."}), 400

    # Gemini usa role 'model' onde a Anthropic usa 'assistant', e o histórico
    # vai em "contents" (não em "messages"). O prompt de sistema vai à parte,
    # em "systemInstruction".
    contents = []
    for m in mensagens:
        if not m.get("content"):
            continue
        role = "model" if m.get("role") == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": m.get("content", "")}]})

    payload = {
        "contents": contents,
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "generationConfig": {"maxOutputTokens": 1500},
    }

    url = GEMINI_URL_TEMPLATE.format(modelo=GEMINI_MODEL, chave=api_key)

    try:
        resp = requests.post(url, json=payload, timeout=40)
    except requests.RequestException as e:
        return jsonify({"sucesso": False, "erro": f"Falha de conexão com o Gemini: {e}"}), 502

    if resp.status_code != 200:
        detalhe = resp.text[:300]
        return jsonify({"sucesso": False, "erro": f"Gemini retornou HTTP {resp.status_code}: {detalhe}"}), 502

    dados = resp.json()
    try:
        partes = dados["candidates"][0]["content"]["parts"]
        texto = "".join(p.get("text", "") for p in partes)
    except (KeyError, IndexError):
        texto = ""
        bloqueio = dados.get("promptFeedback", {}).get("blockReason")
        if bloqueio:
            return jsonify({"sucesso": False, "erro": f"Gemini bloqueou a resposta: {bloqueio}"}), 502

    return jsonify({"sucesso": True, "resposta": texto or "(resposta vazia)", "provedor": "gemini"})


def _decodificar_cabecalho(valor):
    """Cabeçalhos de e-mail podem vir codificados (=?UTF-8?B?...?=);
    isso decodifica pra texto legível."""
    if not valor:
        return ""
    partes = decode_header(valor)
    resultado = ""
    for texto, codificacao in partes:
        if isinstance(texto, bytes):
            resultado += texto.decode(codificacao or "utf-8", errors="replace")
        else:
            resultado += texto
    return resultado


def _extrair_corpo_completo(msg):
    """Pega o corpo em texto puro do e-mail, sem truncar (precisamos do
    texto inteiro para separar as várias licitações que vêm num único
    e-mail de aviso do Comprasnet/Compras.gov.br)."""
    try:
        if msg.is_multipart():
            for parte in msg.walk():
                if parte.get_content_type() == "text/plain" and not parte.get("Content-Disposition"):
                    corpo = parte.get_payload(decode=True)
                    charset = parte.get_content_charset() or "utf-8"
                    return corpo.decode(charset, errors="replace")
            return ""
        else:
            corpo = msg.get_payload(decode=True)
            if corpo:
                charset = msg.get_content_charset() or "utf-8"
                return corpo.decode(charset, errors="replace")
    except Exception:
        pass
    return ""


def _parse_licitacoes_comprasnet(corpo_texto):
    """Separa o e-mail de aviso do Comprasnet/Compras.gov.br em licitações
    individuais. O formato real observado é: vários blocos começando em
    'ORGÃO:' e separados por linhas de asteriscos, cada um com modalidade,
    número, objeto, datas de entrega/abertura e materiais."""
    blocos = re.split(r"\*{10,}", corpo_texto)
    resultado = []
    for bloco in blocos:
        bloco = bloco.strip()
        if not bloco or "ORG" not in bloco.upper():
            continue

        m_orgao = re.search(
            r"ORG[ÃA]O:\s*(.+?)(?:\n\s*\n|\nPreg[ãa]o|\nConcorr[êe]ncia|\nTomada|\nConvite|\nLeil[ãa]o|\nRDC)",
            bloco, re.IGNORECASE | re.DOTALL
        )
        orgao = re.sub(r"\s+", " ", m_orgao.group(1)).strip() if m_orgao else ""

        m_modalidade = re.search(
            r"(Preg[ãa]o Eletr[ôo]nico|Preg[ãa]o Presencial|Concorr[êe]ncia|Tomada de Pre[çc]os|Convite|Leil[ãa]o|RDC)\s*N[ºo°]\s*([\d/]+)",
            bloco, re.IGNORECASE
        )
        modalidade = m_modalidade.group(1) if m_modalidade else ""
        numero = m_modalidade.group(2) if m_modalidade else ""

        m_objeto = re.search(r"OBJETO:\s*(.+?)(?=\nENTREGA DA PROPOSTA|\nMATERIAIS|\Z)", bloco, re.IGNORECASE | re.DOTALL)
        objeto = re.sub(r"\s+", " ", m_objeto.group(1)).strip() if m_objeto else ""

        m_entrega = re.search(r"ENTREGA DA PROPOSTA:\s*(.+)", bloco, re.IGNORECASE)
        entrega = m_entrega.group(1).strip() if m_entrega else ""

        m_abertura = re.search(r"ABERTURA DA PROPOSTA:\s*(.+)", bloco, re.IGNORECASE)
        abertura = m_abertura.group(1).strip() if m_abertura else ""

        m_materiais = re.search(r"MATERIAIS:\s*-*\s*(.+?)\Z", bloco, re.IGNORECASE | re.DOTALL)
        materiais = re.sub(r"\s+", " ", m_materiais.group(1)).strip() if m_materiais else ""

        if not (orgao or objeto):
            continue  # bloco não reconhecido — ignora em vez de mostrar lixo

        resultado.append({
            "orgao": orgao, "modalidade": modalidade, "numero": numero,
            "objeto": objeto, "entrega": entrega, "abertura": abertura, "materiais": materiais,
        })
    return resultado


# Termos padrão para identificar e-mails de aviso de licitação na caixa de
# entrada (independente das palavras-chave de negócio do usuário).
TERMOS_ASSUNTO_PADRAO = ["licita", "edital", "pregão", "pregao", "comprasnet", "compras.gov"]


@app.route("/emails-licitacoes", methods=["POST"])
def buscar_emails_licitacoes():
    body = request.get_json(force=True, silent=True) or {}
    email_conta = body.get("email", "").strip()
    senha_app = body.get("senha_app", "").strip()
    termos_relevancia = [t.strip() for t in (body.get("termos") or []) if t.strip()] or ["livro", "revista", "periódico", "publicação", "biblioteca", "acervo"]
    dias = int(body.get("dias", 14))
    limite = int(body.get("limite", 25))

    if not email_conta or not senha_app:
        return jsonify({"sucesso": False, "erro": "Informe o e-mail e a senha de app do Gmail em Configurações."}), 400

    try:
        imap = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        imap.login(email_conta, senha_app)
    except imaplib.IMAP4.error as e:
        try:
            imap.logout()
        except Exception:
            pass
        return jsonify({"sucesso": False, "erro": f"Falha ao entrar no Gmail: {e}. Confira o e-mail e a senha de app em Configurações."}), 401
    except Exception as e:
        return jsonify({"sucesso": False, "erro": f"Falha de conexão com o Gmail: {e}"}), 502

    encontrados = []
    total_emails_digest = 0
    try:
        imap.select("INBOX", readonly=True)  # readonly=True: nunca marca como lido, nunca apaga

        data_limite = (datetime.now() - timedelta(days=dias)).strftime("%d-%b-%Y")
        status, dados = imap.search(None, f'(SINCE "{data_limite}")')
        if status != "OK":
            raise RuntimeError("Busca IMAP falhou")

        ids = dados[0].split()
        ids = ids[::-1]  # mais recentes primeiro

        for msg_id in ids:
            if len(encontrados) >= limite:
                break
            # BODY.PEEK[] busca o conteúdo SEM marcar a mensagem como lida
            status, msg_dados = imap.fetch(msg_id, "(BODY.PEEK[])")
            if status != "OK" or not msg_dados or not msg_dados[0]:
                continue
            bruto = msg_dados[0][1]
            msg = email_lib.message_from_bytes(bruto)

            assunto = _decodificar_cabecalho(msg.get("Subject", ""))
            remetente = _decodificar_cabecalho(msg.get("From", ""))
            data_email = msg.get("Date", "")

            assunto_norm = normalizar(assunto)
            if not any(normalizar(t) in assunto_norm for t in TERMOS_ASSUNTO_PADRAO):
                continue  # não é um e-mail de aviso de licitação

            total_emails_digest += 1
            corpo = _extrair_corpo_completo(msg)
            licitacoes = _parse_licitacoes_comprasnet(corpo)

            # Filtra só as licitações relevantes pro negócio (objeto ou materiais
            # batendo com as palavras-chave configuradas no Captador)
            relevantes = [
                lic for lic in licitacoes
                if any(normalizar(t) in normalizar(lic["objeto"] + " " + lic["materiais"]) for t in termos_relevancia)
            ]

            if not relevantes:
                continue  # e-mail chegou, mas nenhuma licitação de dentro dele interessa

            encontrados.append({
                "assunto": assunto,
                "remetente": remetente,
                "data": data_email,
                "totalNoEmail": len(licitacoes),
                "licitacoes": relevantes,
            })
    except Exception as e:
        imap.logout()
        return jsonify({"sucesso": False, "erro": f"Erro ao ler e-mails: {e}"}), 502

    imap.logout()
    return jsonify({
        "sucesso": True, "data": encontrados, "total": len(encontrados),
        "emailsDigestVarridos": total_emails_digest,
    })


if __name__ == "__main__":
    print("=" * 60)
    print(" Gold Enterprise Engine — Agente local")
    print(" Rodando em http://localhost:5000")
    print(" Endpoints: GET /editais · POST /ai · GET /health")
    print(" Mantenha este terminal aberto enquanto usa o sistema.")
    print(" No Windows: se aparecer um pop-up do Firewall do Windows")
    print(" pedindo permissão para o Python, clique em 'Permitir acesso'.")
    print("=" * 60)
    porta = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=porta, debug=False)