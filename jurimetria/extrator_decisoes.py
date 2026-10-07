"""Extração estruturada de decisões judiciais com Gemini, para um banco de jurimetria.

Adaptado do tutorial "Document Processing with Gemini" (Google, Apache 2.0).

Fluxo:
    (autos extensos) -> fatiar em peças (contestação, sentença, acórdão...) ->
    classificar cada PDF -> extrair decisões ou defesas -> salvar JSON por arquivo
    (cache) -> planilha com abas Processos/Decisoes/Teses/Defesas/Argumentos.

Os PDFs podem estar numa pasta local/Drive ou num bucket do Cloud Storage
(caminhos "gs://..."). O acesso a "gs://" exige o cliente da Agent Platform/Vertex.

O livro de códigos (CATALOGO_TESES) fica no topo do arquivo: edite-o para
incluir as teses que importam no seu acervo. O modelo só pode escolher códigos
dessa lista (ou "outra"), o que mantém a classificação consistente.
"""

from __future__ import annotations

import json
import re
import tempfile
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

# Acima disso, o envio direto de bytes é recusado: use o bucket (gs://)
# ou fatiar_autos() para separar as peças.
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
    CONTESTACAO = "contestacao"
    RECURSO = "recurso"
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

# Peças de defesa: mostram o roteiro completo, inclusive o que o juiz não apreciou.
TIPOS_DEFESA = {TipoDocumento.CONTESTACAO, TipoDocumento.RECURSO}


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


class TipoPeca(Enum):
    CONTESTACAO = "contestacao"
    APELACAO = "apelacao"
    CONTRARRAZOES = "contrarrazoes"
    AGRAVO = "agravo"
    EMBARGOS_DECLARACAO = "embargos_declaracao"
    RECURSO_TRIBUNAL_SUPERIOR = "recurso_tribunal_superior"
    OUTRA = "outra"


class CategoriaArgumento(Enum):
    PRELIMINAR = "preliminar"
    PREJUDICIAL_MERITO = "prejudicial_merito"
    MERITO = "merito"
    SUBSIDIARIO = "subsidiario"


class PosicaoAcordo(Enum):
    INTERESSE = "interesse"
    DESINTERESSE = "desinteresse"
    NAO_MENCIONA = "nao_menciona"


class ArgumentoDefesa(BaseModel):
    categoria: CategoriaArgumento
    codigo: TeseCodigo = Field(..., description="Código do livro de códigos")
    descricao_livre: str | None = Field(
        None, description="Obrigatória se codigo = outra; senão, detalhe opcional"
    )
    fundamentos: list[str] = Field(
        default_factory=list,
        description="Dispositivos legais, súmulas, temas e precedentes citados para este argumento",
    )
    trecho: str | None = Field(
        None, description="Trecho literal curto (até 300 caracteres) que resume o argumento"
    )


class DefesaExtraida(BaseModel):
    numero_processo: str | None = Field(
        None, description="Número CNJ no formato NNNNNNN-DD.AAAA.J.TR.OOOO"
    )
    tipo_peca: TipoPeca
    data_protocolo: str | None = Field(None, description="AAAA-MM-DD")
    peticionante: str = Field(..., description="Parte que apresenta a peça")
    peca_da_parte_monitorada: bool = Field(
        ..., description="true se a peça foi apresentada pela parte monitorada"
    )
    escritorio: str | None = Field(None, description="Escritório de advocacia, se constar")
    advogados: list[str] = Field(default_factory=list, description="Com OAB, se constar")
    argumentos: list[ArgumentoDefesa] = Field(
        ..., description="Todos os argumentos, na ordem em que aparecem na peça"
    )
    documentos_juntados: list[str] = Field(
        default_factory=list, description="Documentos mencionados como anexos"
    )
    provas_requeridas: list[str] = Field(
        default_factory=list, description="Ex.: perícia de engenharia, prova testemunhal"
    )
    posicao_acordo: PosicaoAcordo
    pedidos: str = Field(..., description="Síntese dos pedidos finais, até 3 frases")
    resumo: str = Field(..., description="Linha de defesa em até 3 frases")
    incertezas: str | None = Field(
        None, description="Pontos ilegíveis, ambíguos ou que exigem conferência humana"
    )


