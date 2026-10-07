"""Gera jurimetria_decisoes.ipynb a partir de extrator_decisoes.py.

Rode depois de editar o módulo: python gerar_notebook.py
"""

import json
from pathlib import Path

AQUI = Path(__file__).parent
MODULO = (AQUI / "extrator_decisoes.py").read_text(encoding="utf-8")


def md(texto):
    return {"cell_type": "markdown", "metadata": {}, "source": texto.strip()}


def code(texto):
    return {
        "cell_type": "code",
        "metadata": {},
        "execution_count": None,
        "outputs": [],
        "source": texto.strip(),
    }


celulas = [
    md("""
# Banco de decisões judiciais com Gemini

Lê PDFs de decisões (sentenças, acórdãos, decisões interlocutórias), extrai os dados
de forma padronizada e monta uma planilha com as abas **Processos**, **Decisoes**,
**Teses**, **Livro_de_Codigos** e **Controle_Arquivos**.

Adaptado do tutorial *Document Processing with Gemini* (Google, Apache 2.0).

**Antes de usar com dados reais**
- **LGPD:** decisões contêm dados pessoais. Use conta paga (Agent Platform/Vertex ou
  Gemini API com faturamento ativo). Em contas gratuitas, os termos do Google podem
  permitir o uso do conteúdo para melhorar os produtos. Confira os termos vigentes.
- **Processos em segredo de justiça** não devem entrar no lote.
- **Conferência humana:** toda linha nasce com `conferido = NÃO`. Confira principalmente
  resultado, valores e situação das teses antes de usar nas análises.
- **Custo:** cada PDF gera 2 chamadas (classificação + extração). Os resultados ficam em
  cache: rodar de novo não cobra os arquivos já processados.
"""),
    md("## 1. Instalação"),
    code("%pip install --upgrade --quiet google-genai pypdf pydantic pandas openpyxl"),
    md("""
## 2. Configuração

Escolha **uma** forma de acesso:
- **A) Google Cloud (Agent Platform/Vertex):** preencha `PROJECT_ID` e deixe `API_KEY` vazio.
  Exige projeto com faturamento e a API habilitada.
- **B) Gemini API (AI Studio):** crie a chave em aistudio.google.com, salve nos *Secrets*
  do Colab (ícone de chave, à esquerda) com o nome `GEMINI_API_KEY` e deixe `PROJECT_ID` vazio.

Os PDFs devem ficar numa pasta do Google Drive, um arquivo por decisão.
"""),
    code("""
# fmt: off
PROJECT_ID = ""  # @param {type: "string"}
LOCATION = "global"  # @param {type: "string"}
MODEL_ID = "gemini-3.8-flash"  # @param {type: "string"}

PARTE_MONITORADA = "NOME DA PARTE LTDA"  # @param {type: "string"}
# Outras grafias, nome fantasia, CNPJ/CPF como aparece nas decisões
APELIDOS = ["NOME DA PARTE", "00.000.000/0001-00"]

PASTA_PDFS = "/content/drive/MyDrive/Jurimetria/NomeDaParte"  # @param {type: "string"}
ARQUIVO_SAIDA = "/content/drive/MyDrive/Jurimetria/banco_decisoes.xlsx"  # @param {type: "string"}
# fmt: on
"""),
    code("""
import sys

API_KEY = None
if "google.colab" in sys.modules:
    from google.colab import drive
    drive.mount("/content/drive")
    if PROJECT_ID:
        from google.colab import auth
        auth.authenticate_user()
    else:
        from google.colab import userdata
        API_KEY = userdata.get("GEMINI_API_KEY")
"""),
    md("""
## 3. Módulo de extração

A célula abaixo grava o arquivo `extrator_decisoes.py`. Para mudar o **livro de códigos**
(lista de teses), edite o dicionário `CATALOGO_TESES` no início dela e rode de novo.
"""),
    code("%%writefile extrator_decisoes.py\n" + MODULO),
    code("""
import importlib, json
import extrator_decisoes as ed
importlib.reload(ed)

client = ed.criar_cliente(project_id=PROJECT_ID or None, location=LOCATION, api_key=API_KEY)
"""),
    md("""
## 4. Teste com um único PDF

Antes do lote, rode com uma decisão que você conhece bem e confira campo a campo.
É o momento de ajustar o livro de códigos e as instruções.
"""),
    code("""
from pathlib import Path

ARQUIVO_TESTE = sorted(Path(PASTA_PDFS).glob("*.pdf"))[0]  # ou o caminho de um PDF específico
pdf_bytes = Path(ARQUIVO_TESTE).read_bytes()

tipo = ed.classificar_documento(client, MODEL_ID, pdf_bytes)
print("Tipo:", tipo.value)

decisao = ed.extrair_decisao(client, MODEL_ID, pdf_bytes, PARTE_MONITORADA, APELIDOS)
print(json.dumps(decisao.model_dump(mode="json"), ensure_ascii=False, indent=2))
print("Número CNJ válido?", ed.validar_numero_cnj(decisao.numero_processo))
"""),
    md("""
## 5. (Opcional) Autos completos: localizar e recortar as decisões

Se você baixou o processo inteiro do eproc, use esta etapa para gerar um PDF só com as
páginas decisórias. Como o modelo pode errar, a página seguinte a cada uma também é
incluída, e vale conferir o PDF recortado. Coloque o recorte na pasta do lote.
Observação: arquivos muito grandes (acima de ~18 MB) precisam ser divididos antes.
"""),
    code("""
AUTOS = "/content/drive/MyDrive/Jurimetria/autos_completos.pdf"  # @param {type: "string"}

paginas = ed.localizar_paginas_decisoes(client, MODEL_ID, Path(AUTOS).read_bytes())
paginas_ampliadas = sorted({p for pg in paginas for p in (pg, pg + 1)})
print("Páginas encontradas:", paginas, "-> recorte:", paginas_ampliadas)

saida = Path(PASTA_PDFS) / (Path(AUTOS).stem + "_decisoes.pdf")
ed.recortar_pdf(AUTOS, saida, paginas_ampliadas)
print("Gerado:", saida)
"""),
    md("""
## 6. Processar a pasta inteira

Despachos e documentos não decisórios são ignorados. Erros não interrompem o lote e
aparecem na aba *Controle_Arquivos*; basta rodar de novo para tentar só os que falharam.
"""),
    code("""
resultados = ed.processar_pasta(client, MODEL_ID, PASTA_PDFS, PARTE_MONITORADA, APELIDOS)

from collections import Counter
print(Counter(r["status"] for r in resultados))
"""),
    md("## 7. Gerar a planilha"),
    code("""
abas = ed.montar_planilha(resultados, ARQUIVO_SAIDA)
abas["Decisoes"].head()
"""),
    md("""
## 8. Primeira leitura estratégica

Indicadores rápidos. Com poucas decisões, trate os números como indícios, não previsões.
"""),
    code("""
import pandas as pd

dec, tes = abas["Decisoes"], abas["Teses"]
if not dec.empty:
    display(pd.crosstab(dec["classe_processual"], dec["resultado_para_parte_monitorada"], margins=True))
    display(pd.crosstab(dec["orgao_julgador"], dec["resultado_para_parte_monitorada"], margins=True))
if not tes.empty:
    # Teses mais alegadas pela parte monitorada e quanto foram acolhidas
    da_parte = tes[tes["quem_alegou"] == "parte_monitorada"]
    display(pd.crosstab(da_parte["tese"], da_parte["situacao"], margins=True))
"""),
    md("""
## 9. Conferência e manutenção

1. Abra a planilha e filtre a coluna **alertas** (número CNJ inválido, sem data, tese fora
   do catálogo, incertezas do modelo). Comece por essas linhas.
2. Confira cada decisão com o PDF e mude **conferido** para `SIM`.
3. Na aba **Processos**, preencha à mão `desfecho_final`, `valor_efetivamente_recebido` e
   `data_transito_arquivamento`: essas informações não estão nas decisões.
4. Se muitas teses caírem em `outra`, é sinal de que o livro de códigos precisa de um novo
   código. Inclua-o em `CATALOGO_TESES` e reprocesse (`reprocessar=True`).
5. Se o arquivo de saída já existir, uma nova planilha é criada com data e hora no nome,
   para nunca apagar uma versão já conferida. Trate a planilha conferida como a versão
   oficial e copie para ela apenas as linhas novas.
"""),
]

notebook = {
    "cells": celulas,
    "metadata": {
        "colab": {"provenance": []},
        "kernelspec": {"display_name": "Python 3", "name": "python3"},
        "language_info": {"name": "python"},
    },
    "nbformat": 4,
    "nbformat_minor": 0,
}

(AQUI / "jurimetria_decisoes.ipynb").write_text(
    json.dumps(notebook, ensure_ascii=False, indent=1), encoding="utf-8"
)
print("Notebook gerado.")
