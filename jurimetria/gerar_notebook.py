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
# Banco de decisões e defesas com Gemini

Lê autos e peças processuais em PDF, separa as peças relevantes (contestações, recursos,
sentenças, acórdãos), extrai os dados de forma padronizada e monta uma planilha com as
abas **Processos**, **Decisoes**, **Teses**, **Defesas**, **Argumentos_Defesa**,
**Livro_de_Codigos** e **Controle_Arquivos**.

Adaptado do tutorial *Document Processing with Gemini* (Google, Apache 2.0).

**Antes de usar com dados reais**
- **LGPD:** as peças contêm dados pessoais. Use conta paga (Agent Platform/Vertex). Em
  contas gratuitas, os termos do Google podem permitir o uso do conteúdo para melhorar
  os produtos. Confira os termos vigentes.
- **Processos em segredo de justiça** não devem entrar no lote.
- **Conferência humana:** toda linha nasce com `conferido = NÃO`. Confira principalmente
  resultados, valores e argumentos antes de usar nas análises.
- **Custo:** cada peça gera 2 chamadas (classificação + extração); o fatiamento dos autos,
  1 chamada a cada bloco de páginas. Os resultados ficam em cache: rodar de novo não
  cobra o que já foi processado.
"""),
    md("## 1. Instalação"),
    code("%pip install --upgrade --quiet google-genai google-cloud-storage pypdf pydantic pandas openpyxl"),
    md("""
## 2. Configuração

**Com bucket (recomendado para autos extensos):** preencha `PROJECT_ID` e use caminhos
`gs://`. O bucket só funciona com o acesso pelo Google Cloud (Agent Platform/Vertex).

**Sem bucket:** deixe `PROJECT_ID` vazio, salve a chave da Gemini API nos *Secrets* do
Colab com o nome `GEMINI_API_KEY` e use pastas do Google Drive (`/content/drive/...`).
Nesse modo, cada PDF enviado precisa ter menos de ~18 MB.

Estrutura sugerida no bucket:
```
gs://SEU-BUCKET/caso-01/autos/   <- PDFs completos, como baixados do eproc
gs://SEU-BUCKET/caso-01/pecas/   <- gerado pelo notebook: um PDF por peça
```
"""),
    code("""
# fmt: off
PROJECT_ID = ""  # @param {type: "string"}
LOCATION = "global"  # @param {type: "string"}
MODEL_ID = "gemini-3.8-flash"  # @param {type: "string"}

PARTE_MONITORADA = "Cyrela Brazil Realty S.A. Empreendimentos e Participações"  # @param {type: "string"}
# Outras grafias, marcas, SPEs e CNPJs como aparecem nos autos
APELIDOS = ["Cyrela", "Living", "Vivaz"]

PASTA_AUTOS = "gs://SEU-BUCKET/caso-01/autos"  # @param {type: "string"}
PASTA_PECAS = "gs://SEU-BUCKET/caso-01/pecas"  # @param {type: "string"}
ARQUIVO_SAIDA = "/content/drive/MyDrive/Jurimetria/banco_cyrela.xlsx"  # @param {type: "string"}
# fmt: on
"""),
    code("""
import os, sys

API_KEY = None
if PROJECT_ID:
    os.environ["GOOGLE_CLOUD_PROJECT"] = PROJECT_ID  # usado pelo cliente do Cloud Storage
if "google.colab" in sys.modules:
    from google.colab import drive
    drive.mount("/content/drive")
    if PROJECT_ID:
        from google.colab import auth
        auth.authenticate_user()
    else:
        from google.colab import userdata
        API_KEY = userdata.get("GEMINI_API_KEY")

if PASTA_AUTOS.startswith("gs://") and not PROJECT_ID:
    raise ValueError("Para ler do bucket (gs://), preencha PROJECT_ID.")
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
print("PDFs em PASTA_AUTOS:", [nome for nome, _ in ed._listar_pdfs(PASTA_AUTOS)])
"""),
    md("""
## 4. Fatiar os autos em peças

Para cada PDF de autos, o modelo localiza contestações, recursos e decisões e grava um
PDF por peça em `PASTA_PECAS`, com nomes como `autos_03_contestacao_p120-158.pdf`.

**Confira os recortes** (abra dois ou três no console do Cloud Storage). Se uma peça vier
cortada ou faltando, recorte à mão com `ed.recortar_pdf` ou coloque o PDF da peça
diretamente em `PASTA_PECAS`. Se você já tem as peças separadas, pule esta etapa.
"""),
    code("""
