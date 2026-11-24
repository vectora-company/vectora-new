# Evaluation Lab

O Evaluation Lab separa planejamento, replay de cassete, execução de runner de
fixture e inicialização de um app instalado. Um resultado de uma modalidade não
vale como evidência de outra.

## Runner de cenário por subprocesso

`vectora eval scenarios` só lê o catálogo e informa prontidão. Ele não inicia
runner nem escreve relatório. Para executar um runner contra a fixture
versionada de um cenário:

```bash
vectora eval scenario \
  --scenario-id workspace-project-open-v1 \
  --runner python \
  --runner-arg /caminho/absoluto/para/runner.py \
  --report-dir ./artifacts/workspace-project-open
```

O diretório de relatório precisa ser novo. O processo recebe caminhos temporários
pelas variáveis `VECTORA_EVAL_SCENARIO_PATH`, `VECTORA_EVAL_FIXTURE_PATH`,
`VECTORA_EVAL_RESULT_PATH`, `VECTORA_EVAL_RUN_ID` e
`VECTORA_EVALUATION_WORKSPACE`. A fixture é copiada para o diretório temporário;
o runner deve gravar em `VECTORA_EVAL_RESULT_PATH` um JSON neste formato:

```json
{
  "schema_version": 1,
  "scenario_id": "workspace-project-open-v1",
  "assertions": [
    { "name": "README disponível", "status": "passed" },
    {
      "name": "aprovação humana auditável",
      "status": "unsupported",
      "reason": "o fixture não contém trilha de aprovação"
    }
  ],
  "metrics": []
}
```

Estados válidos para cada critério: `passed`, `failed`, `blocked`, `skipped` e
`unsupported`. Uma lista vazia de critérios, JSON ausente/inválido, cenário
divergente, saída não zero ou timeout nunca produz `passed`. O runner recebe um
ambiente doméstico e workspace temporários; variáveis de ambiente que parecem
credenciais são removidas. O subprocesso é encerrado junto com seus filhos ao
atingir o limite ou ao cancelar.

Para executar todos os cenários do catálogo, use `vectora eval suite` com o
mesmo contrato de runner. Cada cenário inicia um subprocesso e workspace novos;
`--timeout-seconds` vale individualmente para cada processo e não limita a
duração total da suite. Também é possível repetir `--scenario-id` para selecionar
cenários. A suite grava um relatório agregado e um par JSON/Markdown para cada
cenário. `passed`, `unsupported`, `blocked` e os demais estados continuam
visíveis por cenário e no resumo; um cenário não suportado não vira aprovação.

```bash
vectora eval suite \
  --runner python \
  --runner-arg /caminho/absoluto/para/runner.py \
  --timeout-seconds 120 \
  --report-dir ./artifacts/vectora-core-flows
```

`vectora eval scenarios` continua sendo apenas planejamento: não inicia o
executável e não grava relatório.

O relatório JSON e Markdown marca `fixture_runner`, separado do `fixture_replay`
de cassetes, e inclui o hash da fixture,
digest do comando, `run_id`, status por critério e duração. Ele não comprova que
o app instalado, a interface ou um provider foram exercitados; essa evidência
não é capturada nesse modo. O sistema operacional também não isola a rede do
subprocesso, então use um runner offline e fixtures sintéticas. Para verificar
inicialização de uma instalação, use `vectora eval smoke`; para replay de
interações registradas, use `vectora eval run --suite FILE`.

O `vectora eval smoke` inicia uma instalação com perfil e workspace isolados e
registra tempos monotônicos para Electron pronto, backend iniciado/pronto,
renderer carregado, janela pronta e encerramento. Esses tempos medem etapas do
processo Electron; não atribuem duração individual a cada sidecar. Versões sem
os marcadores estruturados continuam sendo avaliadas pelo resultado de startup,
com as etapas ausentes marcadas como `skipped`.

O backend recebe o mesmo `run_id` e pode registrar etapas estruturadas para
migrations SQLite, tabela de sessões, storage configurado, worker de embeddings,
scheduler e restauração de llama.cpp. O NATS reporta `ready`, `failed` ou
`skipped` quando for solicitado. Esses estados ficam em `backend_stages` e não
são colapsados no resultado geral do smoke; assim um app que abriu pode mostrar
separadamente uma dependência opcional ausente ou com falha. O marcador não
inclui valores de configuração nem credenciais.

O relatório também captura versão do Vectora, Electron, Chromium, release do
sistema operacional e SHA embutido no build. O indicador `source_dirty` mostra
se havia alterações na árvore quando o Electron foi compilado. Builds sem Git
ou metadados válidos deixam SHA/estado ausentes; o smoke não os infere da
máquina de avaliação.

Com `evaluation_recording_enabled` e `run_id`, chamadas do provider registram
tokens de entrada/saída, TTFT e `output_tokens_per_second`. A taxa usa apenas
tokens de saída informados pelo provider e a janela entre o primeiro texto e o
fim do stream; sem ambos, o relatório guarda `unavailable` com motivo.
