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
divergente, critério omitido/duplicado, nome fora do catálogo, saída não zero ou
timeout nunca produz `passed`. Cada nome de assertion precisa corresponder
exatamente a um critério versionado daquele cenário. O runner recebe um
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
digest do comando, `run_id`, maturidade da capability, status por critério e
duração. Maturidade (`unassessed`, `experimental`, `beta` ou `stable`) é
independente do resultado da execução; o catálogo mantém capabilities sem
validação real como `unassessed`. O runner não comprova que
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

Para verificar que a interface realmente montou conteúdo, use
`vectora eval ui-smoke --app /caminho/para/Vectora --report ui-smoke.json`.
Esse modo usa uma porta CDP efêmera restrita a `127.0.0.1`, espera o renderer
instalado concluir o carregamento e exige elementos filhos em `#root`. O
relatório usa `scenario_id=installed-app-ui` e guarda o resultado em
`renderer_status`, sem classificar o teste como execução de tarefa, teste de
qualidade ou evidência de provider. O app e os processos filhos são encerrados
ao terminar a verificação; o limite total inclui inicialização, inspeção e
encerramento. O endpoint não é habilitado no smoke comum nem em uso normal.

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
máquina de avaliação. O Electron encaminha os mesmos metadados ao backend para
que medições opt-in de provider guardem a revisão realmente avaliada. Comparação
e agregação exigem estado dirty conhecido e igual em todas as amostras.
Durante o smoke do app instalado, o avaliador amostra tempo de CPU e memória
residente da árvore de processos do Electron, incluindo filhos. Percentuais de
CPU são relativos a um núcleo lógico; RSS é a soma amostrada dos valores RSS dos
processos e pode contar páginas compartilhadas mais de uma vez. O pico de
processos e a quantidade de CPUs lógicas e memória total do host são registrados
separadamente. A amostragem é intervalar, então pode perder picos curtos. Em
GPUs NVIDIA com NVML e contabilização por processo disponível, o relatório mede
pico de memória GPU dos processos do app. Em outros dispositivos/drivers — e no
Windows WDDM, que não expõe esse valor por processo — a métrica fica
`unavailable` com motivo; utilização global da GPU não é atribuída ao app.
Amostras CPU/RSS ausentes também ficam `unavailable`, nunca zero.

Com `evaluation_recording_enabled` e `run_id`, chamadas do provider registram
tokens de entrada/saída, TTFT e `output_tokens_per_second`. A taxa usa apenas
tokens de saída informados pelo provider e a janela entre o primeiro texto e o
fim do stream; sem ambos, o relatório guarda `unavailable` com motivo.
O adaptador OpenRouter preserva `usage.cost` como `cost_usd`; o Evaluation Lab
registra custo medido somente quando o provider envia valor finito e não negativo.
Ele não estima custo usando tabelas de preços. Sem valor explícito, a métrica fica
`unavailable`.

`vectora eval compare-runs` inclui tamanho da amostra, média, desvio-padrão,
p50/p95 de cada grupo e intervalo normal aproximado para o delta quando ambos
têm ao menos duas observações. O intervalo é descritivo, não um teste de
significância; com amostras pequenas ou variáveis, a comparação deve ser tratada
como inconclusiva. Antes da agregação, todas as execuções precisam compartilhar
build, fixture, policy, avaliador, host, provider, modelo e configuração; perfis
frio/quente permanecem separados. A comparação também rejeita mais de uma
revisão por grupo, mantendo a mudança de SHA entre baseline e candidato explícita.
As métricas continuam separadas por dimensão e unidade.

## Estudos técnicos verificáveis

Use `backend/evaluation/studies/v1/template.json` como ponto de partida e
valide o arquivo antes de compartilhar ou revisar:

```bash
vectora eval study-validate --study estudo.json
```

O formato separa alegações externas, inspeção de código, experimentos
reproduzidos e inferências. Todo estudo registra uma hipótese, um critério que
poderia refutá-la, método, amostra, ambiente, variável, controles, regra de
decisão e limitações. Um estudo planejado fica como `not_run`; marcá-lo concluído
exige resultados observados e referência a código ou experimento. Alegação
externa ou inferência sozinha não sustenta conclusão `supported`/`refuted`.

A validação verifica a estrutura e a força declarada da evidência; não executa
experimentos, autentica a origem das referências nem confirma a conclusão. As
referências devem identificar URLs, caminhos fixados por revisão ou `run_id` que
um revisor consiga consultar. Resultados publicados ainda precisam relatar
amostra e limitações, sem extrapolar o ambiente medido.