mapa_pecas = {}
for nome, caminho in ed._listar_pdfs(PASTA_AUTOS):
    print(f"\\n== {nome}")
    mapa_pecas[nome] = ed.fatiar_autos(client, MODEL_ID, caminho, PASTA_PECAS, PARTE_MONITORADA)
    for p in mapa_pecas[nome]:
        print(f"  {p['tipo']:<28} págs. {p['pagina_inicial']}-{p['pagina_final']}  {p.get('descricao') or ''}")
"""),
    md("""
## 5. Teste com uma única peça

Antes do lote, rode com uma peça que você conhece bem (de preferência uma contestação)
e confira campo a campo. É o momento de ajustar o livro de códigos.
"""),
    code("""
NOME_TESTE, PECA_TESTE = ed._listar_pdfs(PASTA_PECAS)[0]  # troque o índice para outra peça
fonte = PECA_TESTE if str(PECA_TESTE).startswith("gs://") else open(PECA_TESTE, "rb").read()

tipo = ed.classificar_documento(client, MODEL_ID, fonte)
print(NOME_TESTE, "->", tipo.value)
if tipo in ed.TIPOS_DEFESA:
    resultado = ed.extrair_defesa(client, MODEL_ID, fonte, PARTE_MONITORADA, APELIDOS)
else:
    resultado = ed.extrair_decisao(client, MODEL_ID, fonte, PARTE_MONITORADA, APELIDOS)
print(json.dumps(resultado.model_dump(mode="json"), ensure_ascii=False, indent=2))
"""),
    md("""
## 6. Processar todas as peças

Despachos e documentos irrelevantes são ignorados. Erros não interrompem o lote e
aparecem na aba *Controle_Arquivos*; basta rodar de novo para tentar só os que falharam.
O cache fica em `PASTA_PECAS/_extracoes/`.
"""),
    code("""
resultados = ed.processar_pasta(client, MODEL_ID, PASTA_PECAS, PARTE_MONITORADA, APELIDOS)

from collections import Counter
print(Counter(r["status"] for r in resultados))
"""),
    md("## 7. Gerar a planilha"),
    code("""
abas = ed.montar_planilha(resultados, ARQUIVO_SAIDA)
abas["Defesas"].head()
"""),
    md("""
## 8. Mapa da defesa

Com poucos processos, trate os números como indícios, não como previsões.
"""),
    code("""
import pandas as pd

arg, tes = abas["Argumentos_Defesa"], abas["Teses"]
if not arg.empty:
    arg = arg[arg["peca_da_parte_monitorada"] == True]
    # Em quantos processos cada argumento aparece, por categoria
    freq = (arg.groupby(["categoria", "tese"])["numero_processo"].nunique()
              .rename("processos").reset_index().sort_values(["categoria", "processos"], ascending=[True, False]))
    display(freq)
    # Ordem típica: posição média de cada argumento dentro da peça
    display(arg.groupby("tese")["ordem"].mean().sort_values().rename("posicao_media").to_frame())

if not tes.empty:
    # Como os julgadores receberam as teses da parte monitorada
    da_parte = tes[tes["quem_alegou"] == "parte_monitorada"]
    display(pd.crosstab(da_parte["tese"], da_parte["situacao"], margins=True))

dfs = abas["Defesas"]
if not dfs.empty:
    display(dfs[["escritorio", "posicao_acordo", "provas_requeridas"]].value_counts().to_frame("peças"))
"""),
    md("""
## 9. Conferência e manutenção

1. Abra a planilha e filtre a coluna **alertas** (número CNJ inválido, peça de outra parte,
   tese fora do catálogo, incertezas do modelo). Comece por essas linhas.
2. Confira cada linha com o PDF e mude **conferido** para `SIM`.
3. Na aba **Processos**, preencha à mão `desfecho_final`, `valor_efetivamente_recebido` e
   `data_transito_arquivamento`: essas informações não estão nas decisões.
4. Se muitos argumentos caírem em `outra`, falta um código no livro. Inclua-o em
   `CATALOGO_TESES` e reprocesse (`reprocessar=True`).
5. Se o arquivo de saída já existir, uma nova planilha é criada com data e hora no nome,
   para nunca apagar uma versão já conferida.
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
