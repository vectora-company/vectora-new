# Estudos técnicos do Evaluation Lab

Este documento registra uma auditoria estática do código e um protocolo para
experimentos reproduzíveis. Ele não apresenta resultados de execução próprios:
nenhum modelo NCM, checkpoint externo, dataset de terceiros ou provider remoto
foi executado nesta revisão. A revisão está identificada pelo commit do Vectora
que contém este arquivo e pelo SHA do repositório NCM consultado.

## Como interpretar a evidência

| Classe                  | O que significa                                                                   |
| ----------------------- | --------------------------------------------------------------------------------- |
| Alegação publicada      | Resultado ou descrição atribuída ao paper ou ao README dos autores.               |
| Código inspecionado     | Comportamento visível no commit citado; não prova desempenho nem uso em produção. |
| Experimento proposto    | Hipótese que ainda precisa ser executada e pode ser refutada.                     |
| Experimento reproduzido | Só deve ser preenchido com artefatos, comando, amostra e ambiente registrados.    |
| Sem evidência           | Não permite concluir que a capacidade funciona ou que não funciona.               |

Não combinar números entre hardware, modelos, prompts, fixtures, políticas,
providers ou versões incompatíveis. Os relatórios devem manter qualidade,
desempenho, custo, segurança e recursos em dimensões distintas.

## Next-Concept Modeling (NCM)

### Alegações dos autores

O paper descreve uma arquitetura de duas etapas: um tokenizer residual VQ
multiescala cria códigos discretos hierárquicos e um transformer os prediz em
ordem coarse-to-fine, em paralelo dentro de cada nível. O abstract atribui ao
NCM resultados de velocidade e qualidade em LM1B, OpenWebText, XSum e Sudoku.
Esses valores são alegações dos autores; não foram reproduzidos por este projeto
e não são números de benchmark do Vectora.

Mais especificamente, o abstract reporta 21× em LM1B contra o baseline DLM
mais forte em perplexidade generativa, 10× em OpenWebText com qualidade
competitiva, e 31× em XSum com ROUGE-L equivalente; para Sudoku, reporta melhor
resultado que baselines autoregressivos e de difusão. Cada fator está limitado
ao task, baseline, tamanho de modelo, hardware e método do paper.

