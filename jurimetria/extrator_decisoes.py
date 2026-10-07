"""Extração estruturada de decisões judiciais com Gemini, para um banco de jurimetria.

Adaptado do tutorial "Document Processing with Gemini" (Google, Apache 2.0).

Fluxo:
    PDF da decisão -> classificar (sentença, acórdão...) -> extrair campos ->
    salvar JSON por arquivo (cache) -> planilha com abas Processos/Decisoes/Teses.

O livro de códigos (CATALOGO_TESES) fica no topo do arquivo: edite-o para
incluir as teses que importam no seu acervo. O modelo só pode escolher códigos
dessa lista (ou "outra"), o que mantém a classificação consistente.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from enum import Enum
from pathlib import Path

import pandas as pd
import pypdf
from google import genai
from google.genai.types import GenerateContentConfig, Part
from pydantic import BaseModel, Field

PDF_MIME_TYPE = "application/pdf"
JSON_MIME_TYPE = "application/json"
ENUM_MIME_TYPE = "text/x.enum"

# Acima disso, é melhor recortar as páginas da decisão antes de enviar
# (ver localizar_paginas_decisoes / recortar_pdf).
LIMITE_PDF_BYTES = 18 * 1024 * 1024

# ---------------------------------------------------------------------------
# Livro de códigos
# ---------------------------------------------------------------------------

CATALOGO_TESES: dict[str, str] = {
    "nulidade_citacao": "Nulidade ou ausência de citação/intimação válida.",
    "ilegitimidade": "Ilegitimidade ativa ou passiva.",
    "prescricao_decadencia": "Prescrição ou decadência da pretensão.",
    "nulidade_notificacao": "Vício na notificação premonitória, denúncia ou constituição em mora.",
    "purgacao_mora": "Pedido ou deferimento de purgação da mora (art. 62 da Lei 8.245/91).",
    "liminar_despejo": "Liminar de desocupação (art. 59, § 1º, da Lei 8.245/91), inclusive caução.",
    "excesso_cobranca": "Excesso de cobrança, encargos, multa moratória ou correção indevidos.",
    "multa_rescisoria": "Multa por devolução antecipada e sua proporcionalidade (art. 4º da Lei 8.245/91).",
    "benfeitorias": "Indenização ou retenção por benfeitorias (art. 35 da Lei 8.245/91).",
    "danos_imovel_vistoria": "Danos ao imóvel, reparos na devolução, laudo de vistoria.",
    "responsabilidade_fiador": "Exoneração do fiador, aditivo sem anuência (Súmula 214/STJ), outorga conjugal (Súmula 332/STJ).",
    "bem_de_familia": "Impenhorabilidade do bem de família, inclusive do fiador (Tema 1127/STF).",
    "revisional_aluguel": "Revisão do valor do aluguel ou aluguel provisório.",
    "renovatoria": "Requisitos e valor na ação renovatória.",
    # Incorporação e compra e venda na planta
    "atraso_entrega": "Atraso na entrega da unidade e termo inicial da mora (data prevista, habite-se, entrega das chaves).",
    "clausula_tolerancia": "Validade e aplicação da cláusula de tolerância de 180 dias.",
    "caso_fortuito_forca_maior": "Excludentes alegadas para o atraso: chuvas, falta de mão de obra ou insumos, pandemia, entraves com órgãos públicos.",
    "lucros_cessantes": "Lucros cessantes ou aluguéis pelo período de atraso, inclusive se presumidos.",
    "clausula_penal": "Cláusula penal moratória: cumulação com lucros cessantes (Tema 970/STJ) e inversão em favor do comprador (Tema 971/STJ).",
    "correcao_saldo_devedor": "Índice de correção do saldo devedor durante o atraso (INCC x IPCA) e congelamento.",
    "juros_obra": "Juros de obra ou taxa de evolução da obra cobrados durante o atraso.",
    "distrato_retencao": "Resolução ou distrato: percentual de retenção, Lei 13.786/2018, devolução imediata (Súmula 543/STJ).",
    "juros_mora_distrato": "Termo inicial dos juros de mora na restituição por distrato (Tema 1002/STJ).",
    "corretagem_sati": "Restituição de comissão de corretagem e taxa SATI (Temas 938 e 939/STJ).",
    "vicio_construtivo": "Vícios construtivos, prazo de garantia (art. 618 do CC) e prazo para reclamar.",
    "encargos_antes_chaves": "Condomínio, IPTU e outras despesas cobradas antes da entrega das chaves.",
    "grupo_economico_spe": "Responsabilidade da controladora ou do grupo pelas obrigações da SPE.",
    "condominio_cobranca": "Cobrança de cotas condominiais e responsabilidade pelas despesas.",
    "dano_moral": "Pedido de indenização por dano moral (ex.: mero inadimplemento contratual x dano indenizável).",
    "justica_gratuita": "Concessão ou impugnação da gratuidade da justiça.",
    "cerceamento_defesa": "Cerceamento de defesa, julgamento antecipado, indeferimento de prova.",
    "honorarios_sucumbencia": "Fixação, majoração ou redistribuição de honorários e sucumbência.",
    "outra": "Tese fora do catálogo: descreva em 'descricao_livre'.",
}

TeseCodigo = Enum("TeseCodigo", {k.upper(): k for k in CATALOGO_TESES})


class TipoDocumento(Enum):
    SENTENCA = "sentenca"
    ACORDAO = "acordao"
    DECISAO_MONOCRATICA = "decisao_monocratica"
    DECISAO_INTERLOCUTORIA = "decisao_interlocutoria"
    DECISAO_EMBARGOS_DECLARACAO = "decisao_embargos_declaracao"
    DESPACHO = "despacho"
    OUTRO = "outro"


# Despachos e documentos não decisórios não entram no banco.
TIPOS_DECISORIOS = {
    TipoDocumento.SENTENCA,
    TipoDocumento.ACORDAO,
    TipoDocumento.DECISAO_MONOCRATICA,
    TipoDocumento.DECISAO_INTERLOCUTORIA,
    TipoDocumento.DECISAO_EMBARGOS_DECLARACAO,
}


class Instancia(Enum):
    PRIMEIRO_GRAU = "primeiro_grau"
    SEGUNDO_GRAU = "segundo_grau"
    TRIBUNAL_SUPERIOR = "tribunal_superior"


class Polo(Enum):
    AUTOR = "autor"
    REU = "reu"
    TERCEIRO = "terceiro"
    NAO_IDENTIFICADO = "nao_identificado"


class Resultado(Enum):
    FAVORAVEL = "favoravel"
    PARCIALMENTE_FAVORAVEL = "parcialmente_favoravel"
    DESFAVORAVEL = "desfavoravel"
    EXTINTO_SEM_MERITO = "extinto_sem_merito"
    HOMOLOGACAO_ACORDO = "homologacao_acordo"
    NAO_SE_APLICA = "nao_se_aplica"


class ResultadoRecurso(Enum):
    PROVIDO = "provido"
    PARCIALMENTE_PROVIDO = "parcialmente_provido"
    DESPROVIDO = "desprovido"
    NAO_CONHECIDO = "nao_conhecido"
    NAO_SE_APLICA = "nao_se_aplica"


class QuemAlegou(Enum):
    PARTE_MONITORADA = "parte_monitorada"
    PARTE_CONTRARIA = "parte_contraria"
    DE_OFICIO = "de_oficio"
    NAO_IDENTIFICADO = "nao_identificado"


class SituacaoTese(Enum):
    ACOLHIDA = "acolhida"
    PARCIALMENTE_ACOLHIDA = "parcialmente_acolhida"
    REJEITADA = "rejeitada"
    NAO_APRECIADA = "nao_apreciada"
    PREJUDICADA = "prejudicada"


# ---------------------------------------------------------------------------
# Esquema de extração
# ---------------------------------------------------------------------------


class ParteProcesso(BaseModel):
    nome: str = Field(..., description="Nome da parte como consta na decisão")
    polo: Polo
    advogados: list[str] = Field(
        default_factory=list, description="Advogados citados, com OAB se constar"
    )


class Tese(BaseModel):
    codigo: TeseCodigo = Field(..., description="Código do livro de códigos")
    descricao_livre: str | None = Field(
        None, description="Obrigatória se codigo = outra; senão, detalhe opcional"
    )
    quem_alegou: QuemAlegou
    situacao: SituacaoTese
    fundamento: str | None = Field(
        None, description="Dispositivo legal, súmula ou precedente usado pelo julgador"
    )
    trecho: str | None = Field(
        None,
        description="Trecho literal curto (até 300 caracteres) da decisão que comprova a situação da tese",
    )


class DecisaoExtraida(BaseModel):
    numero_processo: str | None = Field(
        None, description="Número CNJ no formato NNNNNNN-DD.AAAA.J.TR.OOOO"
    )
    tribunal: str | None = Field(None, description="Ex.: TJRS, STJ")
    comarca: str | None = None
    orgao_julgador: str | None = Field(None, description="Vara, câmara ou turma")
    julgador: str | None = Field(None, description="Juiz prolator ou relator")
    classe_processual: str | None = Field(None, description="Ex.: Despejo, Apelação Cível")
    assunto: str | None = None
    data_decisao: str | None = Field(None, description="Data da decisão, AAAA-MM-DD")
    tipo_documento: TipoDocumento
    instancia: Instancia
    partes: list[ParteProcesso]
    polo_parte_monitorada: Polo
    resultado_para_parte_monitorada: Resultado
    resultado_recurso: ResultadoRecurso = Field(
        ..., description="Só para acórdãos e decisões monocráticas em recurso"
    )
    recorrente: str | None = Field(None, description="Quem recorreu, se for recurso")
    valor_causa: float | None = Field(None, description="Em reais, número puro")
    valor_condenacao: float | None = Field(
        None, description="Valor líquido da condenação em reais, se fixado"
    )
    honorarios_percentual: float | None = Field(
        None, description="Percentual de honorários fixado, ex.: 10 para 10%"
    )
    teses: list[Tese]
    dispositivo: str = Field(..., description="Síntese fiel do dispositivo, até 3 frases")
    resumo: str = Field(..., description="Resumo do caso em até 3 frases")
    incertezas: str | None = Field(
        None,
        description="Pontos ilegíveis, ambíguos ou que exigem conferência humana",
    )


# ---------------------------------------------------------------------------
# Instruções ao modelo
# ---------------------------------------------------------------------------

INSTRUCAO_CLASSIFICACAO = """Você é especialista em documentos processuais brasileiros.
Classifique o documento em uma das categorias do esquema.
- sentenca: resolve a fase de conhecimento ou extingue a execução em 1º grau.
- acordao: julgamento colegiado.
- decisao_monocratica: decisão do relator que julga o recurso sozinho.
- decisao_interlocutoria: decide questão incidental (ex.: tutela de urgência, liminar).
- decisao_embargos_declaracao: julga embargos de declaração.
- despacho: mero impulso, sem conteúdo decisório.
- outro: petições, certidões e demais documentos."""


def instrucao_extracao(parte_monitorada: str, apelidos: list[str]) -> str:
    teses = "\n".join(f"- {k}: {v}" for k, v in CATALOGO_TESES.items())
    nomes = ", ".join([parte_monitorada, *apelidos])
    return f"""Você é analista de jurimetria e extrai dados de decisões judiciais brasileiras.

