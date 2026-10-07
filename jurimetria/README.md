# Jurimetria – banco de decisões judiciais

Extrai dados padronizados de PDFs de decisões judiciais com Gemini e monta uma
planilha para análise estratégica dos processos de uma parte.

| Arquivo | Para quê |
|---|---|
| `jurimetria_decisoes.ipynb` | Notebook para abrir no Google Colab e usar no dia a dia |
| `extrator_decisoes.py` | Código de extração (esquema, livro de códigos, planilha) |
| `gerar_notebook.py` | Regenera o notebook depois de editar o `.py` |
| `configurar_bucket.sh` | Cria o bucket privado no Cloud Storage (rodar no Cloud Shell) |

## Uso rápido
1. Abra `jurimetria_decisoes.ipynb` no Colab (Arquivo > Fazer upload de notebook).
2. Crie o bucket com `configurar_bucket.sh` e envie os autos para
   `gs://SEU-BUCKET/caso-01/autos/` (ou use uma pasta do Drive, para PDFs pequenos).
3. Preencha a célula de configuração: acesso (projeto Google Cloud **ou** chave da
   Gemini API), modelo, nome da parte monitorada e pastas.
4. Fatie os autos em peças (seção 4), confira os recortes, teste uma peça (seção 5)
   e rode o lote (seções 6 a 8).
5. Confira as linhas com **alertas** e marque `conferido = SIM`.

## Planilha gerada
- **Processos**: uma linha por número CNJ, com colunas de preenchimento manual
  (desfecho final, valor efetivamente recebido, trânsito/arquivamento).
- **Decisoes**: uma linha por decisão, com resultado para a parte monitorada.
- **Teses**: uma linha por tese enfrentada, com o trecho literal que a comprova.
- **Defesas**: uma linha por contestação ou recurso (escritório, provas, acordo).
- **Argumentos_Defesa**: cada argumento da peça, na ordem, com categoria e fundamentos.
- **Livro_de_Codigos**: definição de cada código de tese.
- **Controle_Arquivos**: status de cada PDF (ok, ignorado, erro).

## Cuidados
- Use conta paga para dados reais (LGPD); não processe autos em segredo de justiça.
- Toda extração exige conferência humana antes de entrar nas análises.
