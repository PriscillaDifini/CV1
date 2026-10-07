#!/usr/bin/env bash
# Cria o bucket privado para os autos. Rode no Cloud Shell (console.cloud.google.com,
# ícone ">_" no canto superior direito) depois de preencher as duas variáveis abaixo.
set -euo pipefail

PROJECT_ID="seu-projeto"            # ID do projeto (não o nome de exibição)
BUCKET="jurimetria-xxxxxxxx"        # nome único no mundo; não use nomes de partes ou clientes

gcloud config set project "$PROJECT_ID"
gcloud services enable aiplatform.googleapis.com storage.googleapis.com

# Região São Paulo; acesso uniforme; bloqueio de qualquer acesso público.
gcloud storage buckets create "gs://$BUCKET" \
  --location=southamerica-east1 \
  --uniform-bucket-level-access \
  --public-access-prevention

# Rede de segurança: apaga após 1 dia os blocos temporários (pasta _tmp/) gerados
# ao fatiar autos. O notebook já os apaga; isto cobre execuções interrompidas.
cat > /tmp/ciclo_vida.json <<'JSON'
{"rule": [{"action": {"type": "Delete"}, "condition": {"age": 1, "matchesPrefix": ["_tmp/"]}}]}
JSON
gcloud storage buckets update "gs://$BUCKET" --lifecycle-file=/tmp/ciclo_vida.json

echo "Bucket criado: gs://$BUCKET"
echo "Envie os autos com: gcloud storage cp *.pdf gs://$BUCKET/caso-01/autos/"