PARTE MONITORADA: {parte_monitorada}
Também pode aparecer como: {nomes}.
Os campos "polo_parte_monitorada", "resultado_para_parte_monitorada" e
"quem_alegou" são sempre do ponto de vista da parte monitorada.

Regras:
1. Use apenas o que está no documento. Nunca invente. Sem informação -> null.
2. Datas em AAAA-MM-DD. Valores em reais como número (1.234,56 -> 1234.56).
3. Resultado: "parcialmente_favoravel" quando a parte monitorada obtém algo,
   mas não tudo o que pediu ou evitou. Em acórdão, o resultado é o efeito final
   para a parte monitorada, não apenas se o recurso foi provido.
4. Registre cada tese enfrentada, usando somente estes códigos:
{teses}
5. Em "trecho", copie literalmente a passagem que comprova a situação da tese.
6. Qualquer dúvida, texto ilegível ou ambiguidade vai em "incertezas".
7. Se a parte monitorada não aparecer no documento, diga isso em "incertezas"
   e use polo "nao_identificado" e resultado "nao_se_aplica"."""


PROMPT_PAGINAS = """Este documento são os autos (ou parte dos autos) de um processo judicial.
Retorne os números das páginas que contêm SENTENÇAS, ACÓRDÃOS, DECISÕES MONOCRÁTICAS
ou DECISÕES INTERLOCUTÓRIAS relevantes (tutela, liminar), incluindo ementa e voto.
Ignore petições, procurações, certidões, comprovantes e despachos de mero expediente.
Use a numeração física das páginas do PDF (a primeira página é 1). Na dúvida, inclua a página."""


# ---------------------------------------------------------------------------
# Cliente e chamadas ao modelo
# ---------------------------------------------------------------------------


def criar_cliente(
    project_id: str | None = None,
    location: str = "global",
    api_key: str | None = None,
) -> genai.Client:
    """Com api_key usa a Gemini API (AI Studio); sem ela, a Agent Platform/Vertex."""
    if api_key:
        return genai.Client(api_key=api_key)
    try:
        return genai.Client(enterprise=True, project=project_id, location=location)
    except TypeError:
        # Versões antigas do SDK usam o parâmetro vertexai.
        return genai.Client(vertexai=True, project=project_id, location=location)


def _com_retentativas(funcao, tentativas: int = 4):
    for i in range(tentativas):
        try:
            return funcao()
        except Exception:
            if i == tentativas - 1:
                raise
            time.sleep(2 ** (i + 1))


def _parte_pdf(pdf_bytes: bytes) -> Part:
    if len(pdf_bytes) > LIMITE_PDF_BYTES:
        raise ValueError(
            "PDF grande demais para envio direto. Use localizar_paginas_decisoes() "
            "e recortar_pdf() para enviar só as páginas da decisão."
        )
    return Part.from_bytes(data=pdf_bytes, mime_type=PDF_MIME_TYPE)


def classificar_documento(client, model_id: str, pdf_bytes: bytes) -> TipoDocumento:
    resposta = _com_retentativas(
        lambda: client.models.generate_content(
            model=model_id,
            contents=["Classifique o documento a seguir.", _parte_pdf(pdf_bytes)],
            config=GenerateContentConfig(
                system_instruction=INSTRUCAO_CLASSIFICACAO,
                response_schema=TipoDocumento,
                response_mime_type=ENUM_MIME_TYPE,
                temperature=0,
            ),
        )
    )
    return resposta.parsed


def extrair_decisao(
    client,
    model_id: str,
    pdf_bytes: bytes,
    parte_monitorada: str,
    apelidos: list[str] | None = None,
) -> DecisaoExtraida:
    resposta = _com_retentativas(
        lambda: client.models.generate_content(
            model=model_id,
            contents=["Extraia os dados da decisão judicial a seguir.", _parte_pdf(pdf_bytes)],
            config=GenerateContentConfig(
                system_instruction=instrucao_extracao(parte_monitorada, apelidos or []),
                response_schema=DecisaoExtraida,
                response_mime_type=JSON_MIME_TYPE,
                temperature=0,
            ),
        )
    )
    if resposta.parsed is None:
        return DecisaoExtraida.model_validate_json(resposta.text)
    return resposta.parsed


def localizar_paginas_decisoes(client, model_id: str, pdf_bytes: bytes) -> list[int]:
    """Para autos completos: devolve as páginas que contêm decisões."""
    resposta = _com_retentativas(
        lambda: client.models.generate_content(
            model=model_id,
            contents=["<Documento>", _parte_pdf(pdf_bytes), "</Documento>", PROMPT_PAGINAS],
            config=GenerateContentConfig(
                response_schema=list[int],
                response_mime_type=JSON_MIME_TYPE,
                temperature=0,
            ),
        )
    )
    return sorted(set(resposta.parsed or []))


def recortar_pdf(arquivo_entrada: str | Path, arquivo_saida: str | Path, paginas: list[int]) -> None:
    """Gera um PDF só com as páginas indicadas (numeração a partir de 1)."""
    leitor = pypdf.PdfReader(str(arquivo_entrada))
    escritor = pypdf.PdfWriter()
    for numero in paginas:
        if 1 <= numero <= len(leitor.pages):
            escritor.add_page(leitor.pages[numero - 1])
    escritor.write(str(arquivo_saida))


# ---------------------------------------------------------------------------
# Processamento em lote, com cache por arquivo
# ---------------------------------------------------------------------------


def processar_pasta(
    client,
    model_id: str,
    pasta: str | Path,
    parte_monitorada: str,
    apelidos: list[str] | None = None,
    reprocessar: bool = False,
) -> list[dict]:
    """Processa todos os PDFs da pasta. Cada resultado é salvo em
    <pasta>/_extracoes/<arquivo>.json; numa nova execução, arquivos já
    processados são lidos do cache (sem custo), salvo se reprocessar=True."""
    pasta = Path(pasta)
    pasta_cache = pasta / "_extracoes"
    pasta_cache.mkdir(exist_ok=True)
    resultados = []

    pdfs = sorted(pasta.glob("*.pdf")) + sorted(pasta.glob("*.PDF"))
    for i, caminho in enumerate(pdfs, 1):
        cache = pasta_cache / f"{caminho.stem}.json"
        if cache.exists() and not reprocessar:
            resultados.append(json.loads(cache.read_text(encoding="utf-8")))
            print(f"[{i}/{len(pdfs)}] {caminho.name}: cache")
            continue

        registro = {"arquivo": caminho.name, "status": "ok", "erro": None, "dados": None}
        try:
            pdf_bytes = caminho.read_bytes()
            tipo = classificar_documento(client, model_id, pdf_bytes)
            registro["tipo_classificado"] = tipo.value
            if tipo not in TIPOS_DECISORIOS:
                registro["status"] = "ignorado"
            else:
                dados = extrair_decisao(client, model_id, pdf_bytes, parte_monitorada, apelidos)
                registro["dados"] = dados.model_dump(mode="json")
        except Exception as erro:  # registra e segue para o próximo arquivo
            registro["status"] = "erro"
            registro["erro"] = f"{type(erro).__name__}: {erro}"

        # Erros não vão para o cache, para serem tentados de novo.
        if registro["status"] != "erro":
            cache.write_text(json.dumps(registro, ensure_ascii=False, indent=2), encoding="utf-8")
        resultados.append(registro)
        print(f"[{i}/{len(pdfs)}] {caminho.name}: {registro['status']}")

    return resultados


# ---------------------------------------------------------------------------
# Validações e planilha
# ---------------------------------------------------------------------------

_PADRAO_CNJ = re.compile(r"^(\d{7})-?(\d{2})\.?(\d{4})\.?(\d)\.?(\d{2})\.?(\d{4})$")


def normalizar_numero_cnj(numero: str | None) -> str | None:
    """Formata como NNNNNNN-DD.AAAA.J.TR.OOOO; devolve None se não reconhecer."""
    if not numero:
        return None
    m = _PADRAO_CNJ.match(re.sub(r"\s", "", numero))
    if not m:
        return None
    n, dd, ano, j, tr, oooo = m.groups()
    return f"{n}-{dd}.{ano}.{j}.{tr}.{oooo}"


def validar_numero_cnj(numero: str | None) -> bool:
    """Confere os dígitos verificadores (módulo 97, Resolução CNJ 65/2008)."""
    formatado = normalizar_numero_cnj(numero)
    if not formatado:
        return False
    n, dd, ano, j, tr, oooo = _PADRAO_CNJ.match(formatado).groups()
    resto = int(f"{n}{ano}{j}{tr}{oooo}00") % 97
    return 98 - resto == int(dd)


def _alertas(dados: dict) -> list[str]:
    alertas = []
    if not validar_numero_cnj(dados.get("numero_processo")):
        alertas.append("número CNJ ausente ou inválido")
    if not dados.get("data_decisao"):
        alertas.append("sem data")
    if dados.get("polo_parte_monitorada") == "nao_identificado":
        alertas.append("parte monitorada não identificada")
    if any(t["codigo"] == "outra" for t in dados.get("teses", [])):
        alertas.append("tese fora do catálogo")
    if dados.get("incertezas"):
        alertas.append("modelo registrou incertezas")
    return alertas


def montar_planilha(
    resultados: list[dict], caminho_xlsx: str | Path, sobrescrever: bool = False
) -> dict[str, pd.DataFrame]:
    """Gera a planilha. Se o arquivo já existir e sobrescrever=False, salva uma
    cópia com data e hora no nome, para não apagar uma versão já conferida."""
    caminho_xlsx = Path(caminho_xlsx)
    if caminho_xlsx.exists() and not sobrescrever:
        carimbo = datetime.now().strftime("%Y-%m-%d_%H%M")
        caminho_xlsx = caminho_xlsx.with_name(f"{caminho_xlsx.stem}_{carimbo}{caminho_xlsx.suffix}")
    decisoes, teses, controle = [], [], []

    for r in resultados:
        controle.append(
            {
                "arquivo": r["arquivo"],
                "tipo_classificado": r.get("tipo_classificado"),
                "status": r["status"],
                "erro": r.get("erro"),
            }
        )
        dados = r.get("dados")
        if not dados:
            continue

        id_decisao = len(decisoes) + 1
        numero = normalizar_numero_cnj(dados.get("numero_processo")) or dados.get("numero_processo")
        partes = dados.get("partes", [])
        advogados_monitorada = sorted(
            {a for p in partes if p["polo"] == dados["polo_parte_monitorada"] for a in p["advogados"]}
        )
        decisoes.append(
            {
                "id_decisao": id_decisao,
                "numero_processo": numero,
                "arquivo": r["arquivo"],
                "tribunal": dados.get("tribunal"),
                "comarca": dados.get("comarca"),
                "orgao_julgador": dados.get("orgao_julgador"),
                "julgador": dados.get("julgador"),
                "classe_processual": dados.get("classe_processual"),
                "assunto": dados.get("assunto"),
                "data_decisao": dados.get("data_decisao"),
                "tipo_documento": dados["tipo_documento"],
                "instancia": dados["instancia"],
                "polo_parte_monitorada": dados["polo_parte_monitorada"],
                "resultado_para_parte_monitorada": dados["resultado_para_parte_monitorada"],
                "resultado_recurso": dados["resultado_recurso"],
                "recorrente": dados.get("recorrente"),
                "valor_causa": dados.get("valor_causa"),
                "valor_condenacao": dados.get("valor_condenacao"),
                "honorarios_percentual": dados.get("honorarios_percentual"),
                "parte_contraria": "; ".join(
                    p["nome"] for p in partes if p["polo"] != dados["polo_parte_monitorada"]
                ),
                "advogados_parte_monitorada": "; ".join(advogados_monitorada),
                "dispositivo": dados.get("dispositivo"),
                "resumo": dados.get("resumo"),
                "incertezas": dados.get("incertezas"),
                "alertas": "; ".join(_alertas(dados)),
                "conferido": "NÃO",
            }
        )
        for t in dados.get("teses", []):
            teses.append(
                {
                    "id_decisao": id_decisao,
                    "numero_processo": numero,
                    "data_decisao": dados.get("data_decisao"),
                    "tese": t["codigo"],
                    "descricao_livre": t.get("descricao_livre"),
                    "quem_alegou": t["quem_alegou"],
                    "situacao": t["situacao"],
                    "fundamento": t.get("fundamento"),
                    "trecho": t.get("trecho"),
                    "conferido": "NÃO",
                }
            )

    df_decisoes = pd.DataFrame(decisoes)
    df_teses = pd.DataFrame(teses)
    df_processos = _consolidar_processos(df_decisoes)
    df_codigos = pd.DataFrame(
        [{"codigo": k, "definicao": v} for k, v in CATALOGO_TESES.items()]
    )
    abas = {
        "Processos": df_processos,
        "Decisoes": df_decisoes,
        "Teses": df_teses,
        "Livro_de_Codigos": df_codigos,
        "Controle_Arquivos": pd.DataFrame(controle),
    }
    with pd.ExcelWriter(caminho_xlsx, engine="openpyxl") as escritor:
        for nome, df in abas.items():
            df.to_excel(escritor, sheet_name=nome, index=False)
            planilha = escritor.sheets[nome]
            planilha.freeze_panes = "A2"
            planilha.auto_filter.ref = planilha.dimensions
            for coluna in planilha.columns:
                largura = max((len(str(c.value)) for c in coluna if c.value is not None), default=8)
                planilha.column_dimensions[coluna[0].column_letter].width = min(max(largura + 2, 10), 60)
    print(f"Planilha salva em {caminho_xlsx}")
    return abas


def _consolidar_processos(df_decisoes: pd.DataFrame) -> pd.DataFrame:
    """Uma linha por processo; o desfecho provisório é o da decisão mais recente."""
    colunas = [
        "numero_processo", "tribunal", "comarca", "classe_processual", "polo_parte_monitorada",
        "parte_contraria", "advogados_parte_monitorada", "valor_causa", "qtd_decisoes",
        "data_ultima_decisao", "ultimo_resultado", "valor_condenacao_ultimo",
        "desfecho_final", "valor_efetivamente_recebido", "data_transito_arquivamento",
    ]
    if df_decisoes.empty:
        return pd.DataFrame(columns=colunas)

    linhas = []
    df = df_decisoes.copy()
    df["numero_processo"] = df["numero_processo"].fillna("SEM NÚMERO - " + df["arquivo"])
    for numero, grupo in df.groupby("numero_processo", sort=False):
        grupo = grupo.sort_values("data_decisao", na_position="first")
        ultima = grupo.iloc[-1]
        primeira_1grau = grupo[grupo["instancia"] == "primeiro_grau"]
        base = primeira_1grau.iloc[0] if not primeira_1grau.empty else grupo.iloc[0]
        linhas.append(
            {
                "numero_processo": numero,
                "tribunal": base["tribunal"],
                "comarca": base["comarca"],
                "classe_processual": base["classe_processual"],
                "polo_parte_monitorada": base["polo_parte_monitorada"],
                "parte_contraria": base["parte_contraria"],
                "advogados_parte_monitorada": base["advogados_parte_monitorada"],
                "valor_causa": grupo["valor_causa"].dropna().iloc[0]
                if grupo["valor_causa"].notna().any()
                else None,
                "qtd_decisoes": len(grupo),
                "data_ultima_decisao": ultima["data_decisao"],
                "ultimo_resultado": ultima["resultado_para_parte_monitorada"],
                "valor_condenacao_ultimo": ultima["valor_condenacao"],
                # Preenchimento manual: a decisão não informa o que foi pago de fato.
                "desfecho_final": None,
                "valor_efetivamente_recebido": None,
                "data_transito_arquivamento": None,
            }
        )
    return pd.DataFrame(linhas, columns=colunas)