Fontes primárias: [paper arXiv 2610.14162](https://arxiv.org/abs/2610.14162) e
[repositório HazyResearch/ncm](https://github.com/HazyResearch/ncm/tree/b451c04a06fdd0822109de67ec000e855b1f5d83).
O checkout de código inspecionado foi `b451c04a06fdd0822109de67ec000e855b1f5d83`.

### Código e método observados

- `models/next_concept.py` implementa predição por níveis; a inferência aceita
  KV cache. `utils/fast_generation.py` possui caminho de CUDA Graph e
  `utils/generation.py` conecta essas opções ao gerador.
- `benchmark.py` valida configuração e assets antes de lançar um worker; a
  configuração fixa precisão e opções de compilação, resize de matmul, CUDA
  Graphs e KV cache. Uma invocação seleciona uma GPU, enquanto `evaluate.py`
  distribui shards para avaliação.
- `utils/benchmarking.py` registra SHA de checkpoints e datasets, versão do
  torch/CUDA, GPU, configurações e hashes do código. O timer sincroniza CUDA e
  mede preparação/tokenização, transferência H2D, geração, decode e D2H. Carga
  de checkpoint/dados, compilação, captura de graph, warmup, scoring e I/O ficam
  fora do intervalo cronometrado.
- A rotina executa warmup por forma de batch e repetições cronometradas, relata
  mediana, média, desvio, mínimo/máximo e memória CUDA alocada/reservada. O
  resultado também compara contadores de compilação e capturas de graph antes e
  depois da medição e marca o benchmark incompleto se eles mudarem. O
  README dos autores identifica os números publicados como BF16 em uma H100 de
  80 GB, com versões específicas de PyTorch e CUDA. Isso limita a generalização.
- A interface inspecionada gera amostras completas/trajectories; não foi
  encontrada medição de streaming de primeiro token. O benchmark descrito não
  mede concorrência multiusuário na mesma GPU, footprint de memória de sistema
  nem uma integração com o runtime do Vectora.
- Os datasets e pesos são assets publicados separadamente. Uma réplica precisa
  fixar revisões imutáveis e hashes, além de registrar licenças e a seleção de
  amostras. O script original escolhe as primeiras N amostras na ordem salva
  quando limita conjuntos condicionais; não tratar isso como amostragem aleatória.

### Experimento de réplica proposto

**Hipótese refutável:** no ambiente de réplica e nas mesmas tarefas, o caminho
NCM entrega a qualidade publicada dentro da tolerância definida e mantém a
vantagem de latência quando ambas as implementações usam o mesmo hardware,
precisão, amostras, batch e fronteira de medição. A hipótese é refutada se a
qualidade cair abaixo do limite pré-registrado ou se a razão de latência deixar
de superar o limiar acordado.

- **Variável:** implementação (NCM versus baseline explicitamente identificado).
- **Controles:** hardware e GPU, precisão, versões de runtime, checkpoints,
  datasets, hashes, exemplos/ordem, batch, tamanho de saída, opções de inferência,
  warmup, número de repetições e fronteira de timing.
- **Amostra:** usar a partição completa quando houver recursos; para um piloto,
  registrar um subconjunto fixo com os IDs exatos. Não escolher amostras após ver
  os resultados.
- **Qualidade:** usar a métrica própria da tarefa (ROUGE/BLEU/perplexity ou
  Sudoku resolvido) e a configuração de scoring registrada. Não comparar texto
  por igualdade exata.
- **Desempenho:** repetir pelo menos 30 vezes por condição, separar carga fria,
  warmup e execução quente, publicar distribuição/percentis e intervalo de
  incerteza, além de memória CUDA e de sistema.
- **Streaming e concorrência:** medir tempo até a primeira saída observável,
  tempo total e throughput com 1/2/4 sessões simultâneas; usar limites e
  isolamento equivalentes. Isso é uma extensão do benchmark publicado, não uma
  réplica direta do seu resultado de throughput.
- **Limites:** os números do paper não abrangem ferramentas, RAG, políticas,
  workbenches ou a latência de serviço do Vectora. Resultado em uma GPU não
  estabelece escala multi-GPU ou experiência interativa.

Comando de referência do repositório externo, **não executado nesta revisão**:

```bash
python benchmark.py \
  --config config/benchmark/xsum.yaml \
  --gpu 0 \
  --samples 1024 \
  --batch-size 16 \
  --warmup 25 \
  --repeats 30 \
  --out outputs/benchmark/xsum-replica
```

Para uma reprodução, fixar também o commit `b451c04a06fdd0822109de67ec000e855b1f5d83`,
as revisões de checkpoint/dataset e seus hashes; o comando acima, isoladamente,
não fixa esses assets.

## Harness, Workbenches, contexto e RAG no Vectora

Esta parte é uma leitura estática do código, não uma medição de comportamento em
produção. Pontos de entrada relevantes:

| Área                       | Evidência no código                                                                                                                                                                                                                                                                                                       | O que ainda precisa de experimento                                                                                                                                                                       |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Harness de decisão         | `backend/engine/conversation_loop.py::_build_selected_decision_harness` seleciona o provider do run; `backend/tools/systemone_tools.py::_build_default_harness` monta configuração, cache e budget; `backend/llm/systemone/harness.py::SystemOneHarness` contém semáforos de concorrência, fila, cache e circuit breaker. | Medir quais chamadas realmente passam pelo harness, overhead por padrão/provider, hit/miss de cache, espera de fila, timeout/fallback e fairness entre sessões. Não inferir que toda tool usa o harness. |
| Registro/execução de tools | `backend/tools/registry.py::ToolRegistry` separa registro ativo e toolsets adiados; `backend/engine/conversation_loop.py` coordena o caminho de decisão e dispatch.                                                                                                                                                       | Rastrear cada família de tool desde registro até execução autorizada; identificar caminhos de workbench que chamam APIs diretamente e verificar quais controles compartilham.                            |
| Contexto do workbench      | `backend/tools/workspace.py::get_workbench_context` lê o contexto de arquivos focados/abertos por workspace; handlers dedicados ficam em `backend/api/handlers/`.                                                                                                                                                         | Trocar workspace durante runs com barreiras controladas e confirmar identidade, contexto e destino de cada efeito; medir tamanho/contexto e custo de leitura.                                            |
| RAG                        | `backend/tools/rag.py` implementa busca vetorial, fusão híbrida RRF, reranking opcional, proveniência e telemetria; `backend/services/retrieval_eval.py` calcula Recall@k e reciprocal rank para fixtures.                                                                                                                | Comparar vector versus hybrid e reranker com corpus/queries congelados; medir Recall@k/MRR, latência, custo e recursos separadamente, com mesma política e mesma versão do índice.                       |
| Ciclo de embeddings        | `backend/api/server.py` inicia o worker de embeddings no startup; ingestão enfileira conteúdo para indexação.                                                                                                                                                                                                             | Medir atraso de ingestão até disponibilidade no retrieval, estado após falha/restart, tamanho de fila e consistência do índice.                                                                          |

Para cada hipótese do Vectora, o primeiro experimento deve usar fixtures locais
sintéticas, uma máquina/ambiente registrados e ao menos 30 repetições por
condição de desempenho. Provider remoto é opt-in explícito e recebe somente
conteúdo sintético aprovado. O controle é a mesma versão/fixture/policy com uma
única variável alterada; o relatório registra n efetivo, falhas, dispersão e
limites de confiança. Se a medição de qualidade for determinística, publicar a
contagem exata e os itens avaliados em vez de inferir incerteza estatística.

### Registro de evidências e limites da auditoria

As referências abaixo fixam o código que sustenta esta leitura. A revisão
estática do Vectora usou o commit `b643bf7`; os links ao upstream NCM apontam ao
commit imutável `b451c04a06fdd0822109de67ec000e855b1f5d83`. Os links identificam
o material consultado, mas não substituem execução, revisão independente ou
adoção pelo produto.

| Conclusão limitada                                                                                                                                            | Evidência identificável                                                                                                                                                                                                                                                                                                                                                                                    | O que ela não prova                                                                                   |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| NCM gera por níveis e oferece cache KV no caminho de geração.                                                                                                 | [Implementação do transformer NCM](https://github.com/HazyResearch/ncm/blob/b451c04a06fdd0822109de67ec000e855b1f5d83/models/next_concept.py) e [configuração do gerador](https://github.com/HazyResearch/ncm/blob/b451c04a06fdd0822109de67ec000e855b1f5d83/utils/generation.py).                                                                                                                           | Qualidade ou latência no Vectora, ou comportamento com tools e contexto de workbench.                 |
| O benchmark NCM valida assets/configuração antes do worker e registra medições, hashes e opções de execução.                                                  | [CLI do benchmark](https://github.com/HazyResearch/ncm/blob/b451c04a06fdd0822109de67ec000e855b1f5d83/benchmark.py), [coleta de medições e metadados](https://github.com/HazyResearch/ncm/blob/b451c04a06fdd0822109de67ec000e855b1f5d83/utils/benchmarking.py) e [protocolo publicado no README](https://github.com/HazyResearch/ncm/blob/b451c04a06fdd0822109de67ec000e855b1f5d83/README.md#benchmarking). | Uma réplica independente dos números publicados ou equivalência com outro hardware.                   |
| O caminho principal do Vectora escolhe explicitamente o provider do Decision Model e monta o harness; o harness contém limites de concorrência, fila e cache. | [`_build_selected_decision_harness`](https://github.com/vectora-company/vectora-new/blob/b643bf7/backend/engine/conversation_loop.py#L266-L276) e [estado e semáforos de `SystemOneHarness`](https://github.com/vectora-company/vectora-new/blob/b643bf7/backend/llm/systemone/harness.py#L188-L245).                                                                                                      | Que toda ferramenta ou workbench passe por esse harness.                                              |
| O registry separa tools ativas de toolsets adiados, e o contexto do workbench vem de estado associado ao workspace.                                           | [`ToolRegistry`](https://github.com/vectora-company/vectora-new/blob/b643bf7/backend/tools/registry.py#L159-L199) e [`get_workbench_context`](https://github.com/vectora-company/vectora-new/blob/b643bf7/backend/tools/workspace.py#L260-L282).                                                                                                                                                           | Que todos os caminhos da interface usem os mesmos controles de autorização, auditoria e cancelamento. |
| O retrieval pode seguir vector ou hybrid RRF, registrar proveniência e tentar reranking; há métricas locais Recall@k/MRR sobre IDs fornecidos.                | [`vector_search`](https://github.com/vectora-company/vectora-new/blob/b643bf7/backend/tools/rag.py#L503-L525) e [avaliação determinística de retrieval](https://github.com/vectora-company/vectora-new/blob/b643bf7/backend/services/retrieval_eval.py#L15-L75).                                                                                                                                           | Resultado de qualidade em corpus de produção ou de qualquer provider específico.                      |
| A gravação de interações para o Evaluation Lab fica desligada por padrão; o smoke test instalado confirma startup, sem executar tarefa ou provider.           | [`evaluation_recording_enabled`](https://github.com/vectora-company/vectora-new/blob/b643bf7/backend/settings.py#L458-L459), [captura opt-in](https://github.com/vectora-company/vectora-new/blob/b643bf7/backend/engine/conversation_loop.py#L1570-L1601) e [limites do smoke instalado](https://github.com/vectora-company/vectora-new/blob/b643bf7/backend/evaluation/installed.py#L220-L230).          | Um benchmark end-to-end dos fluxos do app; essa evidência ainda não está disponível nesta auditoria.  |

Os resultados de `backend/evaluation/isolation.py` e seus testes são evidência
do mecanismo de teste e do journal da bancada. Eles não demonstram que todos os
efeitos de produção já são despachados por esse mecanismo. O estudo continua
sem execução de provider, sem medição de qualidade e sem benchmark do app
instalado; cada uma dessas lacunas permanece separada de `passed` e de qualquer
claim de maturidade.

### Hipóteses experimentais

1. **Harness:** sob uma mistura fixa de padrões de baixo e alto risco, o harness
   mantém decisões autorizadas e auditáveis dentro do limite de latência
   pré-registrado. Refutar se alguma ação protegida escapar do gate, se a decisão
   auditada divergir da executada ou se p95 exceder o limite. Controlar provider,
   padrão, policy, payload, cache frio/quente e concorrência.
2. **Isolamento e recuperação:** trocar workspace ou revogar policy enquanto
   uma operação espera não altera o workspace capturado nem permite dispatch sem
   grant atual. Usar fixtures temporárias, barreiras, IDs de efeito fixos e
   verificar filesystem/ledger/processos após cancelamento. As salvaguardas
   testadas na stack atual são evidência do harness de avaliação, não prova de
   que toda a aplicação já usa o mesmo executor.
3. **RAG:** com corpus e queries congelados, uma configuração de retrieval só é
   melhor se a qualidade de recuperação aumentar sem violar limites de latência,
   custo ou recursos. Registrar embedding, índice, chunks, filtro, `k`, reranker,
   policy e hashes. Refutar a melhoria se Recall@k/MRR cair ou se o custo exceder
   o limite pré-registrado.
4. **Workbench:** para cada superfície (Git, browser, context graph, memória e
   terminal), comparar o caminho UI/API com o caminho de tool do agente. Hipótese
   de contrato comum só é aceita quando identidade, autorização, isolamento,
   auditoria, cancelamento e estados terminais coincidem em testes instrumentados.

Cada experimento deve gravar comando, SHA, `run_id`, ambiente, configuração
redigida/digest, policy, fixture, avaliador, estados e limitações no Evaluation
Lab. Uma execução `blocked`, `skipped`, `cancelled` ou `not_run` nunca entra como
sucesso ou amostra zero.

## Conclusões permitidas nesta revisão

- **Código inspecionado:** o checkout consultado do NCM contém um benchmark com
  metadados de configuração e sincronização CUDA; a implementação publicada
  não demonstra, por si só, integração NCM com Vectora.
- **Alegação externa:** os resultados quantitativos continuam atribuídos ao
  paper e ao ambiente descrito pelos autores.
- **Sem evidência:** não há réplica de NCM nesta entrega, nem medição própria de
  sua qualidade, serving, streaming, concorrência ou footprint no ambiente Vectora.
- **Inferência limitada:** a configuração pode servir como base para um
  experimento local controlado; isso não implica que NCM seja adequado para
  produção nem que uma recomendação de arquitetura esteja aprovada.

Uma revisão técnica independente dos assets, comandos e artefatos deve ocorrer
antes de reutilizar qualquer número em material público.