class Peca(BaseModel):
    tipo: TipoDocumento
    pagina_inicial: int = Field(..., description="Página física do PDF, a partir de 1")
    pagina_final: int
    descricao: str | None = Field(None, description="Ex.: 'Contestação de XYZ SPE Ltda.'")


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
- contestacao: contestação ou defesa do réu (inclui reconvenção na mesma peça).
- recurso: apelação, contrarrazões, agravo, embargos de declaração opostos pela
  parte ou recurso aos tribunais superiores (a peça da parte, não o julgamento).
- despacho: mero impulso, sem conteúdo decisório.
- outro: petição inicial, procurações, certidões, comprovantes e demais documentos."""


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


def instrucao_defesa(parte_monitorada: str, apelidos: list[str]) -> str:
    teses = "\n".join(f"- {k}: {v}" for k, v in CATALOGO_TESES.items())
    nomes = ", ".join([parte_monitorada, *apelidos])
    return f"""Você é analista de estratégia processual e mapeia a linha de defesa
apresentada em peças processuais brasileiras (contestações e recursos).

PARTE MONITORADA: {parte_monitorada}
Também pode aparecer como: {nomes}. Sociedades de propósito específico (SPE)
do mesmo grupo contam como parte monitorada.

Regras:
1. Use apenas o que está na peça. Nunca invente. Sem informação -> null ou lista vazia.
2. Liste TODOS os argumentos, na ordem da peça, classificando a categoria:
   preliminar (questões processuais), prejudicial_merito (prescrição, decadência),
   merito, ou subsidiario (pedidos "caso assim não se entenda").
3. Use somente estes códigos de tese:
{teses}
4. Em "fundamentos", liste leis, súmulas, temas repetitivos e julgados citados.
5. Em "trecho", copie literalmente uma passagem curta que resuma o argumento.
6. Datas em AAAA-MM-DD.
7. Qualquer dúvida, texto ilegível ou ambiguidade vai em "incertezas"."""


def prompt_pecas(parte_monitorada: str) -> str:
    return f"""Este documento são os autos (ou parte dos autos) de um processo judicial.
Liste as peças relevantes, com a página inicial e a final de cada uma:
- decisões: sentenças, acórdãos (ementa, relatório e voto), decisões monocráticas,
  decisões interlocutórias relevantes (tutela, liminar) e decisões em embargos;
- contestações e recursos (apelação, contrarrazões, agravo, embargos de declaração),
  principalmente os apresentados por {parte_monitorada} ou por suas SPEs.
