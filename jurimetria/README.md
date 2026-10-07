# Jurimetria – banco de decisões judiciais

Extrai dados padronizados de PDFs de decisões judiciais com Gemini e monta uma
planilha para análise estratégica dos processos de uma parte.

| Arquivo | Para quê |
|---|---|
| `jurimetria_decisoes.ipynb` | Notebook para abrir no Google Colab e usar no dia a dia |
| `extrator_decisoes.py` | Código de extração (esquema, livro de códigos, planilha) |
| `gerar_notebook.py` | Regenera o notebook depois de editar o `.py` |

## Uso rápido
1. Abra `jurimetria_decisoes.ipynb` no Colab (Arquivo > Fazer upload de notebook).
2. Coloque os PDFs (um por decisão) numa pasta do Google Drive.
3. Preencha a célula de configuração: acesso (projeto Google Cloud **ou** chave da
   Gemini API), modelo, nome da parte monitorada e pastas.
4. Teste com um único PDF (seção 4), ajuste o livro de códigos se precisar, depois
   rode o lote (seções 6 e 7).
5. Confira as linhas com **alertas** e marque `conferido = SIM`.

## Planilha gerada
- **Processos**: uma linha por número CNJ, com colunas de preenchimento manual
  (desfecho final, valor efetivamente recebido, trânsito/arquivamento).
- **Decisoes**: uma linha por decisão, com resultado para a parte monitorada.
- **Teses**: uma linha por tese enfrentada, com o trecho literal que a comprova.
- **Livro_de_Codigos**: definição de cada código de tese.
- **Controle_Arquivos**: status de cada PDF (ok, ignorado, erro).

## Cuidados
- Use conta paga para dados reais (LGPD); não processe autos em segredo de justiça.
- Toda extração exige conferência humana antes de entrar nas análises.