Ignore petição inicial, procurações, certidões, comprovantes, documentos anexos
e despachos de mero expediente.
Use a numeração física das páginas do PDF (a primeira página é 1). Na dúvida
sobre onde a peça termina, inclua a página seguinte."""


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


def _parte_pdf(fonte: bytes | str) -> Part:
    """Aceita os bytes do PDF ou um caminho "gs://bucket/arquivo.pdf"."""
    if isinstance(fonte, str):
        return Part.from_uri(file_uri=fonte, mime_type=PDF_MIME_TYPE)
    if len(fonte) > LIMITE_PDF_BYTES:
        raise ValueError(
            "PDF grande demais para envio direto. Envie pelo bucket (gs://) ou use "
            "fatiar_autos() para separar as peças."
        )
    return Part.from_bytes(data=fonte, mime_type=PDF_MIME_TYPE)


def classificar_documento(client, model_id: str, fonte: bytes | str) -> TipoDocumento:
    resposta = _com_retentativas(
        lambda: client.models.generate_content(
            model=model_id,
            contents=["Classifique o documento a seguir.", _parte_pdf(fonte)],
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
    fonte: bytes | str,
    parte_monitorada: str,
    apelidos: list[str] | None = None,
) -> DecisaoExtraida:
    resposta = _com_retentativas(
        lambda: client.models.generate_content(
            model=model_id,
            contents=["Extraia os dados da decisão judicial a seguir.", _parte_pdf(fonte)],
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


def extrair_defesa(
    client,
    model_id: str,
    fonte: bytes | str,
    parte_monitorada: str,
    apelidos: list[str] | None = None,
) -> DefesaExtraida:
    resposta = _com_retentativas(
        lambda: client.models.generate_content(
            model=model_id,
            contents=["Mapeie a linha de defesa da peça a seguir.", _parte_pdf(fonte)],
            config=GenerateContentConfig(
                system_instruction=instrucao_defesa(parte_monitorada, apelidos or []),
                response_schema=DefesaExtraida,
                response_mime_type=JSON_MIME_TYPE,
                temperature=0,
            ),
        )
    )
    if resposta.parsed is None:
        return DefesaExtraida.model_validate_json(resposta.text)
    return resposta.parsed


def localizar_pecas(client, model_id: str, fonte: bytes | str, parte_monitorada: str) -> list[Peca]:
    """Lista as peças relevantes (decisões, contestações, recursos) de um PDF de autos."""
    resposta = _com_retentativas(
        lambda: client.models.generate_content(
            model=model_id,
            contents=["<Documento>", _parte_pdf(fonte), "</Documento>", prompt_pecas(parte_monitorada)],
            config=GenerateContentConfig(
                response_schema=list[Peca],
                response_mime_type=JSON_MIME_TYPE,
                temperature=0,
            ),
        )
    )
    pecas = resposta.parsed or []
    relevantes = TIPOS_DECISORIOS | TIPOS_DEFESA
    return [p for p in pecas if p.tipo in relevantes and p.pagina_final >= p.pagina_inicial]


def recortar_pdf(arquivo_entrada: str | Path, arquivo_saida: str | Path, paginas: list[int]) -> None:
    """Gera um PDF só com as páginas indicadas (numeração a partir de 1)."""
    leitor = pypdf.PdfReader(str(arquivo_entrada))
    escritor = pypdf.PdfWriter()
    for numero in paginas:
        if 1 <= numero <= len(leitor.pages):
            escritor.add_page(leitor.pages[numero - 1])
    escritor.write(str(arquivo_saida))


# ---------------------------------------------------------------------------
# Cloud Storage
# ---------------------------------------------------------------------------


def _eh_gs(caminho) -> bool:
    return isinstance(caminho, str) and caminho.startswith("gs://")


def _blob(uri: str):
    from google.cloud import storage  # importado só quando o bucket é usado

    bucket, _, nome = uri.removeprefix("gs://").partition("/")
    return storage.Client().bucket(bucket).blob(nome)


def _listar_pdfs(origem: str | Path) -> list[tuple[str, str | Path]]:
    """(nome do arquivo, caminho) de cada PDF da origem, sem entrar em subpastas."""
    if _eh_gs(origem):
        from google.cloud import storage

        bucket, _, prefixo = origem.removeprefix("gs://").partition("/")
        prefixo = prefixo.rstrip("/") + "/" if prefixo else ""
        blobs = storage.Client().list_blobs(bucket, prefix=prefixo, delimiter="/")
        return sorted(
            (b.name.rsplit("/", 1)[-1], f"gs://{bucket}/{b.name}")
            for b in blobs
            if b.name.lower().endswith(".pdf")
        )
    pasta = Path(origem)
    return sorted((p.name, p) for p in pasta.iterdir() if p.suffix.lower() == ".pdf")


def _juntar(origem: str | Path, *partes: str) -> str | Path:
    if _eh_gs(origem):
        return "/".join([origem.rstrip("/"), *partes])
    return Path(origem).joinpath(*partes)


def _ler_texto(caminho) -> str | None:
    if _eh_gs(caminho):
        blob = _blob(caminho)
        return blob.download_as_text(encoding="utf-8") if blob.exists() else None
    caminho = Path(caminho)
    return caminho.read_text(encoding="utf-8") if caminho.exists() else None


def _gravar_texto(caminho, texto: str) -> None:
    if _eh_gs(caminho):
        _blob(caminho).upload_from_string(texto, content_type="application/json")
    else:
        Path(caminho).parent.mkdir(parents=True, exist_ok=True)
        Path(caminho).write_text(texto, encoding="utf-8")


def _baixar(caminho, destino_local: Path) -> Path:
    if _eh_gs(caminho):
        _blob(caminho).download_to_filename(str(destino_local))
        return destino_local
    return Path(caminho)


def _enviar(arquivo_local: Path, destino) -> None:
    if _eh_gs(destino):
        _blob(destino).upload_from_filename(str(arquivo_local), content_type=PDF_MIME_TYPE)
    else:
        Path(destino).parent.mkdir(parents=True, exist_ok=True)
        Path(destino).write_bytes(arquivo_local.read_bytes())


# ---------------------------------------------------------------------------
# Autos extensos: fatiar em peças
# ---------------------------------------------------------------------------


def _unir_pecas_vizinhas(pecas: list[Peca]) -> list[Peca]:
    """Junta trechos da mesma peça que ficaram divididos entre dois blocos."""
    unidas: list[Peca] = []
    for p in sorted(pecas, key=lambda x: x.pagina_inicial):
        anterior = unidas[-1] if unidas else None
        if anterior and anterior.tipo == p.tipo and p.pagina_inicial <= anterior.pagina_final + 1:
            anterior.pagina_final = max(anterior.pagina_final, p.pagina_final)
        else:
            unidas.append(p.model_copy())
    return unidas


def fatiar_autos(
    client,
    model_id: str,
    autos: str | Path,
    destino: str | Path,
    parte_monitorada: str,
    paginas_por_bloco: int = 150,
) -> list[dict]:
    """Localiza as peças relevantes em autos extensos e grava um PDF por peça em
    `destino` (pasta local ou gs://), com nomes como
    "<autos>_03_contestacao_p120-158.pdf". Devolve a lista de peças gravadas.

    Os autos são lidos em blocos de `paginas_por_bloco` páginas, para respeitar
    os limites do modelo. Confira os recortes antes de processar o lote."""
    nome_autos = Path(str(autos).rsplit("/", 1)[-1]).stem
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        local = _baixar(autos, tmp / "autos.pdf")
        leitor = pypdf.PdfReader(str(local))
        total = len(leitor.pages)

        encontradas: list[Peca] = []
        for inicio in range(0, total, paginas_por_bloco):
            fim = min(inicio + paginas_por_bloco, total)
            bloco = tmp / f"bloco_{inicio + 1}.pdf"
            escritor = pypdf.PdfWriter()
            for i in range(inicio, fim):
                escritor.add_page(leitor.pages[i])
            escritor.write(str(bloco))

            if _eh_gs(destino):
                # Na raiz do bucket, para a regra de ciclo de vida de _tmp/ alcançar.
                bucket = destino.removeprefix("gs://").split("/", 1)[0]
                uri_bloco = f"gs://{bucket}/_tmp/{nome_autos}_bloco_{inicio + 1}.pdf"
                _enviar(bloco, uri_bloco)
                fonte = uri_bloco
            else:
                fonte = bloco.read_bytes()
            try:
                pecas = localizar_pecas(client, model_id, fonte, parte_monitorada)
            finally:
                if _eh_gs(destino):
                    _blob(uri_bloco).delete()

            for p in pecas:  # converte para a numeração dos autos completos
                p.pagina_inicial = min(max(p.pagina_inicial, 1), fim - inicio) + inicio
                p.pagina_final = min(max(p.pagina_final, 1), fim - inicio) + inicio
            encontradas.extend(pecas)
            print(f"Páginas {inicio + 1}-{fim} de {total}: {len(pecas)} peça(s)")

        gravadas = []
        for n, p in enumerate(_unir_pecas_vizinhas(encontradas), 1):
            nome = f"{nome_autos}_{n:02d}_{p.tipo.value}_p{p.pagina_inicial}-{p.pagina_final}.pdf"
            arquivo = tmp / nome
            recortar_pdf(local, arquivo, list(range(p.pagina_inicial, p.pagina_final + 1)))
            _enviar(arquivo, _juntar(destino, nome))
            gravadas.append({"arquivo": nome, **p.model_dump(mode="json")})
    return gravadas


# ---------------------------------------------------------------------------
# Processamento em lote, com cache por arquivo
# ---------------------------------------------------------------------------


def processar_pasta(
    client,
    model_id: str,
    origem: str | Path,
    parte_monitorada: str,
    apelidos: list[str] | None = None,
    reprocessar: bool = False,
) -> list[dict]:
    """Processa todos os PDFs de uma pasta local ou de um prefixo gs://.

    Decisões vão para "dados"; contestações e recursos, para "defesa". Cada
    resultado é salvo em <origem>/_extracoes/<arquivo>.json; numa nova execução,
    arquivos já processados são lidos do cache (sem custo), salvo se
    reprocessar=True. Com gs://, o cache também fica no bucket."""
    resultados = []
    pdfs = _listar_pdfs(origem)
    for i, (nome, caminho) in enumerate(pdfs, 1):
        cache = _juntar(origem, "_extracoes", f"{Path(nome).stem}.json")
        em_cache = None if reprocessar else _ler_texto(cache)
        if em_cache:
            resultados.append(json.loads(em_cache))
            print(f"[{i}/{len(pdfs)}] {nome}: cache")
            continue

        registro = {"arquivo": nome, "status": "ok", "erro": None, "dados": None, "defesa": None}
        try:
            fonte = caminho if _eh_gs(caminho) else Path(caminho).read_bytes()
            tipo = classificar_documento(client, model_id, fonte)
            registro["tipo_classificado"] = tipo.value
            if tipo in TIPOS_DECISORIOS:
                dados = extrair_decisao(client, model_id, fonte, parte_monitorada, apelidos)
                registro["dados"] = dados.model_dump(mode="json")
            elif tipo in TIPOS_DEFESA:
                defesa = extrair_defesa(client, model_id, fonte, parte_monitorada, apelidos)
                registro["defesa"] = defesa.model_dump(mode="json")
            else:
                registro["status"] = "ignorado"
        except Exception as erro:  # registra e segue para o próximo arquivo
            registro["status"] = "erro"
            registro["erro"] = f"{type(erro).__name__}: {erro}"

        # Erros não vão para o cache, para serem tentados de novo.
        if registro["status"] != "erro":
            _gravar_texto(cache, json.dumps(registro, ensure_ascii=False, indent=2))
        resultados.append(registro)
        print(f"[{i}/{len(pdfs)}] {nome}: {registro['status']}")

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
    decisoes, teses, defesas, argumentos, controle = [], [], [], [], []

    for r in resultados:
        controle.append(
            {
                "arquivo": r["arquivo"],
                "tipo_classificado": r.get("tipo_classificado"),
                "status": r["status"],
                "erro": r.get("erro"),
            }
        )
        if r.get("defesa"):
            _registrar_defesa(r["arquivo"], r["defesa"], defesas, argumentos)
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
        "Defesas": pd.DataFrame(defesas),
        "Argumentos_Defesa": pd.DataFrame(argumentos),
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


def _registrar_defesa(arquivo: str, defesa: dict, defesas: list, argumentos: list) -> None:
    id_defesa = len(defesas) + 1
    numero = normalizar_numero_cnj(defesa.get("numero_processo")) or defesa.get("numero_processo")
    alertas = []
    if not validar_numero_cnj(defesa.get("numero_processo")):
        alertas.append("número CNJ ausente ou inválido")
    if not defesa.get("peca_da_parte_monitorada"):
        alertas.append("peça de outra parte")
    if any(a["codigo"] == "outra" for a in defesa.get("argumentos", [])):
        alertas.append("argumento fora do catálogo")
    if defesa.get("incertezas"):
        alertas.append("modelo registrou incertezas")
    defesas.append(
        {
            "id_defesa": id_defesa,
            "numero_processo": numero,
            "arquivo": arquivo,
            "tipo_peca": defesa["tipo_peca"],
            "data_protocolo": defesa.get("data_protocolo"),
            "peticionante": defesa.get("peticionante"),
            "peca_da_parte_monitorada": defesa.get("peca_da_parte_monitorada"),
            "escritorio": defesa.get("escritorio"),
            "advogados": "; ".join(defesa.get("advogados", [])),
            "qtd_argumentos": len(defesa.get("argumentos", [])),
            "documentos_juntados": "; ".join(defesa.get("documentos_juntados", [])),
            "provas_requeridas": "; ".join(defesa.get("provas_requeridas", [])),
            "posicao_acordo": defesa.get("posicao_acordo"),
            "pedidos": defesa.get("pedidos"),
            "resumo": defesa.get("resumo"),
            "incertezas": defesa.get("incertezas"),
            "alertas": "; ".join(alertas),
            "conferido": "NÃO",
        }
    )
    for ordem, a in enumerate(defesa.get("argumentos", []), 1):
        argumentos.append(
            {
                "id_defesa": id_defesa,
                "numero_processo": numero,
                "tipo_peca": defesa["tipo_peca"],
                "peca_da_parte_monitorada": defesa.get("peca_da_parte_monitorada"),
                "ordem": ordem,
                "categoria": a["categoria"],
                "tese": a["codigo"],
                "descricao_livre": a.get("descricao_livre"),
                "fundamentos": "; ".join(a.get("fundamentos", [])),
                "trecho": a.get("trecho"),
                "conferido": "NÃO",
            }
        )


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
